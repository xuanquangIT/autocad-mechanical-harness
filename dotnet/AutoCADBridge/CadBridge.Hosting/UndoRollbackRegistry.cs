using System.Text.Json;

namespace CadBridge.Hosting;

/// <summary>In-memory lifecycle of the one AutoCAD undo group that may restore a commit.</summary>
public enum UndoRollbackState
{
    Available,
    Executing,
    Consumed,
    Quarantined,
    Superseded,
}

public enum UndoRollbackBeginKind
{
    Execute,
    Replay,
    Rejected,
    Conflict,
}

/// <summary>Fail-closed decision after an AutoCAD undo command observation.</summary>
public enum UndoRollbackRevisionFenceDecision
{
    Restored,
    RetryOnlyWhenUnchangedOnAttempt1,
    Quarantine,
}

/// <summary>
/// Pure bounded fence for the one permitted retry of an undo restore. It never guesses from a
/// third revision: anything other than the exact pre-commit or post-commit revision quarantines.
/// </summary>
public static class UndoRollbackRevisionFence
{
    public static UndoRollbackRevisionFenceDecision Decide(
        string observedRevision,
        string expectedPostCommitRevision,
        string targetPreviousRevision,
        int attempt)
    {
        ValidateRevision(observedRevision, nameof(observedRevision));
        ValidateRevision(expectedPostCommitRevision, nameof(expectedPostCommitRevision));
        ValidateRevision(targetPreviousRevision, nameof(targetPreviousRevision));
        if (attempt is < 1 or > 2)
        {
            return UndoRollbackRevisionFenceDecision.Quarantine;
        }

        if (string.Equals(observedRevision, targetPreviousRevision, StringComparison.Ordinal))
        {
            return UndoRollbackRevisionFenceDecision.Restored;
        }

        return string.Equals(observedRevision, expectedPostCommitRevision, StringComparison.Ordinal) &&
            attempt == 1
            ? UndoRollbackRevisionFenceDecision.RetryOnlyWhenUnchangedOnAttempt1
            : UndoRollbackRevisionFenceDecision.Quarantine;
    }

    private static void ValidateRevision(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Any(char.IsControl))
        {
            throw new ArgumentException("Revision must be non-empty safe text.", name);
        }
    }
}

/// <summary>
/// The opaque receipt held only by the AutoCAD process that created the matching undo group.
/// It is deliberately not durable: a restarted process has no authority to reactivate it.
/// </summary>
public sealed record UndoRollbackReceipt(
    string ReceiptId,
    string UndoGroup,
    string JobId,
    string DocumentId,
    string CheckpointId,
    string PreviousRevision,
    string NewRevision,
    string ProcessEpoch);

/// <summary>Verified rollback request identity. ApprovalId is the idempotency key.</summary>
public sealed record UndoRollbackRequest(
    string ReceiptId,
    string UndoGroup,
    string JobId,
    string DocumentId,
    string CheckpointId,
    string CurrentRevision,
    string ProcessEpoch,
    string ApprovalId,
    string RequestDigest);

/// <summary>Immutable result retained for an idempotent rollback replay.</summary>
public sealed record UndoRollbackResult(
    string ReceiptId,
    string JobId,
    string DocumentId,
    string CheckpointId,
    string PreviousRevision,
    string NewRevision,
    JsonElement Data);

public sealed record UndoRollbackBegin(
    UndoRollbackBeginKind Kind,
    UndoRollbackReceipt? Receipt = null,
    UndoRollbackResult? Result = null);

/// <summary>
/// Process-local authorization state for a one-step AutoCAD undo rollback. The registry does not
/// persist receipts and intentionally treats every restart as an unknown receipt. Once an AutoCAD
/// undo command may have run without a confirmed result, its receipt is quarantined permanently.
/// </summary>
public sealed class UndoRollbackRegistry
{
    private readonly object _gate = new();
    private readonly string _processEpoch;
    private readonly Dictionary<string, Entry> _entriesByReceipt = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _activeReceiptByDocument = new(StringComparer.Ordinal);
    private readonly Dictionary<string, ApprovalAttempt> _attemptByApprovalId = new(StringComparer.Ordinal);

    public UndoRollbackRegistry(string processEpoch)
    {
        ValidateText(processEpoch, nameof(processEpoch));
        _processEpoch = processEpoch;
    }

    public UndoRollbackReceipt Register(UndoRollbackReceipt receipt)
    {
        ArgumentNullException.ThrowIfNull(receipt);
        ValidateReceipt(receipt);
        if (!string.Equals(receipt.ProcessEpoch, _processEpoch, StringComparison.Ordinal))
        {
            throw new ArgumentException("Receipt belongs to a different process epoch.", nameof(receipt));
        }

        lock (_gate)
        {
            if (_entriesByReceipt.TryGetValue(receipt.ReceiptId, out var existing))
            {
                if (existing.Receipt != receipt)
                {
                    throw new InvalidOperationException("Rollback receipt id is already bound to another scope.");
                }

                return existing.Receipt;
            }

            if (_activeReceiptByDocument.TryGetValue(receipt.DocumentId, out var previousReceiptId) &&
                _entriesByReceipt.TryGetValue(previousReceiptId, out var previous))
            {
                if (previous.State == UndoRollbackState.Executing)
                {
                    throw new InvalidOperationException("Cannot supersede a rollback while its undo command is executing.");
                }

                previous.State = UndoRollbackState.Superseded;
            }

            _entriesByReceipt.Add(receipt.ReceiptId, new Entry(receipt));
            _activeReceiptByDocument[receipt.DocumentId] = receipt.ReceiptId;
            return receipt;
        }
    }

    public UndoRollbackBegin Begin(UndoRollbackRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateRequest(request);
        lock (_gate)
        {
            if (_attemptByApprovalId.TryGetValue(request.ApprovalId, out var priorAttempt))
            {
                if (!string.Equals(priorAttempt.RequestDigest, request.RequestDigest, StringComparison.Ordinal))
                {
                    return new UndoRollbackBegin(UndoRollbackBeginKind.Conflict);
                }

                if (!MatchesScope(priorAttempt.Request, request))
                {
                    return new UndoRollbackBegin(UndoRollbackBeginKind.Rejected);
                }

                return priorAttempt.Result is { } replay
                    ? new UndoRollbackBegin(UndoRollbackBeginKind.Replay, Result: CloneResult(replay))
                    : new UndoRollbackBegin(UndoRollbackBeginKind.Rejected);
            }

            if (!_entriesByReceipt.TryGetValue(request.ReceiptId, out var entry) ||
                !Matches(entry.Receipt, request) ||
                entry.State != UndoRollbackState.Available)
            {
                return new UndoRollbackBegin(UndoRollbackBeginKind.Rejected);
            }

            entry.State = UndoRollbackState.Executing;
            _attemptByApprovalId.Add(request.ApprovalId, new ApprovalAttempt(request, null));
            return new UndoRollbackBegin(UndoRollbackBeginKind.Execute, entry.Receipt);
        }
    }

    /// <summary>Stores a confirmed rollback result for replay and permanently consumes its undo group.</summary>
    public UndoRollbackResult CompleteSuccess(UndoRollbackRequest request, JsonElement resultData)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateRequest(request);
        if (resultData.ValueKind != JsonValueKind.Object)
        {
            throw new ArgumentException("Rollback result data must be an object.", nameof(resultData));
        }

        lock (_gate)
        {
            var entry = RequireExecutingAttempt(request);
            var receipt = entry.Receipt;
            var result = new UndoRollbackResult(
                receipt.ReceiptId,
                receipt.JobId,
                receipt.DocumentId,
                receipt.CheckpointId,
                receipt.PreviousRevision,
                receipt.NewRevision,
                resultData.Clone());
            entry.State = UndoRollbackState.Consumed;
            _attemptByApprovalId[request.ApprovalId] = new ApprovalAttempt(
                request,
                result);
            return CloneResult(result);
        }
    }

    /// <summary>
    /// Records that an AutoCAD command may have executed but its terminal state is not proven.
    /// This receipt can never execute again, even with another approval.
    /// </summary>
    public void QuarantineAfterCommandUncertainty(UndoRollbackRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateRequest(request);
        lock (_gate)
        {
            var entry = RequireExecutingAttempt(request);
            entry.State = UndoRollbackState.Quarantined;
        }
    }

    /// <summary>Releases an attempt only when no AutoCAD command has been issued.</summary>
    public void CancelBeforeCommand(UndoRollbackRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateRequest(request);
        lock (_gate)
        {
            var entry = RequireExecutingAttempt(request);
            entry.State = UndoRollbackState.Available;
            _attemptByApprovalId.Remove(request.ApprovalId);
        }
    }

    /// <summary>
    /// Invalidates an unused undo receipt when any unrelated AutoCAD command starts in its
    /// document. An executing command is left untouched so its caller can conclusively complete
    /// or quarantine it; a consumed receipt remains available only for replay of its own result.
    /// </summary>
    public bool InvalidateAvailableForDocument(string documentId)
    {
        ValidateText(documentId, nameof(documentId));
        lock (_gate)
        {
            if (!_activeReceiptByDocument.TryGetValue(documentId, out var receiptId) ||
                !_entriesByReceipt.TryGetValue(receiptId, out var entry) ||
                entry.State != UndoRollbackState.Available)
            {
                return false;
            }

            entry.State = UndoRollbackState.Superseded;
            return true;
        }
    }

    public UndoRollbackState? GetState(string receiptId)
    {
        ValidateText(receiptId, nameof(receiptId));
        lock (_gate)
        {
            return _entriesByReceipt.TryGetValue(receiptId, out var entry) ? entry.State : null;
        }
    }

    private Entry RequireExecutingAttempt(UndoRollbackRequest request)
    {
        if (!_entriesByReceipt.TryGetValue(request.ReceiptId, out var entry) ||
            entry.State != UndoRollbackState.Executing ||
            !Matches(entry.Receipt, request) ||
            !_attemptByApprovalId.TryGetValue(request.ApprovalId, out var attempt) ||
            !string.Equals(attempt.RequestDigest, request.RequestDigest, StringComparison.Ordinal) ||
            !MatchesScope(attempt.Request, request))
        {
            throw new InvalidOperationException("Rollback attempt is not executing for this exact scope.");
        }

        return entry;
    }

    private static bool Matches(UndoRollbackReceipt receipt, UndoRollbackRequest request) =>
        string.Equals(receipt.ReceiptId, request.ReceiptId, StringComparison.Ordinal) &&
        string.Equals(receipt.UndoGroup, request.UndoGroup, StringComparison.Ordinal) &&
        string.Equals(receipt.JobId, request.JobId, StringComparison.Ordinal) &&
        string.Equals(receipt.DocumentId, request.DocumentId, StringComparison.Ordinal) &&
        string.Equals(receipt.CheckpointId, request.CheckpointId, StringComparison.Ordinal) &&
        string.Equals(receipt.NewRevision, request.CurrentRevision, StringComparison.Ordinal) &&
        string.Equals(receipt.ProcessEpoch, request.ProcessEpoch, StringComparison.Ordinal);

    private static bool MatchesScope(UndoRollbackRequest left, UndoRollbackRequest right) =>
        string.Equals(left.ReceiptId, right.ReceiptId, StringComparison.Ordinal) &&
        string.Equals(left.UndoGroup, right.UndoGroup, StringComparison.Ordinal) &&
        string.Equals(left.JobId, right.JobId, StringComparison.Ordinal) &&
        string.Equals(left.DocumentId, right.DocumentId, StringComparison.Ordinal) &&
        string.Equals(left.CheckpointId, right.CheckpointId, StringComparison.Ordinal) &&
        string.Equals(left.CurrentRevision, right.CurrentRevision, StringComparison.Ordinal) &&
        string.Equals(left.ProcessEpoch, right.ProcessEpoch, StringComparison.Ordinal) &&
        string.Equals(left.ApprovalId, right.ApprovalId, StringComparison.Ordinal);

    private static UndoRollbackResult CloneResult(UndoRollbackResult result) =>
        result with { Data = result.Data.Clone() };

    private static void ValidateReceipt(UndoRollbackReceipt receipt)
    {
        ValidateText(receipt.ReceiptId, nameof(receipt.ReceiptId));
        ValidateText(receipt.UndoGroup, nameof(receipt.UndoGroup));
        ValidateText(receipt.JobId, nameof(receipt.JobId));
        ValidateText(receipt.DocumentId, nameof(receipt.DocumentId));
        ValidateText(receipt.CheckpointId, nameof(receipt.CheckpointId));
        ValidateText(receipt.PreviousRevision, nameof(receipt.PreviousRevision));
        ValidateText(receipt.NewRevision, nameof(receipt.NewRevision));
        ValidateText(receipt.ProcessEpoch, nameof(receipt.ProcessEpoch));
    }

    private static void ValidateRequest(UndoRollbackRequest request)
    {
        ValidateText(request.ReceiptId, nameof(request.ReceiptId));
        ValidateText(request.UndoGroup, nameof(request.UndoGroup));
        ValidateText(request.JobId, nameof(request.JobId));
        ValidateText(request.DocumentId, nameof(request.DocumentId));
        ValidateText(request.CheckpointId, nameof(request.CheckpointId));
        ValidateText(request.CurrentRevision, nameof(request.CurrentRevision));
        ValidateText(request.ProcessEpoch, nameof(request.ProcessEpoch));
        ValidateText(request.ApprovalId, nameof(request.ApprovalId));
        ValidateText(request.RequestDigest, nameof(request.RequestDigest));
    }

    private static void ValidateText(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Any(char.IsControl))
        {
            throw new ArgumentException("Rollback state identifiers must be non-empty safe text.", name);
        }
    }

    private sealed class Entry
    {
        public Entry(UndoRollbackReceipt receipt) => Receipt = receipt;

        public UndoRollbackReceipt Receipt { get; }

        public UndoRollbackState State { get; set; } = UndoRollbackState.Available;
    }

    private sealed record ApprovalAttempt(UndoRollbackRequest Request, UndoRollbackResult? Result)
    {
        public string RequestDigest => Request.RequestDigest;
    }
}
