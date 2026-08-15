using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CadBridge.Hosting;

/// <summary>The durable lifecycle of one immutable whole-DWG checkpoint.</summary>
public enum DurableCheckpointState
{
    Available,
    Restoring,
    Consumed,
    Quarantined,
    Expired,
}

/// <summary>
/// A checkpoint catalog record. The source path is represented only by a domain-separated hash;
/// callers must never persist the raw customer path alongside this record.
/// </summary>
public sealed record DurableCheckpointRecord(
    int CatalogSchema,
    string CheckpointId,
    string JobId,
    string DocumentId,
    string PreRevision,
    string OriginalPathHash,
    string CheckpointFileName,
    string Sha256,
    long ByteLength,
    string DwgVersion,
    DateTimeOffset CreatedUtc,
    DurableCheckpointState State);

/// <summary>
/// Authenticated, restart-safe catalog for whole-DWG checkpoint artifacts. The catalog never opens
/// a source drawing and has no AutoCAD dependency. Callers stage an immutable DWG inside the root,
/// then register it before any live drawing replacement begins.
/// </summary>
public sealed class DurableCheckpointCatalog : IDisposable
{
    public const int CurrentCatalogSchema = 1;

    private const string CatalogFileName = "checkpoint-catalog.v1.json";
    private const string WatermarkFileName = "checkpoint-catalog.v1.watermark";
    private const int MaximumCatalogBytes = 16 * 1024 * 1024;
    private const int MaximumWatermarkBytes = 16 * 1024;
    private const int MaximumRecords = 100_000;
    private const int MaximumIdentifierLength = 128;
    private const int MaximumRevisionLength = 256;
    private const int MaximumCheckpointFileNameLength = 128;
    private const int MaximumDwgVersionLength = 32;
    private static readonly StringComparison PathComparison = OperatingSystem.IsWindows()
        ? StringComparison.OrdinalIgnoreCase
        : StringComparison.Ordinal;
    private static readonly StringComparer FileNameComparer = OperatingSystem.IsWindows()
        ? StringComparer.OrdinalIgnoreCase
        : StringComparer.Ordinal;
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = false,
        WriteIndented = false,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        MaxDepth = 32,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    private readonly object _gate = new();
    private readonly string _root;
    private readonly string _catalogPath;
    private readonly string _watermarkPath;
    private readonly string _mutexName;
    private readonly byte[] _authenticationKey;
    private long _highestObservedGeneration;
    private bool _disposed;

    /// <summary>
    /// Opens or creates a catalog. The HMAC key is copied and must contain at least 256 bits of
    /// caller-managed entropy. It is never written to the checkpoint root.
    /// </summary>
    public DurableCheckpointCatalog(string root, ReadOnlySpan<byte> authenticationKey)
    {
        if (authenticationKey.Length < 32)
        {
            throw new ArgumentException(
                "The checkpoint catalog authentication key must contain at least 32 bytes.",
                nameof(authenticationKey));
        }

        _root = PrepareRoot(root);
        _catalogPath = ResolvePathUnderRoot(CatalogFileName);
        _watermarkPath = ResolvePathUnderRoot(WatermarkFileName);
        var mutexIdentity = OperatingSystem.IsWindows() ? _root.ToUpperInvariant() : _root;
        _mutexName = "CadHarness.CheckpointCatalog." + Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(mutexIdentity))).ToLowerInvariant();
        _authenticationKey = authenticationKey.ToArray();

        try
        {
            lock (_gate)
            {
                using var processLock = AcquireInterprocessLock();
                var catalogExists = File.Exists(_catalogPath);
                var watermarkExists = File.Exists(_watermarkPath);
                if (catalogExists != watermarkExists)
                {
                    throw new InvalidDataException(
                        "The authenticated checkpoint catalog is incomplete.");
                }

                if (catalogExists)
                {
                    var loaded = LoadAndRecoverUnderLock();
                    _highestObservedGeneration = loaded.Generation;
                }
                else
                {
                    if (Directory.EnumerateFiles(_root, "*.dwg", SearchOption.TopDirectoryOnly).Any())
                    {
                        throw new InvalidDataException(
                            "Checkpoint artifacts exist without an authenticated catalog.");
                    }

                    PersistUnderLock(EmptyCatalog);
                    _highestObservedGeneration = EmptyCatalog.Generation;
                }
            }
        }
        catch
        {
            CryptographicOperations.ZeroMemory(_authenticationKey);
            throw;
        }
    }

    /// <summary>
    /// Computes the opaque path identity used in persisted records. The input must be absolute;
    /// it is normalized only in memory and is never included in an exception or serialized value.
    /// </summary>
    public static string ComputeOriginalPathHash(string originalPath)
    {
        if (string.IsNullOrWhiteSpace(originalPath) ||
            !Path.IsPathFullyQualified(originalPath))
        {
            throw new ArgumentException("The original drawing path must be absolute.", nameof(originalPath));
        }

        string normalized;
        try
        {
            normalized = Path.TrimEndingDirectorySeparator(Path.GetFullPath(originalPath))
                .Normalize(NormalizationForm.FormC);
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException)
        {
            throw new ArgumentException(
                "The original drawing path is invalid.",
                nameof(originalPath));
        }

        if (OperatingSystem.IsWindows())
        {
            normalized = normalized.ToUpperInvariant();
        }

        var identity = "cad-harness-original-path-v1\0" + normalized;
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity)))
            .ToLowerInvariant();
    }

    /// <summary>
    /// Registers an already-staged DWG as Available. Checksum, length, and DWG version are derived
    /// from one read of the file. Checkpoint ids and filenames are single-assignment: every duplicate
    /// registration fails, including an otherwise identical replay.
    /// </summary>
    public DurableCheckpointRecord RegisterCheckpoint(
        string checkpointId,
        string jobId,
        string documentId,
        string preRevision,
        string originalPath,
        string checkpointFileName,
        DateTimeOffset createdUtc)
    {
        ValidateIdentifier(checkpointId, nameof(checkpointId));
        ValidateIdentifier(jobId, nameof(jobId));
        ValidateIdentifier(documentId, nameof(documentId));
        ValidateRevision(preRevision);
        ValidateCheckpointFileName(checkpointFileName);
        if (createdUtc.Offset != TimeSpan.Zero)
        {
            throw new ArgumentException("The checkpoint creation time must be UTC.", nameof(createdUtc));
        }

        var originalPathHash = ComputeOriginalPathHash(originalPath);

        return WithFreshCatalog(payload =>
        {
            if (payload.Records.Any(record =>
                    string.Equals(record.CheckpointId, checkpointId, StringComparison.Ordinal)))
            {
                throw new InvalidOperationException("The checkpoint id is already registered.");
            }

            if (payload.Records.Any(record => FileNameComparer.Equals(
                    record.CheckpointFileName,
                    checkpointFileName)))
            {
                throw new InvalidOperationException(
                    "The checkpoint filename is already registered.");
            }

            var facts = ReadArtifactFacts(checkpointFileName);
            var record = new DurableCheckpointRecord(
                CurrentCatalogSchema,
                checkpointId,
                jobId,
                documentId,
                preRevision,
                originalPathHash,
                checkpointFileName,
                facts.Sha256,
                facts.ByteLength,
                facts.DwgVersion,
                createdUtc,
                DurableCheckpointState.Available);
            var records = payload.Records.ToList();
            records.Add(record);
            var replacement = payload with
            {
                Generation = checked(payload.Generation + 1),
                Records = records,
            };
            return MutationResult<DurableCheckpointRecord>.Persist(record, replacement);
        });
    }

    /// <summary>Returns one record after refreshing and validating active artifacts.</summary>
    public DurableCheckpointRecord GetRequired(string checkpointId)
    {
        ValidateIdentifier(checkpointId, nameof(checkpointId));
        return WithFreshCatalog(payload => MutationResult<DurableCheckpointRecord>.NoWrite(
            RequireRecord(payload, checkpointId),
            payload));
    }

    /// <summary>Returns an ordered immutable snapshot after refreshing active artifacts.</summary>
    public IReadOnlyList<DurableCheckpointRecord> Snapshot() =>
        WithFreshCatalog(payload => MutationResult<IReadOnlyList<DurableCheckpointRecord>>.NoWrite(
            (IReadOnlyList<DurableCheckpointRecord>)payload.Records
                .OrderBy(record => record.CheckpointId, StringComparer.Ordinal)
                .ToArray(),
            payload));

    /// <summary>
    /// Changes Available to Restoring before document replacement. Replaying BeginRestore while the
    /// record is already Restoring is idempotent and performs no write; terminal states fail closed.
    /// A valid Restoring record survives restart but is never automatically replayed by this catalog.
    /// </summary>
    public DurableCheckpointRecord BeginRestore(string checkpointId) =>
        Transition(
            checkpointId,
            DurableCheckpointState.Available,
            DurableCheckpointState.Restoring,
            DurableCheckpointState.Restoring,
            "Only an available checkpoint can begin restoration.");

    /// <summary>
    /// Changes Restoring to Consumed only after the host proves replacement completed. Replaying
    /// Complete for an already Consumed record is idempotent and performs no write.
    /// </summary>
    public DurableCheckpointRecord Complete(string checkpointId) =>
        Transition(
            checkpointId,
            DurableCheckpointState.Restoring,
            DurableCheckpointState.Consumed,
            DurableCheckpointState.Consumed,
            "Only a restoring checkpoint can be completed.");

    /// <summary>
    /// Changes Restoring back to Available only when the host proves replacement never began.
    /// Replaying cancellation while Available is idempotent; it can never undo Consumed.
    /// </summary>
    public DurableCheckpointRecord CancelBeforeReplacement(string checkpointId) =>
        Transition(
            checkpointId,
            DurableCheckpointState.Restoring,
            DurableCheckpointState.Available,
            DurableCheckpointState.Available,
            "Only a restoring checkpoint can be cancelled before replacement.");

    /// <summary>
    /// Irreversibly removes an Available or Restoring checkpoint from use. Replaying Quarantine is
    /// idempotent; Consumed and Expired remain terminal and cannot be rewritten.
    /// </summary>
    public DurableCheckpointRecord Quarantine(string checkpointId) =>
        WithFreshCatalog(payload =>
        {
            var current = RequireRecord(payload, checkpointId);
            if (current.State == DurableCheckpointState.Quarantined)
            {
                return MutationResult<DurableCheckpointRecord>.NoWrite(current, payload);
            }

            if (current.State is not (DurableCheckpointState.Available or
                DurableCheckpointState.Restoring))
            {
                throw new InvalidOperationException(
                    "Only an active checkpoint can be quarantined.");
            }

            return ReplaceState(payload, current, DurableCheckpointState.Quarantined);
        });

    /// <summary>
    /// Marks an unused Available checkpoint Expired. Replaying expiration is idempotent; restoration
    /// and terminal records cannot be expired.
    /// </summary>
    public DurableCheckpointRecord Expire(string checkpointId) =>
        Transition(
            checkpointId,
            DurableCheckpointState.Available,
            DurableCheckpointState.Expired,
            DurableCheckpointState.Expired,
            "Only an available checkpoint can expire.");

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed)
            {
                return;
            }

            CryptographicOperations.ZeroMemory(_authenticationKey);
            _disposed = true;
        }

        GC.SuppressFinalize(this);
    }

    private static readonly CatalogPayload EmptyCatalog = new(
        CurrentCatalogSchema,
        Generation: 0,
        Records: []);

    private DurableCheckpointRecord Transition(
        string checkpointId,
        DurableCheckpointState requiredState,
        DurableCheckpointState nextState,
        DurableCheckpointState idempotentState,
        string failureMessage)
    {
        ValidateIdentifier(checkpointId, nameof(checkpointId));
        return WithFreshCatalog(payload =>
        {
            var current = RequireRecord(payload, checkpointId);
            if (current.State == idempotentState)
            {
                return MutationResult<DurableCheckpointRecord>.NoWrite(current, payload);
            }

            if (current.State != requiredState)
            {
                throw new InvalidOperationException(failureMessage);
            }

            return ReplaceState(payload, current, nextState);
        });
    }

    private static MutationResult<DurableCheckpointRecord> ReplaceState(
        CatalogPayload payload,
        DurableCheckpointRecord current,
        DurableCheckpointState nextState)
    {
        var replacementRecord = current with { State = nextState };
        var records = payload.Records
            .Select(record => string.Equals(
                    record.CheckpointId,
                    current.CheckpointId,
                    StringComparison.Ordinal)
                ? replacementRecord
                : record)
            .ToList();
        var replacementCatalog = payload with
        {
            Generation = checked(payload.Generation + 1),
            Records = records,
        };
        return MutationResult<DurableCheckpointRecord>.Persist(
            replacementRecord,
            replacementCatalog);
    }

    private T WithFreshCatalog<T>(Func<CatalogPayload, MutationResult<T>> action)
    {
        lock (_gate)
        {
            ThrowIfDisposed();
            EnsureRootStillSafe();
            using var processLock = AcquireInterprocessLock();
            var refreshed = LoadAndRecoverUnderLock();
            var mutation = action(refreshed);
            if (mutation.ShouldPersist)
            {
                PersistUnderLock(mutation.Catalog);
            }

            _highestObservedGeneration = Math.Max(
                _highestObservedGeneration,
                mutation.Catalog.Generation);
            return mutation.Value;
        }
    }

    private CatalogPayload LoadAndRecoverUnderLock()
    {
        if (!File.Exists(_catalogPath))
        {
            throw new InvalidDataException("The authenticated checkpoint catalog is missing.");
        }

        var authenticatedCatalog = ReadCatalogFile();
        ValidateWatermark(authenticatedCatalog);
        var payload = authenticatedCatalog.Payload;
        if (payload.Generation < _highestObservedGeneration)
        {
            throw new InvalidDataException("A checkpoint catalog replay was detected.");
        }

        ValidateCatalogStructure(payload);
        var recovered = false;
        var records = new List<DurableCheckpointRecord>(payload.Records.Count);
        foreach (var record in payload.Records)
        {
            if ((record.State is DurableCheckpointState.Available or
                    DurableCheckpointState.Restoring) &&
                !ArtifactMatches(record))
            {
                records.Add(record with { State = DurableCheckpointState.Quarantined });
                recovered = true;
            }
            else
            {
                records.Add(record);
            }
        }

        if (!recovered)
        {
            _highestObservedGeneration = Math.Max(_highestObservedGeneration, payload.Generation);
            return payload;
        }

        var replacement = payload with
        {
            Generation = checked(payload.Generation + 1),
            Records = records,
        };
        PersistUnderLock(replacement);
        _highestObservedGeneration = Math.Max(
            _highestObservedGeneration,
            replacement.Generation);
        return replacement;
    }

    private AuthenticatedCatalog ReadCatalogFile()
    {
        RejectReparsePoint(_catalogPath, "The checkpoint catalog must not be a reparse point.");
        var information = new FileInfo(_catalogPath);
        if (!information.Exists ||
            (information.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0 ||
            information.Length is <= 0 or > MaximumCatalogBytes)
        {
            throw new InvalidDataException("The checkpoint catalog file is invalid.");
        }

        byte[] bytes;
        try
        {
            using var stream = new FileStream(
                _catalogPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.SequentialScan);
            if (stream.Length is <= 0 or > MaximumCatalogBytes)
            {
                throw new InvalidDataException("The checkpoint catalog size is invalid.");
            }

            bytes = new byte[checked((int)stream.Length)];
            stream.ReadExactly(bytes);
        }
        catch (EndOfStreamException exception)
        {
            throw new InvalidDataException("The checkpoint catalog is truncated.", exception);
        }

        CatalogEnvelope envelope;
        try
        {
            using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 32,
            });
            RejectDuplicateJsonProperties(document.RootElement);
            envelope = JsonSerializer.Deserialize<CatalogEnvelope>(bytes, SerializerOptions)
                ?? throw new InvalidDataException("The checkpoint catalog is empty.");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("The checkpoint catalog is malformed.", exception);
        }

        if (envelope.Payload is null || !IsLowerHex(envelope.AuthenticationTag, 64))
        {
            throw new InvalidDataException("The checkpoint catalog envelope is invalid.");
        }

        var canonicalPayload = JsonSerializer.SerializeToUtf8Bytes(
            envelope.Payload,
            SerializerOptions);
        var expected = ComputeAuthenticationTag("checkpoint-catalog-v1", canonicalPayload);
        var supplied = Convert.FromHexString(envelope.AuthenticationTag);
        if (!CryptographicOperations.FixedTimeEquals(expected, supplied))
        {
            throw new InvalidDataException("The checkpoint catalog failed authentication.");
        }

        return new AuthenticatedCatalog(
            envelope.Payload,
            Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant());
    }

    private void PersistUnderLock(CatalogPayload payload)
    {
        ValidateCatalogStructure(payload);
        var canonicalPayload = JsonSerializer.SerializeToUtf8Bytes(payload, SerializerOptions);
        var authenticationTag = Convert.ToHexString(ComputeAuthenticationTag(
            "checkpoint-catalog-v1",
            canonicalPayload)).ToLowerInvariant();
        var envelopeBytes = JsonSerializer.SerializeToUtf8Bytes(
            new CatalogEnvelope(payload, authenticationTag),
            SerializerOptions);
        if (envelopeBytes.Length is <= 0 or > MaximumCatalogBytes)
        {
            throw new InvalidDataException("The checkpoint catalog size is invalid.");
        }

        WriteDurableAtomic(_catalogPath, CatalogFileName, envelopeBytes);

        var catalogEnvelopeSha256 = Convert.ToHexString(SHA256.HashData(envelopeBytes))
            .ToLowerInvariant();
        var watermarkPayload = new WatermarkPayload(
            CurrentCatalogSchema,
            payload.Generation,
            catalogEnvelopeSha256);
        var canonicalWatermark = JsonSerializer.SerializeToUtf8Bytes(
            watermarkPayload,
            SerializerOptions);
        var watermarkTag = Convert.ToHexString(ComputeAuthenticationTag(
            "checkpoint-catalog-watermark-v1",
            canonicalWatermark)).ToLowerInvariant();
        var watermarkBytes = JsonSerializer.SerializeToUtf8Bytes(
            new WatermarkEnvelope(watermarkPayload, watermarkTag),
            SerializerOptions);
        if (watermarkBytes.Length is <= 0 or > MaximumWatermarkBytes)
        {
            throw new InvalidDataException("The checkpoint catalog watermark size is invalid.");
        }

        WriteDurableAtomic(_watermarkPath, WatermarkFileName, watermarkBytes);

        var readBack = ReadCatalogFile();
        ValidateWatermark(readBack);
        var readBackBytes = JsonSerializer.SerializeToUtf8Bytes(
            readBack.Payload,
            SerializerOptions);
        if (!canonicalPayload.AsSpan().SequenceEqual(readBackBytes))
        {
            throw new IOException("The checkpoint catalog write could not be verified.");
        }
    }

    private void WriteDurableAtomic(string target, string targetFileName, byte[] payload)
    {
        var temporary = ResolvePathUnderRoot(
            $".{targetFileName}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(
                       temporary,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       bufferSize: 4096,
                       FileOptions.WriteThrough))
            {
                stream.Write(payload);
                stream.Flush(flushToDisk: true);
            }

            if (File.Exists(target))
            {
                RejectReparsePoint(
                    target,
                    "A checkpoint catalog metadata file must not be a reparse point.");
                File.Replace(temporary, target, destinationBackupFileName: null);
            }
            else
            {
                File.Move(temporary, target);
            }

            using var committed = new FileStream(
                target,
                FileMode.Open,
                FileAccess.ReadWrite,
                FileShare.Read,
                bufferSize: 1,
                FileOptions.WriteThrough);
            committed.Flush(flushToDisk: true);
        }
        finally
        {
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
        }
    }

    private void ValidateWatermark(AuthenticatedCatalog catalog)
    {
        RejectReparsePoint(
            _watermarkPath,
            "The checkpoint catalog watermark must not be a reparse point.");
        var information = new FileInfo(_watermarkPath);
        if (!information.Exists ||
            (information.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0 ||
            information.Length is <= 0 or > MaximumWatermarkBytes)
        {
            throw new InvalidDataException("The checkpoint catalog watermark is invalid.");
        }

        byte[] bytes;
        using (var stream = new FileStream(
                   _watermarkPath,
                   FileMode.Open,
                   FileAccess.Read,
                   FileShare.Read,
                   bufferSize: 4096,
                   FileOptions.SequentialScan))
        {
            if (stream.Length is <= 0 or > MaximumWatermarkBytes)
            {
                throw new InvalidDataException("The checkpoint catalog watermark size is invalid.");
            }

            bytes = new byte[checked((int)stream.Length)];
            stream.ReadExactly(bytes);
        }

        WatermarkEnvelope envelope;
        try
        {
            using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 8,
            });
            RejectDuplicateJsonProperties(document.RootElement);
            envelope = JsonSerializer.Deserialize<WatermarkEnvelope>(bytes, SerializerOptions)
                ?? throw new InvalidDataException("The checkpoint catalog watermark is empty.");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException(
                "The checkpoint catalog watermark is malformed.",
                exception);
        }

        if (envelope.Payload is null || !IsLowerHex(envelope.AuthenticationTag, 64))
        {
            throw new InvalidDataException("The checkpoint catalog watermark envelope is invalid.");
        }

        var canonicalPayload = JsonSerializer.SerializeToUtf8Bytes(
            envelope.Payload,
            SerializerOptions);
        var expected = ComputeAuthenticationTag(
            "checkpoint-catalog-watermark-v1",
            canonicalPayload);
        var supplied = Convert.FromHexString(envelope.AuthenticationTag);
        if (!CryptographicOperations.FixedTimeEquals(expected, supplied) ||
            envelope.Payload.CatalogSchema != CurrentCatalogSchema ||
            envelope.Payload.Generation != catalog.Payload.Generation ||
            !string.Equals(
                envelope.Payload.CatalogEnvelopeSha256,
                catalog.EnvelopeSha256,
                StringComparison.Ordinal))
        {
            throw new InvalidDataException(
                "The checkpoint catalog watermark does not match the catalog.");
        }
    }

    private byte[] ComputeAuthenticationTag(string domain, ReadOnlySpan<byte> payload)
    {
        var domainBytes = Encoding.ASCII.GetBytes(domain);
        var authenticated = new byte[checked(domainBytes.Length + 1 + payload.Length)];
        domainBytes.CopyTo(authenticated, 0);
        payload.CopyTo(authenticated.AsSpan(domainBytes.Length + 1));
        return HMACSHA256.HashData(_authenticationKey, authenticated);
    }

    private ArtifactFacts ReadArtifactFacts(string checkpointFileName)
    {
        var path = ResolveCheckpointPath(checkpointFileName);
        RejectReparsePoint(path, "A checkpoint artifact must not be a reparse point.");
        if (!File.Exists(path))
        {
            throw new FileNotFoundException("The staged checkpoint artifact is missing.");
        }

        var attributes = File.GetAttributes(path);
        if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
        {
            throw new InvalidDataException("The checkpoint artifact is not a regular local file.");
        }

        using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.SequentialScan);
        if (stream.Length <= 6)
        {
            throw new InvalidDataException("The checkpoint artifact is not a valid DWG file.");
        }

        Span<byte> header = stackalloc byte[6];
        stream.ReadExactly(header);
        var dwgVersion = Encoding.ASCII.GetString(header);
        if (!IsDwgVersion(dwgVersion))
        {
            throw new InvalidDataException("The checkpoint artifact has an invalid DWG version header.");
        }

        stream.Position = 0;
        var sha256 = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        RejectReparsePoint(path, "A checkpoint artifact must not be a reparse point.");
        return new ArtifactFacts(sha256, stream.Length, dwgVersion);
    }

    private bool ArtifactMatches(DurableCheckpointRecord record)
    {
        try
        {
            var facts = ReadArtifactFacts(record.CheckpointFileName);
            return facts.ByteLength == record.ByteLength &&
                string.Equals(facts.Sha256, record.Sha256, StringComparison.Ordinal) &&
                string.Equals(facts.DwgVersion, record.DwgVersion, StringComparison.Ordinal);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            return false;
        }
    }

    private static void ValidateCatalogStructure(CatalogPayload payload)
    {
        if (payload.CatalogSchema != CurrentCatalogSchema ||
            payload.Generation < 0 ||
            payload.Records is null ||
            payload.Records.Count > MaximumRecords)
        {
            throw new InvalidDataException("The checkpoint catalog schema is invalid.");
        }

        var checkpointIds = new HashSet<string>(StringComparer.Ordinal);
        var checkpointFileNames = new HashSet<string>(FileNameComparer);
        foreach (var record in payload.Records)
        {
            ValidateLoadedRecord(record);
            if (!checkpointIds.Add(record.CheckpointId) ||
                !checkpointFileNames.Add(record.CheckpointFileName))
            {
                throw new InvalidDataException(
                    "The checkpoint catalog contains a duplicate record.");
            }
        }
    }

    private static void ValidateLoadedRecord(DurableCheckpointRecord record)
    {
        try
        {
            if (record.CatalogSchema != CurrentCatalogSchema ||
                !IsLowerHex(record.OriginalPathHash, 64) ||
                !IsLowerHex(record.Sha256, 64) ||
                record.ByteLength <= 6 ||
                !IsDwgVersion(record.DwgVersion) ||
                record.CreatedUtc.Offset != TimeSpan.Zero ||
                !Enum.IsDefined(record.State))
            {
                throw new InvalidDataException("The checkpoint record failed validation.");
            }

            ValidateIdentifier(record.CheckpointId, nameof(record.CheckpointId));
            ValidateIdentifier(record.JobId, nameof(record.JobId));
            ValidateIdentifier(record.DocumentId, nameof(record.DocumentId));
            ValidateRevision(record.PreRevision);
            ValidateCheckpointFileName(record.CheckpointFileName);
        }
        catch (ArgumentException exception)
        {
            throw new InvalidDataException("The checkpoint record failed validation.", exception);
        }
    }

    private static DurableCheckpointRecord RequireRecord(
        CatalogPayload payload,
        string checkpointId) =>
        payload.Records.SingleOrDefault(record => string.Equals(
            record.CheckpointId,
            checkpointId,
            StringComparison.Ordinal))
        ?? throw new KeyNotFoundException("The checkpoint id is not registered.");

    private static void ValidateIdentifier(string value, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(value) ||
            value.Length > MaximumIdentifierLength ||
            !IsSafeIdentifierCharacter(value[0]) ||
            value.Any(character => !IsSafeIdentifierCharacter(character)))
        {
            throw new ArgumentException("The checkpoint identity is invalid.", parameterName);
        }
    }

    private static void ValidateRevision(string preRevision)
    {
        if (string.IsNullOrWhiteSpace(preRevision) ||
            preRevision.Length > MaximumRevisionLength ||
            preRevision.Any(character => character is < '!' or > '~'))
        {
            throw new ArgumentException("The checkpoint revision is invalid.", nameof(preRevision));
        }
    }

    private static void ValidateCheckpointFileName(string checkpointFileName)
    {
        if (string.IsNullOrWhiteSpace(checkpointFileName) ||
            checkpointFileName.Length > MaximumCheckpointFileNameLength ||
            Path.IsPathFullyQualified(checkpointFileName) ||
            !string.Equals(
                Path.GetFileName(checkpointFileName),
                checkpointFileName,
                StringComparison.Ordinal) ||
            !checkpointFileName.EndsWith(".dwg", StringComparison.OrdinalIgnoreCase) ||
            !IsSafeIdentifierCharacter(checkpointFileName[0]) ||
            checkpointFileName.Any(character => !IsSafeFileNameCharacter(character)))
        {
            throw new ArgumentException(
                "The checkpoint filename is not allowlisted.",
                nameof(checkpointFileName));
        }
    }

    private static bool IsSafeIdentifierCharacter(char character) =>
        character is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or
            '_' or '-' or '.' or ':';

    private static bool IsSafeFileNameCharacter(char character) =>
        character is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or
            '_' or '-' or '.';

    private static bool IsDwgVersion(string value) =>
        value.Length <= MaximumDwgVersionLength &&
        value.Length == 6 &&
        value[0] == 'A' &&
        value[1] == 'C' &&
        value[2..].All(character => character is >= '0' and <= '9');

    private static bool IsLowerHex(string value, int expectedLength) =>
        value is not null &&
        value.Length == expectedLength &&
        value.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');

    private string ResolveCheckpointPath(string checkpointFileName)
    {
        ValidateCheckpointFileName(checkpointFileName);
        return ResolvePathUnderRoot(checkpointFileName);
    }

    private string ResolvePathUnderRoot(string fileName)
    {
        var path = Path.GetFullPath(Path.Combine(_root, fileName));
        if (!string.Equals(Path.GetDirectoryName(path), _root, PathComparison))
        {
            throw new InvalidOperationException("The checkpoint path escaped its configured root.");
        }

        return path;
    }

    private static string PrepareRoot(string root)
    {
        if (string.IsNullOrWhiteSpace(root) ||
            !Path.IsPathFullyQualified(root) ||
            IsNetworkOrDevicePath(root))
        {
            throw new ArgumentException(
                "The checkpoint root must be an absolute local path.",
                nameof(root));
        }

        string fullPath;
        try
        {
            fullPath = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException)
        {
            throw new ArgumentException("The checkpoint root is invalid.", nameof(root));
        }

        if (IsNetworkOrDevicePath(fullPath) || IsNetworkDrive(fullPath))
        {
            throw new ArgumentException(
                "The checkpoint root must be an absolute local path.",
                nameof(root));
        }

        RejectExistingReparseComponents(fullPath);
        CommitJournalSecurity.PreparePrivateRoot(fullPath);
        RejectExistingReparseComponents(fullPath);
        return fullPath;
    }

    private void EnsureRootStillSafe()
    {
        if (!Directory.Exists(_root) || IsNetworkDrive(_root))
        {
            throw new InvalidDataException("The checkpoint root is no longer a local directory.");
        }

        RejectExistingReparseComponents(_root);
    }

    private static bool IsNetworkOrDevicePath(string path) =>
        path.StartsWith("\\\\", StringComparison.Ordinal) ||
        path.StartsWith("//", StringComparison.Ordinal) ||
        path.StartsWith("\\??\\", StringComparison.Ordinal);

    private static bool IsNetworkDrive(string path)
    {
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        var root = Path.GetPathRoot(path);
        return !string.IsNullOrEmpty(root) && new DriveInfo(root).DriveType == DriveType.Network;
    }

    private static void RejectExistingReparseComponents(string path)
    {
        DirectoryInfo? current = new(path);
        while (current is not null)
        {
            if (current.Exists &&
                (current.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException(
                    "The checkpoint root must not contain reparse-point components.");
            }

            current = current.Parent;
        }
    }

    private static void RejectReparsePoint(string path, string message)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException(message);
        }
    }

    private IDisposable AcquireInterprocessLock()
    {
        var mutex = new Mutex(initiallyOwned: false, _mutexName);
        var acquired = false;
        try
        {
            try
            {
                acquired = mutex.WaitOne(TimeSpan.FromSeconds(10));
            }
            catch (AbandonedMutexException)
            {
                acquired = true;
            }

            if (!acquired)
            {
                throw new IOException("The checkpoint catalog lock could not be acquired.");
            }

            return new MutexLease(mutex);
        }
        catch
        {
            if (acquired)
            {
                mutex.ReleaseMutex();
            }

            mutex.Dispose();
            throw;
        }
    }

    private static void RejectDuplicateJsonProperties(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                if (!names.Add(property.Name))
                {
                    throw new JsonException("Duplicate JSON properties are not allowed.");
                }

                RejectDuplicateJsonProperties(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                RejectDuplicateJsonProperties(item);
            }
        }
    }

    private void ThrowIfDisposed() => ObjectDisposedException.ThrowIf(_disposed, this);

    private sealed record ArtifactFacts(string Sha256, long ByteLength, string DwgVersion);

    private sealed record CatalogPayload(
        int CatalogSchema,
        long Generation,
        List<DurableCheckpointRecord> Records);

    private sealed record CatalogEnvelope(
        CatalogPayload Payload,
        string AuthenticationTag);

    private sealed record AuthenticatedCatalog(
        CatalogPayload Payload,
        string EnvelopeSha256);

    private sealed record WatermarkPayload(
        int CatalogSchema,
        long Generation,
        string CatalogEnvelopeSha256);

    private sealed record WatermarkEnvelope(
        WatermarkPayload Payload,
        string AuthenticationTag);

    private sealed record MutationResult<T>(
        T Value,
        CatalogPayload Catalog,
        bool ShouldPersist)
    {
        public static MutationResult<T> Persist(T value, CatalogPayload catalog) =>
            new(value, catalog, ShouldPersist: true);

        public static MutationResult<T> NoWrite(T value, CatalogPayload catalog) =>
            new(value, catalog, ShouldPersist: false);
    }

    private sealed class MutexLease : IDisposable
    {
        private Mutex? _mutex;

        public MutexLease(Mutex mutex) => _mutex = mutex;

        public void Dispose()
        {
            var mutex = Interlocked.Exchange(ref _mutex, null);
            if (mutex is null)
            {
                return;
            }

            mutex.ReleaseMutex();
            mutex.Dispose();
        }
    }
}
