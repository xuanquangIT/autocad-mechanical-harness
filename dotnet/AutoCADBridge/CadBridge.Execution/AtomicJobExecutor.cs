using CadBridge.Contracts;

namespace CadBridge.Execution;

/// <summary>
/// A typed operation accepted by the atomic executor. Concrete bridge operation contracts implement
/// this interface; the executor never evaluates command strings, reflection metadata, or dynamic values.
/// </summary>
public interface IAtomicJobOperation
{
    string OperationId { get; }
}

/// <summary>
/// Marshals one callback into AutoCAD's command/document context. The production implementation must
/// keep the callback on the host context for its complete lifetime.
/// </summary>
public interface ICommandContextMarshaller
{
    ValueTask<TResult> ExecuteAsync<TResult>(
        Func<CancellationToken, ValueTask<TResult>> callback,
        CancellationToken cancellationToken);
}

/// <summary>
/// A host already bound to exactly one target document. Binding is performed before this execution
/// layer, so this interface cannot select another document or execute an arbitrary host command.
/// </summary>
public interface IAtomicDocumentHost
{
    IDocumentLock AcquireDocumentLock();

    IUndoGroup BeginUndoGroup();

    IAtomicTransaction BeginTransaction();
}

/// <summary>A held lock for the target document.</summary>
public interface IDocumentLock : IDisposable;

/// <summary>
/// One undo group for the complete job. Dispose only releases host resources; it must never implicitly
/// complete or roll back the group.
/// </summary>
public interface IUndoGroup : IDisposable
{
    void Complete();

    void Rollback();
}

/// <summary>
/// One database transaction for the complete job. Dispose only releases host resources; it must never
/// implicitly commit or abort the transaction.
/// </summary>
public interface IAtomicTransaction : IDisposable
{
    void Commit();

    void Abort();
}

/// <summary>Dispatches one typed operation inside the current transaction.</summary>
public delegate ValueTask AtomicOperationDispatcher(
    IAtomicJobOperation operation,
    IAtomicTransaction transaction,
    CancellationToken cancellationToken);

/// <summary>Runs all pre-commit validation inside the same transaction.</summary>
public delegate ValueTask AtomicPreCommitValidator(
    IAtomicTransaction transaction,
    CancellationToken cancellationToken);

/// <summary>
/// Validates the already-bound document while its lock is held and before an undo group or write
/// transaction exists. This closes the revision race between an external preflight and the first
/// database mutation.
/// </summary>
public delegate ValueTask AtomicLockedDocumentValidator(CancellationToken cancellationToken);

/// <summary>
/// Reads committed state while the same document lock is still held. The callback receives no live
/// cancellation token because the database commit boundary has already been crossed.
/// </summary>
public delegate ValueTask AtomicPostCommitObserver(CancellationToken cancellationToken);

public enum AtomicExecutionOutcome
{
    Committed,
    Failed,
    UnknownCommitState,
}

public enum AtomicExecutionStage
{
    CommandContext,
    DocumentLock,
    LockedDocumentValidation,
    UndoGroup,
    Transaction,
    Operation,
    Validation,
    Commit,
    PostCommitInspection,
    UndoCompletion,
    Completed,
}

public enum AtomicFailureKind
{
    None,
    Cancelled,
    HostFailure,
    OperationFailure,
    ValidationFailure,
    RollbackFailure,
    UnknownCommitState,
}

/// <summary>Observable lifecycle counters intended for contract tests and production telemetry.</summary>
public sealed record AtomicExecutionTrace(
    AtomicExecutionStage Stage,
    int CommandContextEntries,
    int DocumentLocksAcquired,
    int UndoGroupsStarted,
    int TransactionsStarted,
    int OperationsDispatched,
    int CancellationCheckpoints,
    int TransactionCommitsStarted,
    int TransactionCommitsCompleted,
    int TransactionAborts,
    int UndoGroupsCompleted,
    int UndoGroupsRolledBack);

/// <summary>A safe terminal result. Error messages never include host exceptions, paths, or stacks.</summary>
public sealed record AtomicExecutionResult(
    AtomicExecutionOutcome Outcome,
    AtomicFailureKind FailureKind,
    AtomicExecutionTrace Trace,
    IpcError? Error)
{
    public bool IsCommitted => Outcome == AtomicExecutionOutcome.Committed;
}

/// <summary>
/// Executes a complete job under exactly one document lock, undo group, and transaction. All failures
/// before the irreversible commit boundary are rolled back. Once Commit is invoked, any failure is
/// conservatively classified as UNKNOWN_COMMIT_STATE and must be reconciled by the caller.
/// </summary>
public sealed class AtomicJobExecutor
{
    private const string FailedCode = "ATOMIC_JOB_FAILED";
    private const string CancelledCode = "IPC_TIMEOUT";
    private const string UnknownCommitCode = "UNKNOWN_COMMIT_STATE";

    private readonly ICommandContextMarshaller _commandContext;
    private readonly IAtomicDocumentHost _document;

    public AtomicJobExecutor(
        ICommandContextMarshaller commandContext,
        IAtomicDocumentHost document)
    {
        ArgumentNullException.ThrowIfNull(commandContext);
        ArgumentNullException.ThrowIfNull(document);
        _commandContext = commandContext;
        _document = document;
    }

    public async ValueTask<AtomicExecutionResult> ExecuteAsync(
        IReadOnlyList<IAtomicJobOperation> operations,
        AtomicOperationDispatcher dispatch,
        AtomicPreCommitValidator validate,
        CancellationToken cancellationToken = default)
    {
        return await ExecuteAsync(
            operations,
            dispatch,
            validateLockedDocument: null,
            validate,
            cancellationToken);
    }

    public async ValueTask<AtomicExecutionResult> ExecuteAsync(
        IReadOnlyList<IAtomicJobOperation> operations,
        AtomicOperationDispatcher dispatch,
        AtomicLockedDocumentValidator? validateLockedDocument,
        AtomicPreCommitValidator validate,
        CancellationToken cancellationToken = default)
    {
        return await ExecuteAsync(
            operations,
            dispatch,
            validateLockedDocument,
            validate,
            observeCommittedDocument: null,
            cancellationToken);
    }

    public async ValueTask<AtomicExecutionResult> ExecuteAsync(
        IReadOnlyList<IAtomicJobOperation> operations,
        AtomicOperationDispatcher dispatch,
        AtomicLockedDocumentValidator? validateLockedDocument,
        AtomicPreCommitValidator validate,
        AtomicPostCommitObserver? observeCommittedDocument,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(operations);
        ArgumentNullException.ThrowIfNull(dispatch);
        ArgumentNullException.ThrowIfNull(validate);
        if (operations.Any(operation => operation is null))
        {
            throw new ArgumentException("Operations cannot contain null values.", nameof(operations));
        }

        var state = new ExecutionState();
        try
        {
            return await _commandContext.ExecuteAsync(
                token => ExecuteInCommandContextAsync(
                    operations,
                    dispatch,
                    validateLockedDocument,
                    validate,
                    observeCommittedDocument,
                    state,
                    token),
                cancellationToken);
        }
        catch (OperationCanceledException)
        {
            return state.CommitStarted
                ? Unknown(state)
                : Failure(state, AtomicFailureKind.Cancelled, CancelledCode, retryable: true);
        }
        catch (Exception)
        {
            return state.CommitStarted
                ? Unknown(state)
                : Failure(state, AtomicFailureKind.HostFailure, FailedCode);
        }
    }

    private async ValueTask<AtomicExecutionResult> ExecuteInCommandContextAsync(
        IReadOnlyList<IAtomicJobOperation> operations,
        AtomicOperationDispatcher dispatch,
        AtomicLockedDocumentValidator? validateLockedDocument,
        AtomicPreCommitValidator validate,
        AtomicPostCommitObserver? observeCommittedDocument,
        ExecutionState state,
        CancellationToken cancellationToken)
    {
        state.CommandContextEntries++;
        IDocumentLock? documentLock = null;
        IUndoGroup? undoGroup = null;
        IAtomicTransaction? transaction = null;

        try
        {
            state.Stage = AtomicExecutionStage.DocumentLock;
            documentLock = _document.AcquireDocumentLock()
                ?? throw new InvalidOperationException("The document host returned no document lock.");
            state.DocumentLocksAcquired++;

            if (validateLockedDocument is not null)
            {
                state.Stage = AtomicExecutionStage.LockedDocumentValidation;
                Checkpoint(state, cancellationToken);
                await validateLockedDocument(cancellationToken);
            }

            state.Stage = AtomicExecutionStage.UndoGroup;
            undoGroup = _document.BeginUndoGroup()
                ?? throw new InvalidOperationException("The document host returned no undo group.");
            state.UndoGroupsStarted++;

            state.Stage = AtomicExecutionStage.Transaction;
            transaction = _document.BeginTransaction()
                ?? throw new InvalidOperationException("The document host returned no transaction.");
            state.TransactionsStarted++;

            foreach (var operation in operations)
            {
                state.Stage = AtomicExecutionStage.Operation;
                Checkpoint(state, cancellationToken);
                await dispatch(operation, transaction, cancellationToken);
                state.OperationsDispatched++;
            }

            state.Stage = AtomicExecutionStage.Validation;
            Checkpoint(state, cancellationToken);
            await validate(transaction, cancellationToken);

            // This is the final cancellable point. Commit itself intentionally receives no token:
            // cancellation after this checkpoint cannot safely prove whether AutoCAD persisted the job.
            state.Stage = AtomicExecutionStage.Commit;
            Checkpoint(state, cancellationToken);
            state.CommitStarted = true;
            state.TransactionCommitsStarted++;
            transaction.Commit();
            state.TransactionCommitsCompleted++;

            if (observeCommittedDocument is not null)
            {
                state.Stage = AtomicExecutionStage.PostCommitInspection;
                await observeCommittedDocument(CancellationToken.None);
            }

            state.Stage = AtomicExecutionStage.UndoCompletion;
            undoGroup.Complete();
            state.UndoGroupsCompleted++;
            state.Stage = AtomicExecutionStage.Completed;
            return new AtomicExecutionResult(
                AtomicExecutionOutcome.Committed,
                AtomicFailureKind.None,
                state.Snapshot(),
                null);
        }
        catch (Exception exception) when (!state.CommitStarted)
        {
            var failureKind = ClassifyPreCommitFailure(state.Stage, exception);
            return RollBackBeforeCommit(transaction, undoGroup, state, failureKind);
        }
        catch (Exception)
        {
            // Commit has started. Calling Abort or Undo here could corrupt a successfully committed job.
            return Unknown(state);
        }
        finally
        {
            // Nested finally blocks guarantee every successfully acquired host scope is disposed
            // exactly once even when an earlier Dispose implementation itself fails.
            try
            {
                transaction?.Dispose();
            }
            finally
            {
                try
                {
                    undoGroup?.Dispose();
                }
                finally
                {
                    documentLock?.Dispose();
                }
            }
        }
    }

    private static AtomicExecutionResult RollBackBeforeCommit(
        IAtomicTransaction? transaction,
        IUndoGroup? undoGroup,
        ExecutionState state,
        AtomicFailureKind failureKind)
    {
        var rollbackFailed = false;
        if (transaction is not null)
        {
            try
            {
                transaction.Abort();
                state.TransactionAborts++;
            }
            catch (Exception)
            {
                rollbackFailed = true;
            }
        }

        if (undoGroup is not null)
        {
            try
            {
                undoGroup.Rollback();
                state.UndoGroupsRolledBack++;
            }
            catch (Exception)
            {
                rollbackFailed = true;
            }
        }

        if (rollbackFailed)
        {
            // Commit has not started, so this is a failed cleanup rather than an unknown
            // commit outcome. The host scopes are still disposed by the caller's finally.
            return Failure(state, AtomicFailureKind.RollbackFailure, FailedCode);
        }

        var code = failureKind == AtomicFailureKind.Cancelled ? CancelledCode : FailedCode;
        return Failure(state, failureKind, code, retryable: failureKind == AtomicFailureKind.Cancelled);
    }

    private static AtomicFailureKind ClassifyPreCommitFailure(
        AtomicExecutionStage stage,
        Exception exception)
    {
        if (exception is OperationCanceledException)
        {
            return AtomicFailureKind.Cancelled;
        }

        return stage switch
        {
            AtomicExecutionStage.Operation => AtomicFailureKind.OperationFailure,
            AtomicExecutionStage.LockedDocumentValidation => AtomicFailureKind.ValidationFailure,
            AtomicExecutionStage.Validation => AtomicFailureKind.ValidationFailure,
            _ => AtomicFailureKind.HostFailure,
        };
    }

    private static void Checkpoint(ExecutionState state, CancellationToken cancellationToken)
    {
        state.CancellationCheckpoints++;
        cancellationToken.ThrowIfCancellationRequested();
    }

    private static AtomicExecutionResult Failure(
        ExecutionState state,
        AtomicFailureKind failureKind,
        string code,
        bool retryable = false)
    {
        var outcome = code == UnknownCommitCode
            ? AtomicExecutionOutcome.UnknownCommitState
            : AtomicExecutionOutcome.Failed;
        return new AtomicExecutionResult(
            outcome,
            failureKind,
            state.Snapshot(),
            new IpcError(code, SafeMessage(code))
            {
                Retryable = retryable,
                RequiredAction = code == UnknownCommitCode
                    ? "Reconcile the job before any further commit attempt."
                    : null,
            });
    }

    private static AtomicExecutionResult Unknown(ExecutionState state) =>
        Failure(state, AtomicFailureKind.UnknownCommitState, UnknownCommitCode);

    private static string SafeMessage(string code) => code switch
    {
        CancelledCode => "The atomic job was cancelled before commit.",
        UnknownCommitCode => "The commit outcome is unknown and requires reconciliation.",
        _ => "The atomic job failed safely before commit.",
    };

    private sealed class ExecutionState
    {
        public AtomicExecutionStage Stage { get; set; } = AtomicExecutionStage.CommandContext;

        public int CommandContextEntries { get; set; }

        public int DocumentLocksAcquired { get; set; }

        public int UndoGroupsStarted { get; set; }

        public int TransactionsStarted { get; set; }

        public int OperationsDispatched { get; set; }

        public int CancellationCheckpoints { get; set; }

        public int TransactionCommitsStarted { get; set; }

        public int TransactionCommitsCompleted { get; set; }

        public int TransactionAborts { get; set; }

        public int UndoGroupsCompleted { get; set; }

        public int UndoGroupsRolledBack { get; set; }

        public bool CommitStarted { get; set; }

        public AtomicExecutionTrace Snapshot() => new(
            Stage,
            CommandContextEntries,
            DocumentLocksAcquired,
            UndoGroupsStarted,
            TransactionsStarted,
            OperationsDispatched,
            CancellationCheckpoints,
            TransactionCommitsStarted,
            TransactionCommitsCompleted,
            TransactionAborts,
            UndoGroupsCompleted,
            UndoGroupsRolledBack);
    }
}
