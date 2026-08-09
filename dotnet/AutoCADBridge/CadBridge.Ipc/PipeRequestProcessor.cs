using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using CadBridge.Contracts;

namespace CadBridge.Ipc;

/// <summary>
/// Creates a local named-pipe server with an ACL restricted to <see cref="PipeEndpoint.AllowedUserSid"/>.
/// The IPC assembly deliberately has no insecure default implementation: the Windows host must inject a
/// factory which applies the security descriptor before exposing the pipe.
/// </summary>
public interface ILocalNamedPipeFactory
{
    Stream CreateServer(PipeEndpoint endpoint);
}

public sealed record PipeEndpoint(string PipeName, string AllowedUserSid);

public sealed record PipeServerOptions
{
    private static readonly Regex SidPattern = new(
        @"^S-[0-9]+(?:-[0-9]+)+$",
        RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);

    private static readonly Regex SchemaVersionPattern = new(
        @"^[0-9]+\.[0-9]+$",
        RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);

    public required string PipeNameTemplate { get; init; }

    public required string UserSid { get; init; }

    public int MaxRequestBytes { get; init; } = 1_048_576;

    public int MaxRequestDepth { get; init; } = 32;

    public string SupportedSchemaVersion { get; init; } = IpcContract.CurrentSchemaVersion;

    public PipeEndpoint CreateEndpoint()
    {
        if (!SidPattern.IsMatch(UserSid))
        {
            throw new ArgumentException("UserSid must be a canonical Windows SID.", nameof(UserSid));
        }

        if (!PipeNameTemplate.Contains("{user_sid}", StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "PipeNameTemplate must contain the per-user {user_sid} token.",
                nameof(PipeNameTemplate));
        }

        if (MaxRequestBytes is < 4 or > 16_777_216)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaxRequestBytes),
                "MaxRequestBytes must be between 4 bytes and 16 MiB.");
        }

        if (MaxRequestDepth is < 1 or > 128)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaxRequestDepth),
                "MaxRequestDepth must be between 1 and 128.");
        }

        if (!SchemaVersionPattern.IsMatch(SupportedSchemaVersion))
        {
            throw new ArgumentException(
                "SupportedSchemaVersion must use major.minor syntax.",
                nameof(SupportedSchemaVersion));
        }

        var pipeName = PipeNameTemplate.Replace("{user_sid}", UserSid, StringComparison.Ordinal);
        if (pipeName.Length is < 1 or > 256 ||
            pipeName.IndexOfAny(['\\', '/', ':']) >= 0 ||
            pipeName.Any(character => !(char.IsAsciiLetterOrDigit(character) || character is '.' or '_' or '-')) ||
            !pipeName.Contains(UserSid, StringComparison.Ordinal))
        {
            throw new ArgumentException("Resolved pipe name is not a safe local per-user name.", nameof(PipeNameTemplate));
        }

        return new PipeEndpoint(pipeName, UserSid);
    }

    /// <summary>
    /// Opens the endpoint only through an injected factory whose contract requires applying the
    /// per-user ACL. This keeps parsing tests cross-platform without providing an insecure fallback.
    /// </summary>
    public Stream CreateSecuredServer(ILocalNamedPipeFactory factory)
    {
        ArgumentNullException.ThrowIfNull(factory);
        return factory.CreateServer(CreateEndpoint());
    }
}

/// <summary>
/// A handler-owned result without wire-envelope fields.  The processor remains the sole owner of
/// schema version, request identity, terminal status rendering and error-envelope construction.
/// </summary>
public sealed record PipeHandlerResult
{
    private PipeHandlerResult(IpcResponseStatus status, JsonObject? data, IpcError? error)
    {
        Status = status;
        Data = data;
        Error = error;
    }

    public IpcResponseStatus Status { get; }

    public JsonObject? Data { get; }

    public IpcError? Error { get; }

    public static PipeHandlerResult Ok(JsonObject? data = null) =>
        new(IpcResponseStatus.Ok, data, error: null);

    public static PipeHandlerResult Rejected(IpcError error) =>
        new(IpcResponseStatus.Rejected, data: null, RequireError(error));

    public static PipeHandlerResult Conflict(IpcError error) =>
        new(IpcResponseStatus.Conflict, data: null, RequireError(error));

    public static PipeHandlerResult Failed(IpcError error) =>
        new(IpcResponseStatus.Failed, data: null, RequireError(error));

    private static IpcError RequireError(IpcError error)
    {
        ArgumentNullException.ThrowIfNull(error);
        return error;
    }
}

public delegate ValueTask<PipeHandlerResult> PipeRequestHandler(
    JsonElement request,
    CancellationToken cancellationToken);

/// <summary>
/// Bounded, exception-safe IPC boundary. Validation finishes before the handler (and therefore any
/// transaction-opening code) can run.
/// </summary>
public sealed class PipeRequestProcessor
{
    private const string UnknownRequestId = "unknown";

    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);

    private static readonly HashSet<string> AllowedProperties =
    [
        "schema_version",
        "method",
        "request_id",
        "params",
        "idempotency_key",
        "job_id",
    ];

    private static readonly HashSet<string> AllowedMethods =
    [
        "handshake",
        "status",
        "inspect_document",
        "inspect_selection",
        "preview",
        "validate_revision",
        "cancel",
        "commit",
        "rollback",
        "export",
    ];

    private readonly PipeServerOptions _options;
    private readonly RequestCancellationRegistry _requests = new();

    public PipeRequestProcessor(PipeServerOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        _ = options.CreateEndpoint();
        _options = options;
    }

    /// <summary>Requests cancellation of exactly one active request.</summary>
    public bool Cancel(string requestId) => _requests.TryCancel(requestId);

    /// <summary>
    /// Reads one big-endian, length-prefixed UTF-8 JSON request and always returns a framed response.
    /// A declared oversize request is rejected before allocating or reading its body.
    /// </summary>
    public async ValueTask<byte[]> ProcessNextAsync(
        Stream input,
        PipeRequestHandler handler,
        CancellationToken connectionCancellation = default)
    {
        ArgumentNullException.ThrowIfNull(input);
        ArgumentNullException.ThrowIfNull(handler);

        try
        {
            var header = new byte[sizeof(int)];
            if (!await ReadExactlyAsync(input, header, connectionCancellation).ConfigureAwait(false))
            {
                return EncodeFrame(Error(
                    UnknownRequestId,
                    "INVALID_FEATURE_PARAMETERS",
                    "The IPC frame header is incomplete."));
            }

            var length = BinaryPrimitives.ReadInt32BigEndian(header);
            if (length is <= 0 || length > _options.MaxRequestBytes)
            {
                return EncodeFrame(Error(
                    UnknownRequestId,
                    "INVALID_FEATURE_PARAMETERS",
                    "The IPC request size is outside the configured limit."));
            }

            var payload = new byte[length];
            if (!await ReadExactlyAsync(input, payload, connectionCancellation).ConfigureAwait(false))
            {
                return EncodeFrame(Error(
                    UnknownRequestId,
                    "INVALID_FEATURE_PARAMETERS",
                    "The IPC frame body is incomplete."));
            }

            return EncodeFrame(await ProcessPayloadAsync(payload, handler, connectionCancellation).ConfigureAwait(false));
        }
        catch (OperationCanceledException)
        {
            return EncodeFrame(Error(
                UnknownRequestId,
                "IPC_TIMEOUT",
                "The IPC request was cancelled or timed out.",
                retryable: true,
                status: "failed"));
        }
        catch (Exception)
        {
            return EncodeFrame(Error(
                UnknownRequestId,
                "INTERNAL_ERROR",
                "The IPC boundary could not process the request.",
                status: "failed"));
        }
    }

    /// <summary>Processes an already de-framed payload; useful for deterministic contract tests.</summary>
    public async ValueTask<JsonObject> ProcessPayloadAsync(
        ReadOnlyMemory<byte> payload,
        PipeRequestHandler handler,
        CancellationToken connectionCancellation = default)
    {
        ArgumentNullException.ThrowIfNull(handler);
        var requestId = UnknownRequestId;

        try
        {
            if (payload.IsEmpty || payload.Length > _options.MaxRequestBytes)
            {
                return Error(
                    requestId,
                    "INVALID_FEATURE_PARAMETERS",
                    "The IPC request size is outside the configured limit.");
            }

            string json;
            try
            {
                json = StrictUtf8.GetString(payload.Span);
            }
            catch (DecoderFallbackException)
            {
                return Error(requestId, "INVALID_FEATURE_PARAMETERS", "The IPC request is not valid UTF-8.");
            }

            using var document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = _options.MaxRequestDepth,
                });

            var request = document.RootElement;
            if (!TryValidateRequest(request, out requestId, out var validationCode, out var validationMessage))
            {
                return Error(requestId, validationCode, validationMessage);
            }

            if (request.GetProperty("method").GetString() == "cancel")
            {
                return await ProcessCancellationAsync(
                    request,
                    requestId,
                    connectionCancellation).ConfigureAwait(false);
            }

            if (!_requests.TryRegister(requestId, out var lease))
            {
                return Error(
                    requestId,
                    "INVALID_FEATURE_PARAMETERS",
                    "A request with this request_id is already active.");
            }

            using (lease)
            using (var linkedCancellation = CancellationTokenSource.CreateLinkedTokenSource(
                       lease.Token,
                       connectionCancellation))
            {
                try
                {
                    linkedCancellation.Token.ThrowIfCancellationRequested();
                    var result = await handler(request, linkedCancellation.Token).ConfigureAwait(false);
                    linkedCancellation.Token.ThrowIfCancellationRequested();
                    return HandlerResponse(requestId, result);
                }
                catch (OperationCanceledException) when (linkedCancellation.IsCancellationRequested)
                {
                    return Error(
                        requestId,
                        "IPC_TIMEOUT",
                        "The IPC request was cancelled or timed out.",
                        retryable: true,
                        status: "failed");
                }
                catch (Exception)
                {
                    return Error(
                        requestId,
                        "INTERNAL_ERROR",
                        "The bridge handler failed safely.",
                        status: "failed");
                }
            }
        }
        catch (JsonException)
        {
            return Error(
                requestId,
                "INVALID_FEATURE_PARAMETERS",
                "The IPC request is malformed or exceeds the configured JSON depth.");
        }
        catch (OperationCanceledException) when (connectionCancellation.IsCancellationRequested)
        {
            return Error(
                requestId,
                "IPC_TIMEOUT",
                "The IPC request was cancelled or timed out.",
                retryable: true,
                status: "failed");
        }
        catch (Exception)
        {
            return Error(
                requestId,
                "INTERNAL_ERROR",
                "The IPC boundary could not process the request.",
                status: "failed");
        }
    }

    private async ValueTask<JsonObject> ProcessCancellationAsync(
        JsonElement request,
        string controlRequestId,
        CancellationToken connectionCancellation)
    {
        var parameters = request.GetProperty("params");
        if (!TryGetBoundedString(
                parameters,
                "target_request_id",
                minimumLength: 1,
                maximumLength: 64,
                out var targetRequestId) ||
            string.Equals(controlRequestId, targetRequestId, StringComparison.Ordinal))
        {
            return Error(
                controlRequestId,
                "INVALID_FEATURE_PARAMETERS",
                "A cancel request requires a distinct active target_request_id.");
        }

        if (!await _requests.TryCancelAndWaitForTerminalAsync(
                targetRequestId,
                connectionCancellation).ConfigureAwait(false))
        {
            return Error(
                controlRequestId,
                "INVALID_FEATURE_PARAMETERS",
                "The target request is not active.");
        }

        return Success(
            controlRequestId,
            new JsonObject
            {
                ["cancelled_request_id"] = targetRequestId,
                ["terminal"] = true,
            });
    }

    private bool TryValidateRequest(
        JsonElement request,
        out string requestId,
        out string errorCode,
        out string errorMessage)
    {
        requestId = UnknownRequestId;
        errorCode = "INVALID_FEATURE_PARAMETERS";
        errorMessage = "The IPC request does not match the request schema.";

        if (request.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        var seenProperties = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in request.EnumerateObject())
        {
            if (!AllowedProperties.Contains(property.Name) || !seenProperties.Add(property.Name))
            {
                return false;
            }
        }

        if (!TryGetBoundedString(request, "request_id", 1, 64, out requestId) ||
            !TryGetBoundedString(request, "schema_version", 3, 32, out var schemaVersion) ||
            !TryGetBoundedString(request, "method", 1, 64, out var method) ||
            !request.TryGetProperty("params", out var parameters) ||
            parameters.ValueKind != JsonValueKind.Object ||
            !IsOptionalBoundedString(request, "job_id", 64) ||
            !IsOptionalBoundedString(request, "idempotency_key", 128) ||
            !AllowedMethods.Contains(method))
        {
            requestId = IsSafeRequestId(requestId) ? requestId : UnknownRequestId;
            return false;
        }

        if (!string.Equals(schemaVersion, _options.SupportedSchemaVersion, StringComparison.Ordinal))
        {
            errorCode = "UNSUPPORTED_SCHEMA_VERSION";
            errorMessage = "The IPC schema version is not supported.";
            return false;
        }

        return true;
    }

    private static bool TryGetBoundedString(
        JsonElement value,
        string propertyName,
        int minimumLength,
        int maximumLength,
        out string result)
    {
        result = string.Empty;
        return value.TryGetProperty(propertyName, out var property) &&
               property.ValueKind == JsonValueKind.String &&
               property.GetString() is { } text &&
               text.Length >= minimumLength &&
               text.Length <= maximumLength &&
               (result = text) is not null;
    }

    private static bool IsOptionalBoundedString(JsonElement value, string propertyName, int maximumLength)
    {
        if (!value.TryGetProperty(propertyName, out var property) || property.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        return property.ValueKind == JsonValueKind.String && property.GetString() is { } text && text.Length <= maximumLength;
    }

    private static bool IsSafeRequestId(string requestId) => requestId.Length is >= 1 and <= 64;

    private JsonObject Success(string requestId, JsonObject? data) => new()
    {
        ["schema_version"] = _options.SupportedSchemaVersion,
        ["request_id"] = requestId,
        ["status"] = "ok",
        ["data"] = data ?? new JsonObject(),
    };

    private JsonObject HandlerResponse(string requestId, PipeHandlerResult? result)
    {
        if (result is null)
        {
            return Error(
                requestId,
                "INTERNAL_ERROR",
                "The bridge handler returned an invalid result.",
                status: "failed");
        }

        if (result.Status == IpcResponseStatus.Ok)
        {
            if (result.Error is not null)
            {
                return Error(
                    requestId,
                    "INTERNAL_ERROR",
                    "The bridge handler returned an invalid result.",
                    status: "failed");
            }

            return Success(requestId, result.Data?.DeepClone().AsObject());
        }

        if (result.Data is not null || result.Error is null)
        {
            return Error(
                requestId,
                "INTERNAL_ERROR",
                "The bridge handler returned an invalid result.",
                status: "failed");
        }

        var status = result.Status switch
        {
            IpcResponseStatus.Rejected => "rejected",
            IpcResponseStatus.Conflict => "conflict",
            IpcResponseStatus.Failed => "failed",
            _ => throw new InvalidOperationException("Unsupported handler result status."),
        };

        return Error(
            requestId,
            result.Error.Code,
            result.Error.Message,
            result.Error.Retryable,
            status,
            result.Error.Details,
            result.Error.RequiredAction);
    }

    private JsonObject Error(
        string requestId,
        string code,
        string message,
        bool retryable = false,
        string status = "rejected",
        IReadOnlyDictionary<string, JsonElement>? details = null,
        string? requiredAction = null)
    {
        var error = new JsonObject
        {
            ["code"] = code,
            ["message"] = message,
            ["retryable"] = retryable,
            ["details"] = details is null
                ? new JsonObject()
                : JsonSerializer.SerializeToNode(details, IpcJson.Options),
        };
        if (requiredAction is not null)
        {
            error["required_action"] = requiredAction;
        }

        return new JsonObject
        {
            ["schema_version"] = _options.SupportedSchemaVersion,
            ["request_id"] = IsSafeRequestId(requestId) ? requestId : UnknownRequestId,
            ["status"] = status,
            ["error"] = error,
        };
    }

    private static byte[] EncodeFrame(JsonObject response)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(response);
        var frame = new byte[sizeof(int) + payload.Length];
        BinaryPrimitives.WriteInt32BigEndian(frame, payload.Length);
        payload.CopyTo(frame.AsSpan(sizeof(int)));
        return frame;
    }

    private static async ValueTask<bool> ReadExactlyAsync(
        Stream input,
        Memory<byte> destination,
        CancellationToken cancellationToken)
    {
        var read = 0;
        while (read < destination.Length)
        {
            var count = await input.ReadAsync(destination[read..], cancellationToken).ConfigureAwait(false);
            if (count == 0)
            {
                return false;
            }

            read += count;
        }

        return true;
    }

    private sealed class RequestCancellationRegistry
    {
        private const int MaximumPendingCancellations = 256;
        private static readonly TimeSpan PendingCancellationLifetime = TimeSpan.FromSeconds(5);

        private readonly object _gate = new();
        private readonly Dictionary<string, RequestEntry> _active = new(StringComparer.Ordinal);
        private readonly Dictionary<string, PendingCancellation> _pending = new(StringComparer.Ordinal);

        public bool TryRegister(string requestId, out RequestLease lease)
        {
            lock (_gate)
            {
                if (_active.ContainsKey(requestId))
                {
                    lease = null!;
                    return false;
                }

                PendingCancellation? pendingCancellation = null;
                if (_pending.Remove(requestId, out var pending))
                {
                    pendingCancellation = pending;
                    pending.Bind();
                }

                var entry = new RequestEntry(pendingCancellation);
                _active.Add(requestId, entry);
                lease = new RequestLease(this, requestId, entry);
                return true;
            }
        }

        public bool TryCancel(string requestId)
        {
            lock (_gate)
            {
                return _active.TryGetValue(requestId, out var entry) && entry.TryCancel();
            }
        }

        public async ValueTask<bool> TryCancelAndWaitForTerminalAsync(
            string requestId,
            CancellationToken cancellationToken)
        {
            Task<bool> terminal;
            lock (_gate)
            {
                if (_active.TryGetValue(requestId, out var entry))
                {
                    if (!entry.TryCancel())
                    {
                        return false;
                    }

                    terminal = WaitForActiveTerminalAsync(entry);
                }
                else if (_pending.TryGetValue(requestId, out var existing))
                {
                    terminal = existing.Terminal;
                }
                else
                {
                    if (_pending.Count >= MaximumPendingCancellations)
                    {
                        return false;
                    }

                    var pending = new PendingCancellation(
                        () => Expire(requestId),
                        PendingCancellationLifetime);
                    _pending.Add(requestId, pending);
                    terminal = pending.Terminal;
                }
            }

            return await terminal.WaitAsync(cancellationToken).ConfigureAwait(false);

            static async Task<bool> WaitForActiveTerminalAsync(RequestEntry entry)
            {
                await entry.WaitForTerminalAsync(CancellationToken.None).ConfigureAwait(false);
                return true;
            }
        }

        public void Complete(string requestId, RequestEntry entry)
        {
            lock (_gate)
            {
                entry.Complete();
                if (_active.TryGetValue(requestId, out var active) && ReferenceEquals(active, entry))
                {
                    _active.Remove(requestId);
                }
            }
        }

        private void Expire(string requestId)
        {
            lock (_gate)
            {
                if (_pending.Remove(requestId, out var pending))
                {
                    pending.Expire();
                }
            }
        }
    }

    private sealed class RequestEntry
    {
        private readonly object _gate = new();
        private readonly CancellationTokenSource _source = new();
        private readonly TaskCompletionSource _terminalCompletion = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly PendingCancellation? _pendingCancellation;
        private bool _terminal;

        public RequestEntry(PendingCancellation? pendingCancellation = null)
        {
            _pendingCancellation = pendingCancellation;
            if (pendingCancellation is not null)
            {
                _source.Cancel();
            }
        }

        public CancellationToken Token => _source.Token;

        public bool TryCancel()
        {
            lock (_gate)
            {
                if (_terminal)
                {
                    return false;
                }

                _source.Cancel();
                return true;
            }
        }

        public void Complete()
        {
            var completed = false;
            lock (_gate)
            {
                if (_terminal)
                {
                    return;
                }

                _terminal = true;
                _source.Dispose();
                completed = true;
            }

            if (completed)
            {
                _terminalCompletion.TrySetResult();
                _pendingCancellation?.CompleteTerminal();
            }
        }

        public Task WaitForTerminalAsync(CancellationToken cancellationToken) =>
            _terminalCompletion.Task.WaitAsync(cancellationToken);

    }

    private sealed class PendingCancellation
    {
        private readonly TaskCompletionSource<bool> _terminalCompletion = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly Timer _expiryTimer;
        private int _boundOrExpired;

        public PendingCancellation(Action expire, TimeSpan lifetime)
        {
            _expiryTimer = new Timer(
                static state => ((Action)state!).Invoke(),
                expire,
                lifetime,
                Timeout.InfiniteTimeSpan);
        }

        public Task<bool> Terminal => _terminalCompletion.Task;

        public void Bind()
        {
            if (Interlocked.CompareExchange(ref _boundOrExpired, 1, 0) == 0)
            {
                _expiryTimer.Dispose();
            }
        }

        public void CompleteTerminal()
        {
            _expiryTimer.Dispose();
            _terminalCompletion.TrySetResult(true);
        }

        public void Expire()
        {
            if (Interlocked.CompareExchange(ref _boundOrExpired, 1, 0) == 0)
            {
                _expiryTimer.Dispose();
                _terminalCompletion.TrySetResult(false);
            }
        }
    }

    private sealed class RequestLease : IDisposable
    {
        private readonly RequestCancellationRegistry _registry;
        private readonly string _requestId;
        private RequestEntry? _entry;

        public RequestLease(RequestCancellationRegistry registry, string requestId, RequestEntry entry)
        {
            _registry = registry;
            _requestId = requestId;
            _entry = entry;
        }

        public CancellationToken Token =>
            _entry?.Token ?? throw new ObjectDisposedException(nameof(RequestLease));

        public void Dispose()
        {
            var entry = Interlocked.Exchange(ref _entry, null);
            if (entry is not null)
            {
                _registry.Complete(_requestId, entry);
            }
        }
    }
}
