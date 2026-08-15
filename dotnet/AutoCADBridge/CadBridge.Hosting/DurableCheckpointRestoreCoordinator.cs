using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CadBridge.Hosting;

/// <summary>The durable phases of one whole-DWG checkpoint replacement.</summary>
public enum DurableCheckpointRestoreState
{
    Prepared,
    Replaced,
    Verified,
    Committed,
    Quarantined,
}

/// <summary>The closed outcome returned by the restore coordinator.</summary>
public enum DurableCheckpointRestoreOutcome
{
    Completed,
    Replayed,
    Cancelled,
    Rejected,
    ScopeConflict,
    RecoveryRequired,
    Quarantined,
}

/// <summary>
/// Non-secret rb1 facts supplied only after an outer authorization boundary has verified the
/// signature, namespace, reviewer, issue time, and expiry. This layer never accepts or parses a
/// rollback token. ApprovalTokenDigest binds recovery to those exact signed token bytes.
/// </summary>
public sealed record ValidatedRollbackAuthorization(
    string ApprovalId,
    string ApprovalTokenDigest,
    string JobId,
    string DocumentId,
    string CheckpointId,
    string PreRevision,
    string PostRevision);

/// <summary>One exact, already-authorized whole-DWG restore request.</summary>
public sealed record DurableCheckpointRestoreRequest(
    ValidatedRollbackAuthorization Authorization,
    string TargetPath);

/// <summary>Result of a restore or idempotent replay.</summary>
public sealed record DurableCheckpointRestoreResult(
    DurableCheckpointRestoreOutcome Outcome,
    DurableCheckpointRestoreState? State = null,
    string? RestoredRevision = null);

/// <summary>Semantic identity read from the live document, independent of file bytes.</summary>
public sealed record DurableRestoreDocumentSnapshot(
    string DocumentId,
    string Revision,
    string OriginalPathHash,
    bool IsOpen);

/// <summary>
/// AutoCAD lifecycle boundary. Implementations must resolve the exact path supplied by the
/// coordinator, close it without saving, and reopen the same document. They must not select a
/// similarly named document or fall back to the active document.
/// </summary>
public interface IDurableRestoreDocumentLifecycle
{
    ValueTask<DurableRestoreDocumentSnapshot> InspectAsync(
        string targetPath,
        CancellationToken cancellationToken);

    ValueTask CloseWithoutSaveAsync(string targetPath, CancellationToken cancellationToken);

    ValueTask ReopenAsync(string targetPath, CancellationToken cancellationToken);
}

/// <summary>
/// Crash-safe coordinator for an atomic whole-DWG checkpoint replacement. The caller must hold the
/// bridge writer lease for the exact document. Staging and journal reservation occur before the
/// point of no cancellation; close, replace, reopen, and verification then run to a durable outcome
/// without observing caller cancellation.
/// </summary>
public sealed class DurableCheckpointRestoreCoordinator : IDisposable
{
    private const string ExecutionLockFileName = ".checkpoint-restore.execution.lock";
    private static readonly StringComparison PathComparison = OperatingSystem.IsWindows()
        ? StringComparison.OrdinalIgnoreCase
        : StringComparison.Ordinal;

    private readonly DurableCheckpointCatalog _catalog;
    private readonly IDurableRestoreDocumentLifecycle _lifecycle;
    private readonly string _checkpointRoot;
    private readonly RestoreJournal _journal;
    private readonly string _executionLockPath;
    private bool _disposed;

    public DurableCheckpointRestoreCoordinator(
        DurableCheckpointCatalog catalog,
        string checkpointRoot,
        string journalRoot,
        ReadOnlySpan<byte> journalAuthenticationKey,
        IDurableRestoreDocumentLifecycle lifecycle)
    {
        ArgumentNullException.ThrowIfNull(catalog);
        ArgumentNullException.ThrowIfNull(lifecycle);
        _catalog = catalog;
        _lifecycle = lifecycle;
        _checkpointRoot = PrepareLocalRoot(checkpointRoot, create: false);
        var preparedJournalRoot = PrepareLocalRoot(journalRoot, create: true);
        _journal = new RestoreJournal(preparedJournalRoot, journalAuthenticationKey);
        _executionLockPath = ResolveDirectChild(preparedJournalRoot, ExecutionLockFileName);
    }

    /// <summary>
    /// Proves that an already signature-validated rb1 scope is bound to an authenticated,
    /// non-quarantined journal entry. This method never creates a reservation, stages a file, or
    /// invokes the document lifecycle; it exists solely to gate recovery after token expiry.
    /// </summary>
    public bool CanResumeExactRecordedAttempt(ValidatedRollbackAuthorization authorization)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        ValidateAuthorization(authorization);
        var checkpoint = _catalog.GetRequired(authorization.CheckpointId);
        var scopeDigest = ComputeRecordedScopeDigest(
            authorization,
            checkpoint,
            checkpoint.OriginalPathHash);

        using var executionLock = AcquireExecutionLock();
        var existing = _journal.TryGet(authorization.ApprovalId);
        return existing is not null &&
            string.Equals(existing.ScopeDigest, scopeDigest, StringComparison.Ordinal) &&
            existing.State is DurableCheckpointRestoreState.Prepared or
                DurableCheckpointRestoreState.Replaced or
                DurableCheckpointRestoreState.Verified or
                DurableCheckpointRestoreState.Committed;
    }

    /// <summary>
    /// Resolves the encrypted target locator of an exact authenticated journal attempt. A null
    /// result means no attempt was ever reserved for this approval. Any existing but mismatched,
    /// quarantined, malformed, non-local, or reparse-backed locator fails closed.
    /// </summary>
    public string? ResolveRecordedTarget(ValidatedRollbackAuthorization authorization)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        ValidateAuthorization(authorization);
        var checkpoint = _catalog.GetRequired(authorization.CheckpointId);
        var scopeDigest = ComputeRecordedScopeDigest(
            authorization,
            checkpoint,
            checkpoint.OriginalPathHash);

        using var executionLock = AcquireExecutionLock();
        var existing = _journal.TryGet(authorization.ApprovalId);
        if (existing is null)
        {
            return null;
        }

        if (!string.Equals(existing.ScopeDigest, scopeDigest, StringComparison.Ordinal) ||
            existing.State == DurableCheckpointRestoreState.Quarantined)
        {
            throw new InvalidDataException(
                "The recorded restore target does not match the authorized scope.");
        }

        var targetPath = ValidateTargetPath(_journal.UnprotectTargetLocator(existing));
        var actualPathHash = DurableCheckpointCatalog.ComputeOriginalPathHash(targetPath);
        if (!CryptographicOperations.FixedTimeEquals(
                Convert.FromHexString(actualPathHash),
                Convert.FromHexString(checkpoint.OriginalPathHash)))
        {
            throw new InvalidDataException(
                "The recorded restore target path does not match the checkpoint.");
        }

        return targetPath;
    }

    /// <summary>
    /// Starts or resumes an exact authorized restore. A committed approval replays without touching
    /// the lifecycle. An interrupted nonterminal attempt is reconciled only from authenticated
    /// state and exact file hashes; ambiguous evidence is quarantined.
    /// </summary>
    public async ValueTask<DurableCheckpointRestoreResult> RestoreAsync(
        DurableCheckpointRestoreRequest request,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        ValidateRequest(request);
        var targetPath = ValidateTargetPath(request.TargetPath);
        var checkpoint = _catalog.GetRequired(request.Authorization.CheckpointId);
        var scopeDigest = ComputeScopeDigest(request.Authorization, checkpoint, targetPath);

        FileStream executionLock;
        try
        {
            executionLock = AcquireExecutionLock();
        }
        catch (IOException)
        {
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.RecoveryRequired);
        }

        await using (executionLock)
        {
            var existing = _journal.TryGet(request.Authorization.ApprovalId);
            if (existing is not null)
            {
                if (!string.Equals(existing.ScopeDigest, scopeDigest, StringComparison.Ordinal))
                {
                    return new DurableCheckpointRestoreResult(
                        DurableCheckpointRestoreOutcome.ScopeConflict,
                        existing.State);
                }

                if (existing.State == DurableCheckpointRestoreState.Committed)
                {
                    return ReplayCommitted(
                        request.Authorization,
                        targetPath,
                        existing);
                }

                if (existing.State == DurableCheckpointRestoreState.Quarantined)
                {
                    TryQuarantineCatalog(checkpoint.CheckpointId);
                    return new DurableCheckpointRestoreResult(
                        DurableCheckpointRestoreOutcome.Quarantined,
                        existing.State);
                }

                return await ResumeAsync(
                    request,
                    targetPath,
                    checkpoint,
                    existing,
                    cancellationToken);
            }

            if (checkpoint.State != DurableCheckpointState.Available)
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.Rejected);
            }

            if (cancellationToken.IsCancellationRequested)
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.Cancelled);
            }

            return await PrepareAndRestoreAsync(
                request,
                targetPath,
                checkpoint,
                scopeDigest,
                cancellationToken);
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _journal.Dispose();
        _disposed = true;
        GC.SuppressFinalize(this);
    }

    private async ValueTask<DurableCheckpointRestoreResult> PrepareAndRestoreAsync(
        DurableCheckpointRestoreRequest request,
        string targetPath,
        DurableCheckpointRecord checkpoint,
        string scopeDigest,
        CancellationToken cancellationToken)
    {
        FileFacts targetFacts;
        try
        {
            targetFacts = ReadDwgFacts(targetPath);
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException)
        {
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Rejected);
        }

        var artifactPath = ResolveCheckpointArtifact(checkpoint.CheckpointFileName);
        FileFacts checkpointFacts;
        try
        {
            checkpointFacts = ReadDwgFacts(artifactPath);
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException)
        {
            TryQuarantineCatalog(checkpoint.CheckpointId);
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Quarantined,
                DurableCheckpointRestoreState.Quarantined);
        }

        if (!FactsMatch(checkpointFacts, checkpoint.Sha256, checkpoint.ByteLength))
        {
            TryQuarantineCatalog(checkpoint.CheckpointId);
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Quarantined,
                DurableCheckpointRestoreState.Quarantined);
        }

        var approvalKeyHash = ComputeApprovalKeyHash(request.Authorization.ApprovalId);
        var scopeSuffix = scopeDigest[..16];
        var stageFileName = $".cad-harness-restore-{approvalKeyHash[..24]}-{scopeSuffix}.stage.dwg";
        var backupFileName = $".cad-harness-restore-{approvalKeyHash[..24]}-{scopeSuffix}.backup.dwg";
        var targetDirectory = Path.GetDirectoryName(targetPath)
            ?? throw new InvalidOperationException("The target drawing has no parent directory.");
        var stagePath = ResolveDirectChild(targetDirectory, stageFileName);
        var backupPath = ResolveDirectChild(targetDirectory, backupFileName);

        try
        {
            EnsureAbsentOrMatchingStage(stagePath, checkpointFacts);
            if (File.Exists(backupPath))
            {
                throw new InvalidDataException("A restore backup already exists before reservation.");
            }

            if (!File.Exists(stagePath))
            {
                CopyDurably(artifactPath, stagePath);
            }

            var stagedFacts = ReadDwgFacts(stagePath);
            if (stagedFacts != checkpointFacts)
            {
                throw new InvalidDataException("The staged checkpoint failed hash verification.");
            }

            var prepared = new RestoreJournalRecord(
                RestoreJournal.CurrentVersion,
                ComputeApprovalKeyHash(request.Authorization.ApprovalId),
                scopeDigest,
                DurableCheckpointRestoreState.Prepared,
                checkpoint.Sha256,
                checkpoint.ByteLength,
                targetFacts.Sha256,
                targetFacts.ByteLength,
                stageFileName,
                backupFileName,
                _journal.ProtectTargetLocator(
                    targetPath,
                    ComputeApprovalKeyHash(request.Authorization.ApprovalId),
                    scopeDigest));
            _journal.CreatePrepared(request.Authorization.ApprovalId, prepared);
            _catalog.BeginRestore(checkpoint.CheckpointId);
            return await ResumeAsync(
                request,
                targetPath,
                checkpoint,
                prepared,
                cancellationToken);
        }
        catch (OperationCanceledException)
        {
            var prepared = _journal.TryGet(request.Authorization.ApprovalId);
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Cancelled,
                prepared?.State);
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or
            InvalidOperationException)
        {
            var journalRecord = _journal.TryGet(request.Authorization.ApprovalId);
            if (journalRecord is not null)
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    journalRecord.State);
            }

            TryDelete(stagePath);
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Rejected);
        }
    }

    private async ValueTask<DurableCheckpointRestoreResult> ResumeAsync(
        DurableCheckpointRestoreRequest request,
        string targetPath,
        DurableCheckpointRecord checkpoint,
        RestoreJournalRecord journal,
        CancellationToken cancellationToken)
    {
        var targetDirectory = Path.GetDirectoryName(targetPath)
            ?? throw new InvalidOperationException("The target drawing has no parent directory.");
        var stagePath = ResolveDirectChild(targetDirectory, journal.StageFileName);
        var backupPath = ResolveDirectChild(targetDirectory, journal.BackupFileName);

        if (journal.State == DurableCheckpointRestoreState.Prepared)
        {
            var evidence = ClassifyPreparedEvidence(targetPath, stagePath, backupPath, journal);
            if (evidence == PreparedEvidence.Replaced)
            {
                try
                {
                    journal = _journal.Transition(
                        request.Authorization.ApprovalId,
                        journal.ScopeDigest,
                        DurableCheckpointRestoreState.Prepared,
                        DurableCheckpointRestoreState.Replaced);
                }
                catch (IOException)
                {
                    return new DurableCheckpointRestoreResult(
                        DurableCheckpointRestoreOutcome.RecoveryRequired,
                        DurableCheckpointRestoreState.Prepared);
                }
            }
            else if (evidence != PreparedEvidence.NotReplaced)
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }
        }

        if (journal.State == DurableCheckpointRestoreState.Prepared)
        {
            if (checkpoint.State is not (DurableCheckpointState.Available or
                DurableCheckpointState.Restoring))
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }

            _catalog.BeginRestore(checkpoint.CheckpointId);
            DurableRestoreDocumentSnapshot before;
            try
            {
                before = await _lifecycle.InspectAsync(targetPath, cancellationToken);
                if (!before.IsOpen)
                {
                    await _lifecycle.ReopenAsync(targetPath, cancellationToken);
                    before = await _lifecycle.InspectAsync(targetPath, cancellationToken);
                }
            }
            catch (OperationCanceledException)
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.Cancelled,
                    journal.State);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    journal.State);
            }

            if (!SnapshotMatches(
                    before,
                    request.Authorization.DocumentId,
                    request.Authorization.PostRevision,
                    checkpoint.OriginalPathHash) ||
                !FactsMatch(ReadDwgFacts(targetPath), journal.PostSha256, journal.PostByteLength))
            {
                return QuarantineBeforeReplacement(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal,
                    stagePath);
            }

            if (cancellationToken.IsCancellationRequested)
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.Cancelled,
                    journal.State);
            }

            // The destructive phase begins here. Caller cancellation is intentionally ignored from
            // this point so a closed document is never left halfway through replacement.
            try
            {
                await _lifecycle.CloseWithoutSaveAsync(targetPath, CancellationToken.None);
                ReplaceDurably(stagePath, targetPath, backupPath);
            }
            catch
            {
                var evidence = ClassifyPreparedEvidence(targetPath, stagePath, backupPath, journal);
                if (evidence != PreparedEvidence.Replaced)
                {
                    return evidence == PreparedEvidence.NotReplaced
                        ? new DurableCheckpointRestoreResult(
                            DurableCheckpointRestoreOutcome.RecoveryRequired,
                            journal.State)
                        : Quarantine(
                            request.Authorization.ApprovalId,
                            checkpoint.CheckpointId,
                            journal);
                }
            }

            try
            {
                journal = _journal.Transition(
                    request.Authorization.ApprovalId,
                    journal.ScopeDigest,
                    DurableCheckpointRestoreState.Prepared,
                    DurableCheckpointRestoreState.Replaced);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    DurableCheckpointRestoreState.Prepared);
            }
        }

        if (journal.State == DurableCheckpointRestoreState.Replaced)
        {
            if (!ReplacementFactsMatch(targetPath, backupPath, journal))
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }

            DurableRestoreDocumentSnapshot after;
            try
            {
                await _lifecycle.ReopenAsync(targetPath, CancellationToken.None);
                after = await _lifecycle.InspectAsync(targetPath, CancellationToken.None);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    journal.State);
            }

            if (!SnapshotMatches(
                    after,
                    request.Authorization.DocumentId,
                    request.Authorization.PreRevision,
                    checkpoint.OriginalPathHash) ||
                !FactsMatch(ReadDwgFacts(targetPath), journal.CheckpointSha256,
                    journal.CheckpointByteLength))
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }

            try
            {
                journal = _journal.Transition(
                    request.Authorization.ApprovalId,
                    journal.ScopeDigest,
                    DurableCheckpointRestoreState.Replaced,
                    DurableCheckpointRestoreState.Verified);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    DurableCheckpointRestoreState.Replaced);
            }
        }

        if (journal.State == DurableCheckpointRestoreState.Verified)
        {
            if (!FactsMatch(ReadDwgFacts(targetPath), journal.CheckpointSha256,
                    journal.CheckpointByteLength))
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }

            DurableRestoreDocumentSnapshot verified;
            try
            {
                verified = await _lifecycle.InspectAsync(targetPath, CancellationToken.None);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    journal.State);
            }

            if (!SnapshotMatches(
                    verified,
                    request.Authorization.DocumentId,
                    request.Authorization.PreRevision,
                    checkpoint.OriginalPathHash))
            {
                return Quarantine(
                    request.Authorization.ApprovalId,
                    checkpoint.CheckpointId,
                    journal);
            }

            try
            {
                var currentCheckpoint = _catalog.GetRequired(checkpoint.CheckpointId);
                if (currentCheckpoint.State == DurableCheckpointState.Restoring)
                {
                    _catalog.Complete(checkpoint.CheckpointId);
                }
                else if (currentCheckpoint.State != DurableCheckpointState.Consumed)
                {
                    return Quarantine(
                        request.Authorization.ApprovalId,
                        checkpoint.CheckpointId,
                        journal);
                }

                journal = _journal.Transition(
                    request.Authorization.ApprovalId,
                    journal.ScopeDigest,
                    DurableCheckpointRestoreState.Verified,
                    DurableCheckpointRestoreState.Committed);
            }
            catch
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    DurableCheckpointRestoreState.Verified);
            }

            if (!TryCleanupCommittedArtifacts(stagePath, backupPath))
            {
                return new DurableCheckpointRestoreResult(
                    DurableCheckpointRestoreOutcome.RecoveryRequired,
                    DurableCheckpointRestoreState.Committed);
            }

            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.Completed,
                journal.State,
                request.Authorization.PreRevision);
        }

        return journal.State == DurableCheckpointRestoreState.Committed
            ? ReplayCommitted(request.Authorization, targetPath, journal)
            : Quarantine(request.Authorization.ApprovalId, checkpoint.CheckpointId, journal);
    }

    private static DurableCheckpointRestoreResult ReplayCommitted(
        ValidatedRollbackAuthorization authorization,
        string targetPath,
        RestoreJournalRecord journal)
    {
        var targetDirectory = Path.GetDirectoryName(targetPath)
            ?? throw new InvalidOperationException("The target drawing has no parent directory.");
        var stagePath = ResolveDirectChild(targetDirectory, journal.StageFileName);
        var backupPath = ResolveDirectChild(targetDirectory, journal.BackupFileName);
        if (!TryCleanupCommittedArtifacts(stagePath, backupPath))
        {
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                DurableCheckpointRestoreState.Committed);
        }

        return new DurableCheckpointRestoreResult(
            DurableCheckpointRestoreOutcome.Replayed,
            DurableCheckpointRestoreState.Committed,
            authorization.PreRevision);
    }

    private DurableCheckpointRestoreResult QuarantineBeforeReplacement(
        string approvalId,
        string checkpointId,
        RestoreJournalRecord journal,
        string stagePath)
    {
        try
        {
            _journal.Quarantine(approvalId, journal.ScopeDigest);
        }
        catch
        {
            return new DurableCheckpointRestoreResult(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                journal.State);
        }

        TryQuarantineCatalog(checkpointId);
        TryDelete(stagePath);
        return new DurableCheckpointRestoreResult(
            DurableCheckpointRestoreOutcome.Quarantined,
            DurableCheckpointRestoreState.Quarantined);
    }

    private DurableCheckpointRestoreResult Quarantine(
        string approvalId,
        string checkpointId,
        RestoreJournalRecord journal)
    {
        try
        {
            if (journal.State != DurableCheckpointRestoreState.Quarantined)
            {
                _journal.Quarantine(approvalId, journal.ScopeDigest);
            }
        }
        catch
        {
            // The caller still receives a fail-closed outcome; no recovery is guessed.
        }

        TryQuarantineCatalog(checkpointId);
        return new DurableCheckpointRestoreResult(
            DurableCheckpointRestoreOutcome.Quarantined,
            DurableCheckpointRestoreState.Quarantined);
    }

    private void TryQuarantineCatalog(string checkpointId)
    {
        try
        {
            var record = _catalog.GetRequired(checkpointId);
            if (record.State is DurableCheckpointState.Available or DurableCheckpointState.Restoring)
            {
                _catalog.Quarantine(checkpointId);
            }
        }
        catch
        {
            // Failure to persist quarantine never authorizes continued use.
        }
    }

    private static PreparedEvidence ClassifyPreparedEvidence(
        string targetPath,
        string stagePath,
        string backupPath,
        RestoreJournalRecord journal)
    {
        var target = TryReadDwgFacts(targetPath);
        var stage = TryReadDwgFacts(stagePath);
        var backup = TryReadDwgFacts(backupPath);
        if (target is not null && stage is not null && backup is null &&
            FactsMatch(target, journal.PostSha256, journal.PostByteLength) &&
            FactsMatch(stage, journal.CheckpointSha256, journal.CheckpointByteLength))
        {
            return PreparedEvidence.NotReplaced;
        }

        if (target is not null && stage is null && backup is not null &&
            FactsMatch(target, journal.CheckpointSha256, journal.CheckpointByteLength) &&
            FactsMatch(backup, journal.PostSha256, journal.PostByteLength))
        {
            return PreparedEvidence.Replaced;
        }

        return PreparedEvidence.Ambiguous;
    }

    private static bool ReplacementFactsMatch(
        string targetPath,
        string backupPath,
        RestoreJournalRecord journal)
    {
        var target = TryReadDwgFacts(targetPath);
        var backup = TryReadDwgFacts(backupPath);
        return target is not null && backup is not null &&
            FactsMatch(target, journal.CheckpointSha256, journal.CheckpointByteLength) &&
            FactsMatch(backup, journal.PostSha256, journal.PostByteLength);
    }

    private static bool SnapshotMatches(
        DurableRestoreDocumentSnapshot snapshot,
        string documentId,
        string revision,
        string originalPathHash) =>
        snapshot.IsOpen &&
        string.Equals(snapshot.DocumentId, documentId, StringComparison.Ordinal) &&
        string.Equals(snapshot.Revision, revision, StringComparison.Ordinal) &&
        string.Equals(snapshot.OriginalPathHash, originalPathHash, StringComparison.Ordinal);

    private static string ComputeScopeDigest(
        ValidatedRollbackAuthorization authorization,
        DurableCheckpointRecord checkpoint,
        string targetPath)
    {
        var pathHash = DurableCheckpointCatalog.ComputeOriginalPathHash(targetPath);
        if (!string.Equals(pathHash, checkpoint.OriginalPathHash, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "The restore target does not match the checkpoint document path.",
                nameof(targetPath));
        }

        return ComputeRecordedScopeDigest(authorization, checkpoint, pathHash);
    }

    private static string ComputeRecordedScopeDigest(
        ValidatedRollbackAuthorization authorization,
        DurableCheckpointRecord checkpoint,
        string pathHash)
    {
        if (!string.Equals(authorization.JobId, checkpoint.JobId, StringComparison.Ordinal) ||
            !string.Equals(authorization.DocumentId, checkpoint.DocumentId, StringComparison.Ordinal) ||
            !string.Equals(authorization.CheckpointId, checkpoint.CheckpointId,
                StringComparison.Ordinal) ||
            !string.Equals(authorization.PreRevision, checkpoint.PreRevision,
                StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "The validated rollback authorization does not match the checkpoint scope.",
                nameof(authorization));
        }

        if (!string.Equals(pathHash, checkpoint.OriginalPathHash, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "The recorded restore path hash does not match the checkpoint scope.",
                nameof(pathHash));
        }

        var canonical = string.Join(
            "\u001f",
            "cad-harness-checkpoint-restore-v1",
            authorization.ApprovalId,
            authorization.ApprovalTokenDigest,
            authorization.JobId,
            authorization.DocumentId,
            authorization.CheckpointId,
            authorization.PreRevision,
            authorization.PostRevision,
            checkpoint.Sha256,
            checkpoint.ByteLength.ToString(System.Globalization.CultureInfo.InvariantCulture),
            checkpoint.DwgVersion,
            pathHash);
        return LowerHex(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
    }

    private static void ValidateRequest(DurableCheckpointRestoreRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateAuthorization(request.Authorization);
    }

    private static void ValidateAuthorization(ValidatedRollbackAuthorization authorization)
    {
        ArgumentNullException.ThrowIfNull(authorization);
        ValidateIdentifier(authorization.ApprovalId, nameof(authorization.ApprovalId));
        if (!IsLowerHexDigest(authorization.ApprovalTokenDigest))
        {
            throw new ArgumentException(
                "The rollback approval token digest is invalid.",
                nameof(authorization.ApprovalTokenDigest));
        }

        ValidateIdentifier(authorization.JobId, nameof(authorization.JobId));
        ValidateIdentifier(authorization.DocumentId, nameof(authorization.DocumentId));
        ValidateIdentifier(authorization.CheckpointId, nameof(authorization.CheckpointId));
        ValidateRevision(authorization.PreRevision, nameof(authorization.PreRevision));
        ValidateRevision(authorization.PostRevision, nameof(authorization.PostRevision));
        if (string.Equals(authorization.PreRevision,
                authorization.PostRevision, StringComparison.Ordinal))
        {
            throw new ArgumentException("Restore revisions must describe an actual change.",
                nameof(authorization));
        }
    }

    private static void ValidateIdentifier(string value, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 128 ||
            value.Any(character => !(char.IsAsciiLetterOrDigit(character) ||
                character is '-' or '_' or '.' or ':')))
        {
            throw new ArgumentException("A restore identifier is invalid.", parameterName);
        }
    }

    private static void ValidateRevision(string value, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 256 ||
            value.Any(char.IsControl))
        {
            throw new ArgumentException("A restore revision is invalid.", parameterName);
        }
    }

    private static string ValidateTargetPath(string targetPath)
    {
        if (string.IsNullOrWhiteSpace(targetPath) || !Path.IsPathFullyQualified(targetPath) ||
            !targetPath.IsNormalized(NormalizationForm.FormC))
        {
            throw new ArgumentException(
                "The restore target must be a canonical absolute local DWG path.",
                nameof(targetPath));
        }

        RejectNetworkOrDevicePath(targetPath, nameof(targetPath));
        var fullPath = Path.GetFullPath(targetPath).Normalize(NormalizationForm.FormC);
        if (!string.Equals(targetPath, fullPath, PathComparison))
        {
            throw new ArgumentException(
                "The restore target must already be in canonical absolute form.",
                nameof(targetPath));
        }

        RejectNetworkOrDevicePath(fullPath, nameof(targetPath));
        if (!string.Equals(Path.GetExtension(fullPath), ".dwg", StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException("The restore target must be a DWG file.",
                nameof(targetPath));
        }

        var directory = Path.GetDirectoryName(fullPath)
            ?? throw new ArgumentException("The restore target has no parent directory.",
                nameof(targetPath));
        RejectExistingReparseComponents(directory);
        RejectReparsePoint(fullPath);
        if (!Directory.Exists(directory) || !File.Exists(fullPath))
        {
            throw new ArgumentException(
                "The restore target must be an existing regular local DWG.",
                nameof(targetPath));
        }

        var attributes = File.GetAttributes(fullPath);
        if ((attributes & (FileAttributes.Directory | FileAttributes.Device |
                FileAttributes.ReadOnly | FileAttributes.ReparsePoint)) != 0)
        {
            throw new ArgumentException(
                "The restore target must be a writable regular local DWG.",
                nameof(targetPath));
        }

        return fullPath;
    }

    private static string PrepareLocalRoot(string root, bool create)
    {
        if (string.IsNullOrWhiteSpace(root) || !Path.IsPathFullyQualified(root))
        {
            throw new ArgumentException("A restore root must be an absolute local path.", nameof(root));
        }

        var fullPath = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        RejectNetworkOrDevicePath(fullPath, nameof(root));
        RejectExistingReparseComponents(Path.GetDirectoryName(fullPath) ?? fullPath);
        if (create)
        {
            Directory.CreateDirectory(fullPath);
        }
        else if (!Directory.Exists(fullPath))
        {
            throw new DirectoryNotFoundException("The checkpoint root does not exist.");
        }

        RejectReparsePoint(fullPath);
        return fullPath;
    }

    private string ResolveCheckpointArtifact(string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName) || Path.IsPathFullyQualified(fileName) ||
            !string.Equals(Path.GetFileName(fileName), fileName, StringComparison.Ordinal) ||
            !string.Equals(Path.GetExtension(fileName), ".dwg", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("The checkpoint artifact name is invalid.");
        }

        var path = ResolveDirectChild(_checkpointRoot, fileName);
        RejectReparsePoint(path);
        return path;
    }

    private static string ResolveDirectChild(string root, string fileName)
    {
        var fullRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var path = Path.GetFullPath(Path.Combine(fullRoot, fileName));
        if (!string.Equals(Path.GetDirectoryName(path), fullRoot, PathComparison))
        {
            throw new InvalidDataException("A restore artifact escaped its directory.");
        }

        return path;
    }

    private FileStream AcquireExecutionLock()
    {
        RejectReparsePoint(_executionLockPath);
        return new FileStream(
            _executionLockPath,
            FileMode.OpenOrCreate,
            FileAccess.ReadWrite,
            FileShare.None,
            bufferSize: 1,
            FileOptions.WriteThrough);
    }

    private static void CopyDurably(string source, string destination)
    {
        RejectReparsePoint(source);
        using var input = new FileStream(
            source,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.SequentialScan);
        using var output = new FileStream(
            destination,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            bufferSize: 64 * 1024,
            FileOptions.WriteThrough);
        input.CopyTo(output);
        output.Flush(flushToDisk: true);
    }

    private static void ReplaceDurably(string stagePath, string targetPath, string backupPath)
    {
        RejectReparsePoint(stagePath);
        RejectReparsePoint(targetPath);
        if (File.Exists(backupPath))
        {
            throw new InvalidDataException("The restore backup already exists.");
        }

        File.Replace(stagePath, targetPath, backupPath, ignoreMetadataErrors: false);
        FlushExisting(targetPath);
        FlushExisting(backupPath);
    }

    private static void FlushExisting(string path)
    {
        using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.ReadWrite,
            FileShare.Read,
            bufferSize: 1,
            FileOptions.WriteThrough);
        stream.Flush(flushToDisk: true);
    }

    private static void EnsureAbsentOrMatchingStage(string stagePath, FileFacts expected)
    {
        RejectReparsePoint(stagePath);
        if (File.Exists(stagePath) && ReadDwgFacts(stagePath) != expected)
        {
            throw new InvalidDataException("An existing restore stage does not match the checkpoint.");
        }
    }

    private static FileFacts ReadDwgFacts(string path)
    {
        RejectReparsePoint(path);
        using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.SequentialScan);
        if (stream.Length < 6)
        {
            throw new InvalidDataException("A restore DWG artifact is truncated.");
        }

        Span<byte> header = stackalloc byte[6];
        stream.ReadExactly(header);
        if (header[0] != (byte)'A' || header[1] != (byte)'C' ||
            !header[2..].ToArray().All(character => character is >= (byte)'0' and <= (byte)'9'))
        {
            throw new InvalidDataException("A restore artifact has an invalid DWG header.");
        }

        stream.Position = 0;
        var digest = SHA256.HashData(stream);
        return new FileFacts(LowerHex(digest), stream.Length);
    }

    private static FileFacts? TryReadDwgFacts(string path)
    {
        try
        {
            return File.Exists(path) ? ReadDwgFacts(path) : null;
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or
            UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static bool FactsMatch(FileFacts facts, string sha256, long byteLength) =>
        string.Equals(facts.Sha256, sha256, StringComparison.Ordinal) &&
        facts.ByteLength == byteLength;

    private static string ComputeApprovalKeyHash(string approvalId) =>
        LowerHex(SHA256.HashData(Encoding.UTF8.GetBytes(
            "cad-harness-restore-approval-v1\0" + approvalId)));

    private static string LowerHex(ReadOnlySpan<byte> bytes) =>
        Convert.ToHexString(bytes).ToLowerInvariant();

    private static bool IsLowerHexDigest(string value) =>
        value.Length == 64 && value.All(character =>
            character is >= '0' and <= '9' or >= 'a' and <= 'f');

    private static void RejectNetworkOrDevicePath(string path, string parameterName)
    {
        if (path.StartsWith("\\\\", StringComparison.Ordinal) ||
            path.StartsWith("//", StringComparison.Ordinal) ||
            path.StartsWith("\\??\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\.\\", StringComparison.Ordinal))
        {
            throw new ArgumentException("Network and device paths are not allowed.", parameterName);
        }

        if (OperatingSystem.IsWindows())
        {
            var root = Path.GetPathRoot(path);
            if (!string.IsNullOrEmpty(root) && new DriveInfo(root).DriveType == DriveType.Network)
            {
                throw new ArgumentException("Mapped network drives are not allowed.", parameterName);
            }
        }
    }

    private static void RejectExistingReparseComponents(string path)
    {
        var current = Path.GetFullPath(path);
        while (!string.IsNullOrEmpty(current))
        {
            RejectReparsePoint(current);
            var parent = Path.GetDirectoryName(current);
            if (string.Equals(parent, current, PathComparison))
            {
                break;
            }

            current = parent ?? string.Empty;
        }
    }

    private static void RejectReparsePoint(string path)
    {
        if (File.Exists(path) || Directory.Exists(path))
        {
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException("Restore paths must not contain reparse points.");
            }
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            RejectReparsePoint(path);
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // Orphans are opaque, same-directory artifacts and are never trusted on a future run.
        }
    }

    private static bool TryCleanupCommittedArtifacts(string stagePath, string backupPath)
    {
        try
        {
            DeleteCommittedArtifact(stagePath);
            DeleteCommittedArtifact(backupPath);
            return !File.Exists(stagePath) && !Directory.Exists(stagePath) &&
                !File.Exists(backupPath) && !Directory.Exists(backupPath);
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or
            UnauthorizedAccessException)
        {
            return false;
        }
    }

    private static void DeleteCommittedArtifact(string path)
    {
        RejectReparsePoint(path);
        if (Directory.Exists(path))
        {
            throw new InvalidDataException(
                "A committed restore artifact path unexpectedly names a directory.");
        }

        if (File.Exists(path))
        {
            File.Delete(path);
        }

        if (File.Exists(path) || Directory.Exists(path))
        {
            throw new IOException("A committed restore artifact could not be removed.");
        }
    }

    private enum PreparedEvidence
    {
        NotReplaced,
        Replaced,
        Ambiguous,
    }

    private sealed record FileFacts(string Sha256, long ByteLength);

    private sealed class RestoreJournal : IDisposable
    {
        public const int CurrentVersion = 1;
        private const int MaximumEntryBytes = 256 * 1024;
        private static readonly JsonSerializerOptions SerializerOptions = new()
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            PropertyNameCaseInsensitive = false,
            WriteIndented = false,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
            MaxDepth = 16,
            Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
        };

        private readonly string _root;
        private readonly byte[] _key;
        private readonly byte[] _targetLocatorKey;
        private bool _disposed;

        public RestoreJournal(string root, ReadOnlySpan<byte> key)
        {
            if (key.Length < 32)
            {
                throw new ArgumentException(
                    "The restore journal authentication key must contain at least 32 bytes.",
                    nameof(key));
            }

            _root = root;
            _key = key.ToArray();
            _targetLocatorKey = HMACSHA256.HashData(
                _key,
                Encoding.UTF8.GetBytes("cad-harness-restore-target-locator-key-v1"));
            foreach (var path in Directory.EnumerateFiles(_root, "*.restore.json",
                         SearchOption.TopDirectoryOnly))
            {
                _ = Read(path);
            }
        }

        public RestoreJournalRecord? TryGet(string approvalId)
        {
            ThrowIfDisposed();
            var path = EntryPath(approvalId);
            return File.Exists(path) ? Read(path) : null;
        }

        public void CreatePrepared(string approvalId, RestoreJournalRecord record)
        {
            ThrowIfDisposed();
            var path = EntryPath(approvalId);
            if (File.Exists(path))
            {
                var existing = Read(path);
                if (existing == record)
                {
                    return;
                }

                throw new InvalidOperationException(
                    "The rollback approval is already bound to another restore attempt.");
            }

            Persist(path, record, replace: false);
        }

        public string ProtectTargetLocator(
            string targetPath,
            string approvalKeyHash,
            string scopeDigest)
        {
            ThrowIfDisposed();
            var plaintext = Encoding.UTF8.GetBytes(targetPath);
            if (plaintext.Length is <= 0 or > 128 * 1024)
            {
                CryptographicOperations.ZeroMemory(plaintext);
                throw new InvalidDataException("The restore target locator is outside its bounds.");
            }

            var nonce = RandomNumberGenerator.GetBytes(12);
            var tag = new byte[16];
            var ciphertext = new byte[plaintext.Length];
            var associatedData = TargetLocatorAssociatedData(approvalKeyHash, scopeDigest);
            try
            {
                using var cipher = new AesGcm(_targetLocatorKey, tag.Length);
                cipher.Encrypt(nonce, plaintext, ciphertext, tag, associatedData);
                var envelope = new byte[1 + nonce.Length + tag.Length + ciphertext.Length];
                envelope[0] = 1;
                Buffer.BlockCopy(nonce, 0, envelope, 1, nonce.Length);
                Buffer.BlockCopy(tag, 0, envelope, 1 + nonce.Length, tag.Length);
                Buffer.BlockCopy(
                    ciphertext,
                    0,
                    envelope,
                    1 + nonce.Length + tag.Length,
                    ciphertext.Length);
                return Convert.ToBase64String(envelope);
            }
            finally
            {
                CryptographicOperations.ZeroMemory(plaintext);
                CryptographicOperations.ZeroMemory(ciphertext);
                CryptographicOperations.ZeroMemory(associatedData);
            }
        }

        public string UnprotectTargetLocator(RestoreJournalRecord record)
        {
            ThrowIfDisposed();
            byte[] envelope;
            try
            {
                envelope = Convert.FromBase64String(record.ProtectedTargetLocator);
            }
            catch (FormatException exception)
            {
                throw new InvalidDataException(
                    "The protected restore target locator is malformed.",
                    exception);
            }

            if (envelope.Length < 30 || envelope[0] != 1)
            {
                throw new InvalidDataException(
                    "The protected restore target locator has an invalid envelope.");
            }

            var nonce = envelope.AsSpan(1, 12);
            var tag = envelope.AsSpan(13, 16);
            var ciphertext = envelope.AsSpan(29);
            var plaintext = new byte[ciphertext.Length];
            var associatedData = TargetLocatorAssociatedData(
                record.ApprovalKeyHash,
                record.ScopeDigest);
            try
            {
                using var cipher = new AesGcm(_targetLocatorKey, tag.Length);
                cipher.Decrypt(nonce, ciphertext, tag, plaintext, associatedData);
                return new UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false,
                    throwOnInvalidBytes: true).GetString(plaintext);
            }
            catch (CryptographicException exception)
            {
                throw new InvalidDataException(
                    "The protected restore target locator failed authentication.",
                    exception);
            }
            catch (DecoderFallbackException exception)
            {
                throw new InvalidDataException(
                    "The protected restore target locator is not valid UTF-8.",
                    exception);
            }
            finally
            {
                CryptographicOperations.ZeroMemory(plaintext);
                CryptographicOperations.ZeroMemory(associatedData);
                CryptographicOperations.ZeroMemory(envelope);
            }
        }

        public RestoreJournalRecord Transition(
            string approvalId,
            string scopeDigest,
            DurableCheckpointRestoreState expected,
            DurableCheckpointRestoreState next)
        {
            ThrowIfDisposed();
            var path = EntryPath(approvalId);
            var current = Read(path);
            if (!string.Equals(current.ScopeDigest, scopeDigest, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("The restore scope digest changed.");
            }

            if (current.State == next)
            {
                return current;
            }

            if (current.State != expected || !IsAllowedTransition(expected, next))
            {
                throw new InvalidOperationException("The restore journal transition is invalid.");
            }

            var replacement = current with { State = next };
            Persist(path, replacement, replace: true);
            return replacement;
        }

        public RestoreJournalRecord Quarantine(string approvalId, string scopeDigest)
        {
            ThrowIfDisposed();
            var path = EntryPath(approvalId);
            var current = Read(path);
            if (!string.Equals(current.ScopeDigest, scopeDigest, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("The restore scope digest changed.");
            }

            if (current.State == DurableCheckpointRestoreState.Quarantined)
            {
                return current;
            }

            if (current.State == DurableCheckpointRestoreState.Committed)
            {
                throw new InvalidOperationException("A committed restore is terminal.");
            }

            var replacement = current with { State = DurableCheckpointRestoreState.Quarantined };
            Persist(path, replacement, replace: true);
            return replacement;
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            CryptographicOperations.ZeroMemory(_key);
            CryptographicOperations.ZeroMemory(_targetLocatorKey);
            _disposed = true;
        }

        private RestoreJournalRecord Read(string path)
        {
            RejectReparsePoint(path);
            var information = new FileInfo(path);
            if (!information.Exists || information.Length is <= 0 or > MaximumEntryBytes)
            {
                throw new InvalidDataException("A restore journal entry has an invalid size.");
            }

            RestoreJournalEnvelope envelope;
            try
            {
                var bytes = File.ReadAllBytes(path);
                using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
                RejectDuplicateProperties(document.RootElement);
                envelope = JsonSerializer.Deserialize<RestoreJournalEnvelope>(bytes,
                    SerializerOptions)
                    ?? throw new InvalidDataException("A restore journal entry is empty.");
            }
            catch (JsonException exception)
            {
                throw new InvalidDataException("A restore journal entry is malformed.", exception);
            }

            ValidateRecord(envelope.Payload, Path.GetFileName(path));
            if (!IsLowerHex(envelope.AuthenticationTag, 64))
            {
                throw new InvalidDataException("A restore journal authentication tag is invalid.");
            }

            var payload = JsonSerializer.SerializeToUtf8Bytes(envelope.Payload, SerializerOptions);
            var expected = HMACSHA256.HashData(_key,
                CombineDomainAndPayload("cad-harness-restore-journal-v1", payload));
            var supplied = Convert.FromHexString(envelope.AuthenticationTag);
            if (!CryptographicOperations.FixedTimeEquals(expected, supplied))
            {
                throw new InvalidDataException("A restore journal entry failed authentication.");
            }

            return envelope.Payload;
        }

        private void Persist(string target, RestoreJournalRecord record, bool replace)
        {
            var payload = JsonSerializer.SerializeToUtf8Bytes(record, SerializerOptions);
            var tag = LowerHex(HMACSHA256.HashData(_key,
                CombineDomainAndPayload("cad-harness-restore-journal-v1", payload)));
            var bytes = JsonSerializer.SerializeToUtf8Bytes(
                new RestoreJournalEnvelope(record, tag), SerializerOptions);
            if (bytes.Length is <= 0 or > MaximumEntryBytes)
            {
                throw new InvalidDataException("A restore journal entry has an invalid size.");
            }

            var temporary = ResolveDirectChild(_root,
                $".{Path.GetFileName(target)}.{Guid.NewGuid():N}.tmp");
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
                    stream.Write(bytes);
                    stream.Flush(flushToDisk: true);
                }

                if (replace)
                {
                    File.Replace(temporary, target, destinationBackupFileName: null);
                }
                else
                {
                    File.Move(temporary, target);
                }

                FlushExisting(target);
            }
            finally
            {
                TryDelete(temporary);
            }
        }

        private string EntryPath(string approvalId) => ResolveDirectChild(
            _root,
            ComputeApprovalKeyHash(approvalId) + ".restore.json");

        private static bool IsAllowedTransition(
            DurableCheckpointRestoreState expected,
            DurableCheckpointRestoreState next) =>
            (expected, next) is
                (DurableCheckpointRestoreState.Prepared,
                    DurableCheckpointRestoreState.Replaced) or
                (DurableCheckpointRestoreState.Prepared,
                    DurableCheckpointRestoreState.Quarantined) or
                (DurableCheckpointRestoreState.Replaced,
                    DurableCheckpointRestoreState.Verified) or
                (DurableCheckpointRestoreState.Replaced,
                    DurableCheckpointRestoreState.Quarantined) or
                (DurableCheckpointRestoreState.Verified,
                    DurableCheckpointRestoreState.Committed) or
                (DurableCheckpointRestoreState.Verified,
                    DurableCheckpointRestoreState.Quarantined);

        private static void ValidateRecord(RestoreJournalRecord record, string fileName)
        {
            if (record.Version != CurrentVersion ||
                !string.Equals(fileName, record.ApprovalKeyHash + ".restore.json",
                    StringComparison.Ordinal) ||
                !IsLowerHex(record.ApprovalKeyHash, 64) ||
                !IsLowerHex(record.ScopeDigest, 64) ||
                !Enum.IsDefined(record.State) ||
                !IsLowerHex(record.CheckpointSha256, 64) || record.CheckpointByteLength < 6 ||
                !IsLowerHex(record.PostSha256, 64) || record.PostByteLength < 6 ||
                !IsSafeArtifactName(record.StageFileName, ".stage.dwg") ||
                !IsSafeArtifactName(record.BackupFileName, ".backup.dwg") ||
                string.IsNullOrEmpty(record.ProtectedTargetLocator) ||
                record.ProtectedTargetLocator.Length > 180_000)
            {
                throw new InvalidDataException("A restore journal entry failed validation.");
            }
        }

        private static bool IsSafeArtifactName(string value, string suffix) =>
            value.Length <= 128 &&
            value.StartsWith(".cad-harness-restore-", StringComparison.Ordinal) &&
            value.EndsWith(suffix, StringComparison.Ordinal) &&
            !Path.IsPathFullyQualified(value) &&
            string.Equals(Path.GetFileName(value), value, StringComparison.Ordinal) &&
            value.All(character => char.IsAsciiLetterOrDigit(character) ||
                character is '-' or '.');

        private static byte[] CombineDomainAndPayload(string domain, byte[] payload)
        {
            var prefix = Encoding.UTF8.GetBytes(domain + "\0");
            var combined = new byte[prefix.Length + payload.Length];
            Buffer.BlockCopy(prefix, 0, combined, 0, prefix.Length);
            Buffer.BlockCopy(payload, 0, combined, prefix.Length, payload.Length);
            return combined;
        }

        private static byte[] TargetLocatorAssociatedData(
            string approvalKeyHash,
            string scopeDigest) => Encoding.UTF8.GetBytes(string.Join(
                "\u001f",
                "cad-harness-restore-target-locator-v1",
                approvalKeyHash,
                scopeDigest));

        private static void RejectDuplicateProperties(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Object)
            {
                var names = new HashSet<string>(StringComparer.Ordinal);
                foreach (var property in element.EnumerateObject())
                {
                    if (!names.Add(property.Name))
                    {
                        throw new InvalidDataException(
                            "A restore journal entry contains duplicate properties.");
                    }

                    RejectDuplicateProperties(property.Value);
                }
            }
            else if (element.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in element.EnumerateArray())
                {
                    RejectDuplicateProperties(item);
                }
            }
        }

        private static bool IsLowerHex(string value, int length) =>
            value.Length == length && value.All(character =>
                character is >= '0' and <= '9' or >= 'a' and <= 'f');

        private void ThrowIfDisposed() => ObjectDisposedException.ThrowIf(_disposed, this);
    }

    private sealed record RestoreJournalRecord(
        int Version,
        string ApprovalKeyHash,
        string ScopeDigest,
        DurableCheckpointRestoreState State,
        string CheckpointSha256,
        long CheckpointByteLength,
        string PostSha256,
        long PostByteLength,
        string StageFileName,
        string BackupFileName,
        string ProtectedTargetLocator);

    private sealed record RestoreJournalEnvelope(
        RestoreJournalRecord Payload,
        string AuthenticationTag);
}
