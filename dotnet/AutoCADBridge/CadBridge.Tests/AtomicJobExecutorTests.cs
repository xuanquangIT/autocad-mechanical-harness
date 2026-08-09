using CadBridge.Execution;
using Xunit;

namespace CadBridge.Tests;

public sealed class AtomicJobExecutorTests
{
    [Fact]
    public async Task SuccessUsesExactlyOneAtomicScopeAndCommit()
    {
        var host = new RecordingDocumentHost();
        var executor = CreateExecutor(host);
        var operations = Operations(3);
        var validationCalls = 0;

        var result = await executor.ExecuteAsync(
            operations,
            StageOperation,
            (transaction, _) =>
            {
                validationCalls++;
                Assert.Same(host.Transaction, transaction);
                return ValueTask.CompletedTask;
            });

        Assert.Equal(AtomicExecutionOutcome.Committed, result.Outcome);
        Assert.Equal(AtomicFailureKind.None, result.FailureKind);
        Assert.Null(result.Error);
        Assert.True(result.IsCommitted);
        Assert.Equal(1, validationCalls);
        Assert.Equal(3, host.Transaction.CommittedEntities);
        AssertScopeCounts(result.Trace, host, commitsStarted: 1, commitsCompleted: 1);
        Assert.Equal(3, result.Trace.OperationsDispatched);
        Assert.Equal(5, result.Trace.CancellationCheckpoints);
        Assert.Equal(1, result.Trace.UndoGroupsCompleted);
        Assert.Equal(0, result.Trace.TransactionAborts);
        Assert.Equal(0, result.Trace.UndoGroupsRolledBack);
    }

    [Theory]
    [InlineData(AcquisitionFailurePoint.DocumentLock)]
    [InlineData(AcquisitionFailurePoint.UndoGroup)]
    [InlineData(AcquisitionFailurePoint.Transaction)]
    public async Task AcquisitionFailureTerminatesEveryPreviouslyAcquiredScopeExactlyOnce(
        AcquisitionFailurePoint failurePoint)
    {
        var host = new RecordingDocumentHost { AcquisitionFailure = failurePoint };
        var result = await CreateExecutor(host).ExecuteAsync(
            Operations(1),
            StageOperation,
            (_, _) => ValueTask.CompletedTask);

        AssertSafeFailure(result, AtomicFailureKind.HostFailure);
        Assert.Equal(0, result.Trace.TransactionCommitsStarted);
        Assert.Equal(0, host.Transaction.CommitCalls);
        Assert.Equal(0, host.Transaction.AbortCalls);

        Assert.Equal(1, host.LockAcquireCalls);
        Assert.Equal(failurePoint == AcquisitionFailurePoint.DocumentLock ? 0 : 1,
            result.Trace.DocumentLocksAcquired);
        Assert.Equal(failurePoint == AcquisitionFailurePoint.DocumentLock ? 0 : 1,
            host.DocumentLock.DisposeCalls);

        Assert.Equal(failurePoint == AcquisitionFailurePoint.DocumentLock ? 0 : 1,
            host.UndoGroupBeginCalls);
        Assert.Equal(failurePoint == AcquisitionFailurePoint.Transaction ? 1 : 0,
            result.Trace.UndoGroupsStarted);
        Assert.Equal(failurePoint == AcquisitionFailurePoint.Transaction ? 1 : 0,
            host.UndoGroup.RollbackCalls);
        Assert.Equal(failurePoint == AcquisitionFailurePoint.Transaction ? 1 : 0,
            host.UndoGroup.DisposeCalls);

        Assert.Equal(failurePoint == AcquisitionFailurePoint.Transaction ? 1 : 0,
            host.TransactionBeginCalls);
        Assert.Equal(0, result.Trace.TransactionsStarted);
        Assert.Equal(0, host.Transaction.DisposeCalls);
    }

    [Fact]
    public async Task AcquisitionRollbackFailureIsFailedNotUnknownAndStillDisposesScopes()
    {
        var host = new RecordingDocumentHost
        {
            AcquisitionFailure = AcquisitionFailurePoint.Transaction,
            UndoGroup = { ThrowOnRollback = true },
        };

        var result = await CreateExecutor(host).ExecuteAsync(
            Operations(1),
            StageOperation,
            (_, _) => ValueTask.CompletedTask);

        Assert.Equal(AtomicExecutionOutcome.Failed, result.Outcome);
        Assert.Equal(AtomicFailureKind.RollbackFailure, result.FailureKind);
        Assert.NotNull(result.Error);
        Assert.Equal("ATOMIC_JOB_FAILED", result.Error.Code);
        Assert.Equal(1, host.UndoGroup.RollbackCalls);
        Assert.Equal(1, host.UndoGroup.DisposeCalls);
        Assert.Equal(1, host.DocumentLock.DisposeCalls);
        Assert.Equal(0, host.Transaction.DisposeCalls);
        Assert.Equal(0, result.Trace.TransactionCommitsStarted);
    }

    [Fact]
    public async Task LockedDocumentValidationFailurePreventsUndoTransactionAndOperations()
    {
        var host = new RecordingDocumentHost();
        var dispatchCalls = 0;
        var result = await CreateExecutor(host).ExecuteAsync(
            Operations(1),
            (_, _, _) =>
            {
                dispatchCalls++;
                return ValueTask.CompletedTask;
            },
            _ => throw new ExpectedTestException(),
            (_, _) => ValueTask.CompletedTask);

        AssertSafeFailure(result, AtomicFailureKind.ValidationFailure);
        Assert.Equal(AtomicExecutionStage.LockedDocumentValidation, result.Trace.Stage);
        Assert.Equal(1, host.LockAcquireCalls);
        Assert.Equal(1, host.DocumentLock.DisposeCalls);
        Assert.Equal(0, host.UndoGroupBeginCalls);
        Assert.Equal(0, host.TransactionBeginCalls);
        Assert.Equal(0, dispatchCalls);
        Assert.Equal(0, result.Trace.TransactionCommitsStarted);
    }

    [Fact]
    public async Task PostCommitInspectionRunsAfterCommitAndBeforeUndoAndLockRelease()
    {
        var host = new RecordingDocumentHost();
        var observerCalls = 0;
        var result = await CreateExecutor(host).ExecuteAsync(
            Operations(1),
            StageOperation,
            validateLockedDocument: null,
            (_, _) => ValueTask.CompletedTask,
            _ =>
            {
                observerCalls++;
                Assert.Equal(1, host.Transaction.CommitCalls);
                Assert.Equal(0, host.UndoGroup.CompleteCalls);
                Assert.Equal(0, host.DocumentLock.DisposeCalls);
                return ValueTask.CompletedTask;
            });

        Assert.Equal(AtomicExecutionOutcome.Committed, result.Outcome);
        Assert.Equal(1, observerCalls);
        Assert.Equal(1, host.UndoGroup.CompleteCalls);
        Assert.Equal(1, host.DocumentLock.DisposeCalls);
    }

    [Fact]
    public async Task Property13_JobWriteIsAtomicAtEveryGeneratedFailurePoint()
    {
        const int exampleCount = 160;
        var random = new Random(0x13_2026);

        for (var example = 0; example < exampleCount; example++)
        {
            var operationCount = random.Next(1, 17);
            var failurePoint = (Property13FailurePoint)(example % 4);
            var failingOperation = random.Next(operationCount);
            var host = new RecordingDocumentHost
            {
                Transaction =
                {
                    ThrowOnCommit = failurePoint == Property13FailurePoint.CommitBeforePersistence,
                    ThrowAfterCommit = failurePoint == Property13FailurePoint.CommitAfterPersistence,
                },
            };
            var dispatchCalls = 0;
            var validationCalls = 0;

            var result = await CreateExecutor(host).ExecuteAsync(
                Operations(operationCount),
                (_, transaction, _) =>
                {
                    var operationIndex = dispatchCalls++;
                    ((RecordingTransaction)transaction).StageEntity();
                    if (failurePoint == Property13FailurePoint.Operation &&
                        operationIndex == failingOperation)
                    {
                        throw new ExpectedTestException();
                    }

                    return ValueTask.CompletedTask;
                },
                (_, _) =>
                {
                    validationCalls++;
                    return failurePoint == Property13FailurePoint.Validation
                        ? throw new ExpectedTestException()
                        : ValueTask.CompletedTask;
                });

            var failedBeforeCommit = failurePoint is
                Property13FailurePoint.Operation or Property13FailurePoint.Validation;
            var expectedDispatchCalls = failurePoint == Property13FailurePoint.Operation
                ? failingOperation + 1
                : operationCount;
            var expectedCompletedDispatches = failurePoint == Property13FailurePoint.Operation
                ? failingOperation
                : operationCount;
            var expectedValidationCalls = failurePoint == Property13FailurePoint.Operation ? 0 : 1;

            Assert.Equal(expectedDispatchCalls, dispatchCalls);
            Assert.Equal(expectedCompletedDispatches, result.Trace.OperationsDispatched);
            Assert.Equal(expectedValidationCalls, validationCalls);
            Assert.Equal(1, host.Transaction.DisposeCalls);
            Assert.Equal(1, host.UndoGroup.DisposeCalls);
            Assert.Equal(1, host.DocumentLock.DisposeCalls);

            if (failedBeforeCommit)
            {
                var expectedFailureKind = failurePoint == Property13FailurePoint.Operation
                    ? AtomicFailureKind.OperationFailure
                    : AtomicFailureKind.ValidationFailure;
                AssertSafeFailure(result, expectedFailureKind);
                Assert.Equal(0, host.Transaction.CommittedEntities);
                Assert.Equal(0, host.Transaction.StagedEntities);
                Assert.Equal(0, host.Transaction.CommitCalls);
                Assert.Equal(1, host.Transaction.AbortCalls);
                Assert.Equal(1, host.UndoGroup.RollbackCalls);
                continue;
            }

            Assert.Equal(AtomicExecutionOutcome.UnknownCommitState, result.Outcome);
            Assert.Equal(AtomicFailureKind.UnknownCommitState, result.FailureKind);
            AssertUnknownError(result);
            Assert.Equal(1, host.Transaction.CommitCalls);
            Assert.Equal(0, host.Transaction.AbortCalls);
            Assert.Equal(0, host.UndoGroup.RollbackCalls);

            var expectedCommittedEntities = failurePoint == Property13FailurePoint.CommitAfterPersistence
                ? operationCount
                : 0;
            var expectedStagedEntities = operationCount - expectedCommittedEntities;
            Assert.Equal(expectedCommittedEntities, host.Transaction.CommittedEntities);
            Assert.Equal(expectedStagedEntities, host.Transaction.StagedEntities);
            Assert.True(
                host.Transaction.CommittedEntities is 0 ||
                host.Transaction.CommittedEntities == operationCount,
                $"Example {example} left a partial commit.");
        }
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(2)]
    public async Task FailureAtEachOperationRollsBackEverything(int failingOperationIndex)
    {
        var host = new RecordingDocumentHost();
        var executor = CreateExecutor(host);
        var dispatchCalls = 0;
        var validationCalls = 0;

        var result = await executor.ExecuteAsync(
            Operations(3),
            (operation, transaction, _) =>
            {
                var currentIndex = dispatchCalls++;
                ((RecordingTransaction)transaction).StageEntity();
                Assert.Equal($"operation-{currentIndex}", operation.OperationId);
                if (currentIndex == failingOperationIndex)
                {
                    throw new ExpectedTestException();
                }

                return ValueTask.CompletedTask;
            },
            (_, _) =>
            {
                validationCalls++;
                return ValueTask.CompletedTask;
            });

        AssertSafeFailure(result, AtomicFailureKind.OperationFailure);
        Assert.Equal(failingOperationIndex + 1, dispatchCalls);
        Assert.Equal(failingOperationIndex, result.Trace.OperationsDispatched);
        Assert.Equal(0, validationCalls);
        Assert.Equal(0, host.Transaction.CommittedEntities);
        Assert.Equal(0, host.Transaction.StagedEntities);
        AssertScopeCounts(result.Trace, host, commitsStarted: 0, commitsCompleted: 0);
        Assert.Equal(1, result.Trace.TransactionAborts);
        Assert.Equal(1, result.Trace.UndoGroupsRolledBack);
    }

    [Theory]
    [InlineData(1, 0, 0)]
    [InlineData(2, 1, 0)]
    [InlineData(3, 2, 0)]
    [InlineData(4, 3, 0)]
    [InlineData(5, 3, 1)]
    public async Task CancellationBeforeEveryCheckpointRollsBackWithoutCommit(
        int cancelledCheckpoint,
        int expectedDispatches,
        int expectedValidations)
    {
        using var cancellation = new CancellationTokenSource();
        var host = new RecordingDocumentHost();
        var executor = CreateExecutor(host);
        var dispatchCalls = 0;
        var validationCalls = 0;
        if (cancelledCheckpoint == 1)
        {
            cancellation.Cancel();
        }

        var result = await executor.ExecuteAsync(
            Operations(3),
            (_, transaction, _) =>
            {
                dispatchCalls++;
                ((RecordingTransaction)transaction).StageEntity();
                if (cancelledCheckpoint == dispatchCalls + 1)
                {
                    cancellation.Cancel();
                }

                return ValueTask.CompletedTask;
            },
            (_, _) =>
            {
                validationCalls++;
                if (cancelledCheckpoint == 5)
                {
                    cancellation.Cancel();
                }

                return ValueTask.CompletedTask;
            },
            cancellation.Token);

        Assert.Equal(AtomicExecutionOutcome.Failed, result.Outcome);
        Assert.Equal(AtomicFailureKind.Cancelled, result.FailureKind);
        Assert.False(result.IsCommitted);
        Assert.NotNull(result.Error);
        Assert.Equal("IPC_TIMEOUT", result.Error.Code);
        Assert.True(result.Error.Retryable);
        Assert.Equal(cancelledCheckpoint, result.Trace.CancellationCheckpoints);
        Assert.Equal(expectedDispatches, dispatchCalls);
        Assert.Equal(expectedDispatches, result.Trace.OperationsDispatched);
        Assert.Equal(expectedValidations, validationCalls);
        Assert.Equal(0, host.Transaction.CommittedEntities);
        Assert.Equal(0, host.Transaction.StagedEntities);
        AssertScopeCounts(result.Trace, host, commitsStarted: 0, commitsCompleted: 0);
        Assert.Equal(1, result.Trace.TransactionAborts);
        Assert.Equal(1, result.Trace.UndoGroupsRolledBack);
    }

    [Fact]
    public async Task ValidationFailureRollsBackWithoutCommit()
    {
        var host = new RecordingDocumentHost();
        var executor = CreateExecutor(host);

        var result = await executor.ExecuteAsync(
            Operations(2),
            StageOperation,
            (_, _) => throw new ExpectedTestException());

        AssertSafeFailure(result, AtomicFailureKind.ValidationFailure);
        Assert.Equal(2, result.Trace.OperationsDispatched);
        Assert.Equal(0, host.Transaction.CommittedEntities);
        Assert.Equal(0, host.Transaction.StagedEntities);
        AssertScopeCounts(result.Trace, host, commitsStarted: 0, commitsCompleted: 0);
        Assert.Equal(1, result.Trace.TransactionAborts);
        Assert.Equal(1, result.Trace.UndoGroupsRolledBack);
    }

    [Fact]
    public async Task AbortFailureBeforeCommitRemainsFailedAndStillRollsBackUndoGroup()
    {
        var host = new RecordingDocumentHost
        {
            Transaction = { ThrowOnAbort = true },
        };
        var executor = CreateExecutor(host);

        var result = await executor.ExecuteAsync(
            Operations(1),
            (_, _, _) => throw new ExpectedTestException(),
            (_, _) => ValueTask.CompletedTask);

        Assert.Equal(AtomicExecutionOutcome.Failed, result.Outcome);
        Assert.Equal(AtomicFailureKind.RollbackFailure, result.FailureKind);
        Assert.NotNull(result.Error);
        Assert.Equal("ATOMIC_JOB_FAILED", result.Error.Code);
        AssertScopeCounts(result.Trace, host, commitsStarted: 0, commitsCompleted: 0);
        Assert.Equal(1, host.Transaction.AbortCalls);
        Assert.Equal(0, result.Trace.TransactionAborts);
        Assert.Equal(1, host.UndoGroup.RollbackCalls);
        Assert.Equal(1, result.Trace.UndoGroupsRolledBack);
        Assert.Equal(0, host.Transaction.CommittedEntities);
    }

    [Fact]
    public async Task CommitThrowIsUnknownAndNeverRetriesOrRollsBack()
    {
        var host = new RecordingDocumentHost
        {
            Transaction = { ThrowOnCommit = true },
        };
        var executor = CreateExecutor(host);

        var result = await executor.ExecuteAsync(
            Operations(2),
            StageOperation,
            (_, _) => ValueTask.CompletedTask);

        Assert.Equal(AtomicExecutionOutcome.UnknownCommitState, result.Outcome);
        Assert.Equal(AtomicFailureKind.UnknownCommitState, result.FailureKind);
        AssertUnknownError(result);
        AssertScopeCounts(result.Trace, host, commitsStarted: 1, commitsCompleted: 0);
        Assert.Equal(1, host.Transaction.CommitCalls);
        AssertNoRollback(result.Trace, host);
    }

    [Fact]
    public async Task UndoCompleteThrowAfterCommitIsUnknownAndNeverRetriesOrRollsBack()
    {
        var host = new RecordingDocumentHost
        {
            UndoGroup = { ThrowOnComplete = true },
        };
        var executor = CreateExecutor(host);

        var result = await executor.ExecuteAsync(
            Operations(2),
            StageOperation,
            (_, _) => ValueTask.CompletedTask);

        Assert.Equal(AtomicExecutionOutcome.UnknownCommitState, result.Outcome);
        Assert.Equal(AtomicFailureKind.UnknownCommitState, result.FailureKind);
        AssertUnknownError(result);
        AssertScopeCounts(result.Trace, host, commitsStarted: 1, commitsCompleted: 1);
        Assert.Equal(2, host.Transaction.CommittedEntities);
        Assert.Equal(1, host.UndoGroup.CompleteCalls);
        Assert.Equal(0, result.Trace.UndoGroupsCompleted);
        AssertNoRollback(result.Trace, host);
    }

    private static AtomicJobExecutor CreateExecutor(RecordingDocumentHost host) =>
        new(new InlineCommandContextMarshaller(), host);

    private static IReadOnlyList<IAtomicJobOperation> Operations(int count) =>
        Enumerable.Range(0, count)
            .Select(index => (IAtomicJobOperation)new TestOperation($"operation-{index}"))
            .ToArray();

    private static ValueTask StageOperation(
        IAtomicJobOperation _,
        IAtomicTransaction transaction,
        CancellationToken __)
    {
        ((RecordingTransaction)transaction).StageEntity();
        return ValueTask.CompletedTask;
    }

    private static void AssertScopeCounts(
        AtomicExecutionTrace trace,
        RecordingDocumentHost host,
        int commitsStarted,
        int commitsCompleted)
    {
        Assert.Equal(1, trace.CommandContextEntries);
        Assert.Equal(1, trace.DocumentLocksAcquired);
        Assert.Equal(1, trace.UndoGroupsStarted);
        Assert.Equal(1, trace.TransactionsStarted);
        Assert.Equal(commitsStarted, trace.TransactionCommitsStarted);
        Assert.Equal(commitsCompleted, trace.TransactionCommitsCompleted);
        Assert.Equal(1, host.LockAcquireCalls);
        Assert.Equal(1, host.UndoGroupBeginCalls);
        Assert.Equal(1, host.TransactionBeginCalls);
        Assert.Equal(commitsStarted, host.Transaction.CommitCalls);
        Assert.Equal(1, host.DocumentLock.DisposeCalls);
        Assert.Equal(1, host.UndoGroup.DisposeCalls);
        Assert.Equal(1, host.Transaction.DisposeCalls);
    }

    private static void AssertSafeFailure(
        AtomicExecutionResult result,
        AtomicFailureKind expectedFailureKind)
    {
        Assert.Equal(AtomicExecutionOutcome.Failed, result.Outcome);
        Assert.Equal(expectedFailureKind, result.FailureKind);
        Assert.False(result.IsCommitted);
        Assert.NotNull(result.Error);
        Assert.Equal("ATOMIC_JOB_FAILED", result.Error.Code);
        Assert.False(result.Error.Retryable);
    }

    private static void AssertUnknownError(AtomicExecutionResult result)
    {
        Assert.False(result.IsCommitted);
        Assert.NotNull(result.Error);
        Assert.Equal("UNKNOWN_COMMIT_STATE", result.Error.Code);
        Assert.False(result.Error.Retryable);
        Assert.Equal(
            "Reconcile the job before any further commit attempt.",
            result.Error.RequiredAction);
    }

    private static void AssertNoRollback(AtomicExecutionTrace trace, RecordingDocumentHost host)
    {
        Assert.Equal(0, host.Transaction.AbortCalls);
        Assert.Equal(0, trace.TransactionAborts);
        Assert.Equal(0, host.UndoGroup.RollbackCalls);
        Assert.Equal(0, trace.UndoGroupsRolledBack);
    }

    private sealed record TestOperation(string OperationId) : IAtomicJobOperation;

    public enum AcquisitionFailurePoint
    {
        None,
        DocumentLock,
        UndoGroup,
        Transaction,
    }

    private enum Property13FailurePoint
    {
        Operation,
        Validation,
        CommitBeforePersistence,
        CommitAfterPersistence,
    }

    private sealed class InlineCommandContextMarshaller : ICommandContextMarshaller
    {
        public ValueTask<TResult> ExecuteAsync<TResult>(
            Func<CancellationToken, ValueTask<TResult>> callback,
            CancellationToken cancellationToken) => callback(cancellationToken);
    }

    private sealed class RecordingDocumentHost : IAtomicDocumentHost
    {
        public int LockAcquireCalls { get; private set; }

        public int UndoGroupBeginCalls { get; private set; }

        public int TransactionBeginCalls { get; private set; }

        public AcquisitionFailurePoint AcquisitionFailure { get; init; }

        public RecordingDocumentLock DocumentLock { get; } = new();

        public RecordingUndoGroup UndoGroup { get; } = new();

        public RecordingTransaction Transaction { get; } = new();

        public IDocumentLock AcquireDocumentLock()
        {
            LockAcquireCalls++;
            if (AcquisitionFailure == AcquisitionFailurePoint.DocumentLock)
            {
                throw new ExpectedTestException();
            }

            return DocumentLock;
        }

        public IUndoGroup BeginUndoGroup()
        {
            UndoGroupBeginCalls++;
            if (AcquisitionFailure == AcquisitionFailurePoint.UndoGroup)
            {
                throw new ExpectedTestException();
            }

            return UndoGroup;
        }

        public IAtomicTransaction BeginTransaction()
        {
            TransactionBeginCalls++;
            if (AcquisitionFailure == AcquisitionFailurePoint.Transaction)
            {
                throw new ExpectedTestException();
            }

            return Transaction;
        }
    }

    private sealed class RecordingDocumentLock : IDocumentLock
    {
        public int DisposeCalls { get; private set; }

        public void Dispose() => DisposeCalls++;
    }

    private sealed class RecordingUndoGroup : IUndoGroup
    {
        public bool ThrowOnComplete { get; set; }

        public bool ThrowOnRollback { get; set; }

        public int CompleteCalls { get; private set; }

        public int RollbackCalls { get; private set; }

        public int DisposeCalls { get; private set; }

        public void Complete()
        {
            CompleteCalls++;
            if (ThrowOnComplete)
            {
                throw new ExpectedTestException();
            }
        }

        public void Rollback()
        {
            RollbackCalls++;
            if (ThrowOnRollback)
            {
                throw new ExpectedTestException();
            }
        }

        public void Dispose() => DisposeCalls++;
    }

    private sealed class RecordingTransaction : IAtomicTransaction
    {
        public bool ThrowOnAbort { get; set; }

        public bool ThrowOnCommit { get; set; }

        public bool ThrowAfterCommit { get; set; }

        public int AbortCalls { get; private set; }

        public int CommitCalls { get; private set; }

        public int CommittedEntities { get; private set; }

        public int StagedEntities { get; private set; }

        public int DisposeCalls { get; private set; }

        public void StageEntity() => StagedEntities++;

        public void Commit()
        {
            CommitCalls++;
            if (ThrowOnCommit)
            {
                throw new ExpectedTestException();
            }

            CommittedEntities += StagedEntities;
            StagedEntities = 0;
            if (ThrowAfterCommit)
            {
                throw new ExpectedTestException();
            }
        }

        public void Abort()
        {
            AbortCalls++;
            if (ThrowOnAbort)
            {
                throw new ExpectedTestException();
            }

            StagedEntities = 0;
        }

        public void Dispose() => DisposeCalls++;
    }

    private sealed class ExpectedTestException : Exception;
}
