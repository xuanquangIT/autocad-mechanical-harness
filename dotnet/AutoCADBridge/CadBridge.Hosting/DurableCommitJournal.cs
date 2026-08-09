using System.Security.Cryptography;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace CadBridge.Hosting;

/// <summary>The durable state of one bridge commit attempt.</summary>
public enum CommitJournalState
{
    Prepared,
    Committed,
    Unknown,
}

/// <summary>The action a host must take after reserving an idempotency key.</summary>
public enum CommitJournalDecisionKind
{
    Execute,
    ReplayCommitted,
    Unknown,
    IdempotencyKeyReused,
}

/// <summary>A closed journal decision. Only a committed replay carries a result.</summary>
public sealed record CommitJournalDecision(
    CommitJournalDecisionKind Kind,
    BridgeHostResult? Result = null,
    string? ReservationId = null);

public sealed record CommitJournalProcessIdentity(int ProcessId, long StartTimeUtcTicks);

public enum CommitJournalProcessLiveness
{
    Alive,
    Dead,
    Unknown,
}

public interface ICommitJournalProcessProbe
{
    CommitJournalProcessIdentity Current { get; }

    CommitJournalProcessLiveness GetLiveness(CommitJournalProcessIdentity identity);
}

/// <summary>
/// Restart-safe idempotency journal for irreversible bridge commits. Entries contain only hashed
/// identifiers, a request digest, state, and (after commit) the receipt needed for replay. Plans,
/// approval tokens, source paths, and exception details are never persisted.
/// </summary>
public sealed class DurableCommitJournal
{
    private const int JournalVersion = 1;
    private const int MaximumJournalBytes = 4 * 1024 * 1024;
    private const int MaximumJournalEntries = 100_000;
    private readonly object _gate = new();
    private readonly string _root;
    private readonly string _mutexName;
    private readonly ICommitJournalProcessProbe _processProbe;
    private readonly byte[] _integrityKey;
    private readonly Dictionary<string, JournalRecord> _records = new(StringComparer.Ordinal);

    public DurableCommitJournal(string root, ICommitJournalProcessProbe? processProbe = null)
    {
        if (string.IsNullOrWhiteSpace(root) || !Path.IsPathFullyQualified(root))
        {
            throw new ArgumentException(
                "The commit journal root must be an absolute local path.",
                nameof(root));
        }

        _root = Path.GetFullPath(root);
        if (_root.StartsWith("\\\\", StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "The commit journal root must not be a network or device path.",
                nameof(root));
        }

        CommitJournalSecurity.PreparePrivateRoot(_root);
        _processProbe = processProbe ?? new SystemCommitJournalProcessProbe();
        _mutexName = "CadHarness.CommitJournal." + Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(_root.ToUpperInvariant()))).ToLowerInvariant();
        using var processLock = AcquireInterprocessLock();
        _integrityKey = CommitJournalSecurity.LoadOrCreateIntegrityKey(
            _root,
            Directory.EnumerateFiles(_root, "*.json", SearchOption.TopDirectoryOnly).Any());
        LoadAndRecover();
    }

    /// <summary>
    /// Durably reserves a (job id, idempotency key, digest) tuple before an AutoCAD write starts.
    /// </summary>
    public CommitJournalDecision Begin(string jobId, string idempotencyKey, string digest)
    {
        ValidateIdentity(jobId, idempotencyKey, digest);
        var keyHash = ComputeKeyHash(jobId, idempotencyKey);
        lock (_gate)
        {
            using var processLock = AcquireInterprocessLock();
            RefreshRecord(keyHash);
            if (_records.TryGetValue(keyHash, out var existing))
            {
                if (!string.Equals(existing.Digest, digest, StringComparison.Ordinal))
                {
                    return new CommitJournalDecision(
                        CommitJournalDecisionKind.IdempotencyKeyReused);
                }

                if (existing.State == CommitJournalState.Prepared &&
                    IsOwnerProvenDead(existing))
                {
                    existing = RecoverAsUnknown(existing);
                }

                return existing.State switch
                {
                    CommitJournalState.Committed when existing.ResultData is not null =>
                        new CommitJournalDecision(
                            CommitJournalDecisionKind.ReplayCommitted,
                            BridgeHostResult.Success(RestoreResultData(
                                existing.ResultData,
                                jobId))),
                    CommitJournalState.Prepared or CommitJournalState.Unknown =>
                        new CommitJournalDecision(CommitJournalDecisionKind.Unknown),
                    _ => throw new InvalidDataException(
                        "The commit journal contains an invalid terminal record."),
                };
            }

            var reservationId = Guid.NewGuid().ToString("N");
            var owner = _processProbe.Current;
            var prepared = new JournalRecord(
                JournalVersion,
                keyHash,
                digest,
                CommitJournalState.Prepared,
                owner.ProcessId,
                owner.StartTimeUtcTicks,
                reservationId,
                ResultData: null);
            Persist(prepared);
            _records.Add(keyHash, prepared);
            return new CommitJournalDecision(
                CommitJournalDecisionKind.Execute,
                ReservationId: reservationId);
        }
    }

    /// <summary>Persists the exact successful receipt before it may be returned to the client.</summary>
    public void MarkCommitted(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId,
        BridgeHostResult result)
    {
        ArgumentNullException.ThrowIfNull(result);
        if (result.Outcome != BridgeHostOutcome.Ok || result.Data is null)
        {
            throw new ArgumentException(
                "Only a successful commit receipt can be journaled as committed.",
                nameof(result));
        }

        Transition(
            jobId,
            idempotencyKey,
            digest,
            reservationId,
            CommitJournalState.Committed,
            CloneResult(result));
    }

    /// <summary>Durably prevents a retry when the irreversible commit outcome cannot be proven.</summary>
    public void MarkUnknown(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId) =>
        Transition(
            jobId,
            idempotencyKey,
            digest,
            reservationId,
            CommitJournalState.Unknown,
            result: null);

    /// <summary>
    /// Removes a prepared attempt only after the executor has proved that no commit occurred.
    /// A crash before removal conservatively recovers the attempt as unknown.
    /// </summary>
    public void Abandon(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId)
    {
        ValidateIdentity(jobId, idempotencyKey, digest);
        var keyHash = ComputeKeyHash(jobId, idempotencyKey);
        lock (_gate)
        {
            using var processLock = AcquireInterprocessLock();
            RefreshRecord(keyHash);
            var existing = RequireMatchingRecord(keyHash, digest);
            if (existing.State != CommitJournalState.Prepared ||
                !IsOwnedReservation(existing, reservationId))
            {
                throw new InvalidOperationException(
                    "Only the owning process can abandon its prepared reservation.");
            }

            File.Delete(GetEntryPath(keyHash));
            _records.Remove(keyHash);
        }
    }

    private void Transition(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId,
        CommitJournalState state,
        BridgeHostResult? result)
    {
        ValidateIdentity(jobId, idempotencyKey, digest);
        var keyHash = ComputeKeyHash(jobId, idempotencyKey);
        lock (_gate)
        {
            using var processLock = AcquireInterprocessLock();
            RefreshRecord(keyHash);
            var existing = RequireMatchingRecord(keyHash, digest);
            if (existing.State != CommitJournalState.Prepared ||
                !IsOwnedReservation(existing, reservationId))
            {
                throw new InvalidOperationException(
                    "Only the owning process can complete its prepared reservation.");
            }

            var replacement = existing with
            {
                State = state,
                ResultData = result is null ? null : SanitizeResultData(result),
            };
            Persist(replacement);
            _records[keyHash] = replacement;
        }
    }

    private JournalRecord RequireMatchingRecord(string keyHash, string digest)
    {
        if (!_records.TryGetValue(keyHash, out var existing))
        {
            throw new InvalidOperationException("The commit was not prepared in the journal.");
        }

        if (!string.Equals(existing.Digest, digest, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "The idempotency key was prepared for another request digest.");
        }

        return existing;
    }

    private void LoadAndRecover()
    {
        var paths = Directory.EnumerateFiles(_root, "*.json", SearchOption.TopDirectoryOnly)
            .Order(StringComparer.Ordinal)
            .Take(MaximumJournalEntries + 1)
            .ToArray();
        if (paths.Length > MaximumJournalEntries)
        {
            throw new InvalidDataException("The commit journal entry limit was exceeded.");
        }

        foreach (var path in paths)
        {
            var record = ReadRecord(path);
            if (!_records.TryAdd(record.KeyHash, record))
            {
                throw new InvalidDataException("The commit journal contains a duplicate entry.");
            }
        }

        // A live owner may still safely abandon a pre-commit failure. Only a positively dead owner
        // turns prepared into unknown; access-denied/uncertain liveness remains prepared/fail-closed.
        foreach (var prepared in _records.Values
                     .Where(record => record.State == CommitJournalState.Prepared &&
                         IsOwnerProvenDead(record))
                     .ToArray())
        {
            RecoverAsUnknown(prepared);
        }
    }

    private JournalRecord RecoverAsUnknown(JournalRecord prepared)
    {
        var unknown = prepared with
        {
            State = CommitJournalState.Unknown,
            ResultData = null,
        };
        Persist(unknown);
        _records[unknown.KeyHash] = unknown;
        return unknown;
    }

    private bool IsOwnerProvenDead(JournalRecord record) =>
        _processProbe.GetLiveness(new CommitJournalProcessIdentity(
            record.OwnerProcessId,
            record.OwnerStartTimeUtcTicks)) == CommitJournalProcessLiveness.Dead;

    private bool IsOwnedReservation(JournalRecord record, string reservationId)
    {
        var owner = _processProbe.Current;
        return string.Equals(record.ReservationId, reservationId, StringComparison.Ordinal) &&
            record.OwnerProcessId == owner.ProcessId &&
            record.OwnerStartTimeUtcTicks == owner.StartTimeUtcTicks;
    }

    private void RefreshRecord(string keyHash)
    {
        var path = GetEntryPath(keyHash);
        if (!File.Exists(path))
        {
            _records.Remove(keyHash);
            return;
        }

        _records[keyHash] = ReadRecord(path);
    }

    private JournalRecord ReadRecord(string path)
    {
        CommitJournalSecurity.RejectReparsePoint(path);
        var fileName = Path.GetFileNameWithoutExtension(path);
        if (!IsLowerHex(fileName, 64))
        {
            throw new InvalidDataException("The commit journal contains an unsafe entry name.");
        }

        var length = new FileInfo(path).Length;
        if (length is <= 0 or > MaximumJournalBytes)
        {
            throw new InvalidDataException("The commit journal entry size is invalid.");
        }

        JournalEnvelope envelope;
        try
        {
            using var stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.SequentialScan);
            envelope = JsonSerializer.Deserialize<JournalEnvelope>(stream, SerializerOptions)
                ?? throw new InvalidDataException("The commit journal entry is empty.");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("The commit journal entry is malformed.", exception);
        }

        ValidateAuthentication(envelope);
        ValidateLoadedRecord(fileName, envelope.Payload);
        return envelope.Payload;
    }

    private void Persist(JournalRecord record)
    {
        var target = GetEntryPath(record.KeyHash);
        var temporary = Path.Combine(_root, $".{record.KeyHash}.{Guid.NewGuid():N}.tmp");
        var authenticatedPayload = JsonSerializer.SerializeToUtf8Bytes(record, SerializerOptions);
        var tag = Convert.ToHexString(HMACSHA256.HashData(_integrityKey, authenticatedPayload))
            .ToLowerInvariant();
        var payload = JsonSerializer.SerializeToUtf8Bytes(
            new JournalEnvelope(record, tag),
            SerializerOptions);
        if (payload.Length is <= 0 or > MaximumJournalBytes)
        {
            throw new InvalidDataException("The commit journal entry size is invalid.");
        }

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

    private void ValidateAuthentication(JournalEnvelope envelope)
    {
        if (!IsLowerHex(envelope.AuthenticationTag, 64))
        {
            throw new InvalidDataException("The commit journal authentication tag is invalid.");
        }

        var payload = JsonSerializer.SerializeToUtf8Bytes(envelope.Payload, SerializerOptions);
        var expected = HMACSHA256.HashData(_integrityKey, payload);
        var supplied = Convert.FromHexString(envelope.AuthenticationTag);
        if (!CryptographicOperations.FixedTimeEquals(expected, supplied))
        {
            throw new InvalidDataException("The commit journal entry failed authentication.");
        }
    }

    private string GetEntryPath(string keyHash)
    {
        var path = Path.GetFullPath(Path.Combine(_root, $"{keyHash}.json"));
        if (!string.Equals(Path.GetDirectoryName(path), _root, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("The commit journal entry escaped its root.");
        }

        return path;
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
                throw new IOException("The commit journal lock could not be acquired.");
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

    private static void ValidateLoadedRecord(string fileName, JournalRecord record)
    {
        if (record.Version != JournalVersion ||
            !string.Equals(record.KeyHash, fileName, StringComparison.Ordinal) ||
            !IsLowerHex(record.KeyHash, 64) ||
            !IsLowerHex(record.Digest, 64) ||
            record.OwnerProcessId <= 0 ||
            record.OwnerStartTimeUtcTicks <= 0 ||
            !IsLowerHex(record.ReservationId, 32) ||
            !Enum.IsDefined(record.State) ||
            (record.State == CommitJournalState.Committed) != (record.ResultData is not null))
        {
            throw new InvalidDataException("The commit journal entry failed validation.");
        }
    }

    private static void ValidateIdentity(string jobId, string idempotencyKey, string digest)
    {
        if (string.IsNullOrWhiteSpace(jobId) || jobId.Length > 64 ||
            string.IsNullOrWhiteSpace(idempotencyKey) || idempotencyKey.Length > 128 ||
            !IsLowerHex(digest, 64))
        {
            throw new ArgumentException("The commit journal identity is invalid.");
        }
    }

    private static string ComputeKeyHash(string jobId, string idempotencyKey)
    {
        var identity = $"{jobId}\u001f{idempotencyKey}";
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity)))
            .ToLowerInvariant();
    }

    private static bool IsLowerHex(string value, int length) =>
        value.Length == length && value.All(character =>
            character is >= '0' and <= '9' or >= 'a' and <= 'f');

    private static BridgeHostResult CloneResult(BridgeHostResult result) =>
        new(result.Outcome, result.Data?.DeepClone().AsObject());

    private static JsonObject SanitizeResultData(BridgeHostResult result)
    {
        var data = result.Data?.DeepClone().AsObject()
            ?? throw new ArgumentException("The commit result has no data.", nameof(result));
        data.Remove("job_id");
        return data;
    }

    private static JsonObject RestoreResultData(JsonObject persisted, string jobId)
    {
        var data = persisted.DeepClone().AsObject();
        data["job_id"] = jobId;
        return data;
    }

    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = false,
        WriteIndented = false,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    private sealed record JournalRecord(
        int Version,
        string KeyHash,
        string Digest,
        CommitJournalState State,
        int OwnerProcessId,
        long OwnerStartTimeUtcTicks,
        string ReservationId,
        JsonObject? ResultData);

    private sealed record JournalEnvelope(
        JournalRecord Payload,
        string AuthenticationTag);

    private sealed class SystemCommitJournalProcessProbe : ICommitJournalProcessProbe
    {
        public CommitJournalProcessIdentity Current
        {
            get
            {
                using var process = Process.GetCurrentProcess();
                return new CommitJournalProcessIdentity(
                    process.Id,
                    process.StartTime.ToUniversalTime().Ticks);
            }
        }

        public CommitJournalProcessLiveness GetLiveness(CommitJournalProcessIdentity identity)
        {
            try
            {
                using var process = Process.GetProcessById(identity.ProcessId);
                return process.StartTime.ToUniversalTime().Ticks == identity.StartTimeUtcTicks
                    ? CommitJournalProcessLiveness.Alive
                    : CommitJournalProcessLiveness.Dead;
            }
            catch (ArgumentException)
            {
                return CommitJournalProcessLiveness.Dead;
            }
            catch (InvalidOperationException)
            {
                return CommitJournalProcessLiveness.Dead;
            }
            catch
            {
                return CommitJournalProcessLiveness.Unknown;
            }
        }
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
