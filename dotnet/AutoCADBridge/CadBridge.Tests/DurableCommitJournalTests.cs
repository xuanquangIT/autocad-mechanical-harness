using System.Text.Json;
using System.Text.Json.Nodes;
using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class DurableCommitJournalTests
{
    private const string DigestA =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    private const string DigestB =
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    [Fact]
    public void PreparedAttemptRecoversAsUnknownAndNeverExecutesTwice()
    {
        using var directory = new TemporaryDirectory();
        var executions = 0;
        var firstOwner = new CommitJournalProcessIdentity(101, 1001);

        var firstProcess = new DurableCommitJournal(
            directory.Path,
            new FakeProcessProbe(firstOwner));
        var first = firstProcess.Begin("job-private-1", "idem-private-1", DigestA);
        if (first.Kind == CommitJournalDecisionKind.Execute)
        {
            executions++;
        }

        var restartedProcess = new DurableCommitJournal(
            directory.Path,
            new FakeProcessProbe(
                new CommitJournalProcessIdentity(202, 2002),
                _ => CommitJournalProcessLiveness.Dead));
        var recovered = restartedProcess.Begin("job-private-1", "idem-private-1", DigestA);
        if (recovered.Kind == CommitJournalDecisionKind.Execute)
        {
            executions++;
        }

        Assert.Equal(1, executions);
        Assert.Equal(CommitJournalDecisionKind.Unknown, recovered.Kind);
        var entry = Assert.Single(Directory.EnumerateFiles(directory.Path, "*.json"));
        using var document = JsonDocument.Parse(File.ReadAllText(entry));
        Assert.Equal(
            "unknown",
            document.RootElement.GetProperty("payload").GetProperty("state").GetString());
    }

    [Fact]
    public void CommittedReceiptIsDurablyReplayedAfterRestart()
    {
        using var directory = new TemporaryDirectory();
        var firstProcess = new DurableCommitJournal(directory.Path);
        var prepared = firstProcess.Begin("job-replay", "idem-replay", DigestA);
        Assert.Equal(CommitJournalDecisionKind.Execute, prepared.Kind);
        firstProcess.MarkCommitted(
            "job-replay",
            "idem-replay",
            DigestA,
            Assert.IsType<string>(prepared.ReservationId),
            BridgeHostResult.Success(new JsonObject
            {
                ["job_id"] = "job-replay",
                ["status"] = "committed",
                ["new_revision"] = "revision-2",
            }));

        var restartedProcess = new DurableCommitJournal(directory.Path);
        var replay = restartedProcess.Begin("job-replay", "idem-replay", DigestA);

        Assert.Equal(CommitJournalDecisionKind.ReplayCommitted, replay.Kind);
        Assert.Equal(BridgeHostOutcome.Ok, replay.Result?.Outcome);
        Assert.Equal("job-replay", replay.Result?.Data?["job_id"]?.GetValue<string>());
        Assert.Equal("committed", replay.Result?.Data?["status"]?.GetValue<string>());
        Assert.Equal("revision-2", replay.Result?.Data?["new_revision"]?.GetValue<string>());
    }

    [Fact]
    public void SameJobAndKeyWithDifferentDigestIsRejectedAcrossRestart()
    {
        using var directory = new TemporaryDirectory();
        var firstProcess = new DurableCommitJournal(directory.Path);
        Assert.Equal(
            CommitJournalDecisionKind.Execute,
            firstProcess.Begin("job-1", "idem-1", DigestA).Kind);

        var restartedProcess = new DurableCommitJournal(directory.Path);
        var decision = restartedProcess.Begin("job-1", "idem-1", DigestB);

        Assert.Equal(CommitJournalDecisionKind.IdempotencyKeyReused, decision.Kind);
    }

    [Fact]
    public void AbandonedPreparedAttemptCanBeRetried()
    {
        using var directory = new TemporaryDirectory();
        var journal = new DurableCommitJournal(directory.Path);
        var prepared = journal.Begin("job-1", "idem-1", DigestA);
        Assert.Equal(CommitJournalDecisionKind.Execute, prepared.Kind);

        journal.Abandon(
            "job-1",
            "idem-1",
            DigestA,
            Assert.IsType<string>(prepared.ReservationId));

        Assert.Equal(
            CommitJournalDecisionKind.Execute,
            journal.Begin("job-1", "idem-1", DigestA).Kind);
    }

    [Fact]
    public void EntryNamesAndPreparedContentDoNotExposeClientIdentifiers()
    {
        using var directory = new TemporaryDirectory();
        var journal = new DurableCommitJournal(directory.Path);
        journal.Begin("sensitive-job-name", "sensitive-idempotency-key", DigestA);

        var entry = Assert.Single(Directory.EnumerateFiles(directory.Path, "*.json"));
        var fileName = Path.GetFileNameWithoutExtension(entry);
        var content = File.ReadAllText(entry);

        Assert.Equal(64, fileName.Length);
        Assert.DoesNotContain("sensitive", fileName, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("sensitive-job-name", content, StringComparison.Ordinal);
        Assert.DoesNotContain("sensitive-idempotency-key", content, StringComparison.Ordinal);
        Assert.Empty(Directory.EnumerateFiles(directory.Path, "*.tmp"));
    }

    [Fact]
    public void CommittedContentRedactsRawJobIdentifier()
    {
        using var directory = new TemporaryDirectory();
        var journal = new DurableCommitJournal(directory.Path);
        var prepared = journal.Begin("private-job-id", "private-idem", DigestA);
        journal.MarkCommitted(
            "private-job-id",
            "private-idem",
            DigestA,
            Assert.IsType<string>(prepared.ReservationId),
            BridgeHostResult.Success(new JsonObject
            {
                ["job_id"] = "private-job-id",
                ["status"] = "committed",
            }));

        var entry = Assert.Single(Directory.EnumerateFiles(directory.Path, "*.json"));
        Assert.DoesNotContain("private-job-id", File.ReadAllText(entry), StringComparison.Ordinal);
    }

    [Fact]
    public void MalformedEntryFailsClosedAtStartup()
    {
        using var directory = new TemporaryDirectory();
        File.WriteAllText(
            Path.Combine(directory.Path, $"{new string('c', 64)}.json"),
            "{not-json}");

        Assert.Throws<InvalidDataException>(() => new DurableCommitJournal(directory.Path));
    }

    [Fact]
    public async Task CoordinatorReplaysCommittedReceiptWithoutSecondHostCallAfterRestart()
    {
        using var directory = new TemporaryDirectory();
        var hostCalls = 0;
        var first = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var firstResult = await first.ExecuteAsync(
            "job-coordinator",
            "idem-coordinator",
            DigestA,
            () => true,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(BridgeHostResult.Success(new JsonObject
                {
                    ["job_id"] = "job-coordinator",
                    ["status"] = "committed",
                }));
            },
            CancellationToken.None);

        var restarted = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var replay = await restarted.ExecuteAsync(
            "job-coordinator",
            "idem-coordinator",
            DigestA,
            () => true,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Failed));
            },
            CancellationToken.None);

        Assert.Equal(BridgeHostOutcome.Ok, firstResult.Outcome);
        Assert.Equal(BridgeHostOutcome.Ok, replay.Outcome);
        Assert.Equal(1, hostCalls);
    }

    [Fact]
    public async Task InvalidOrExpiredAuthorizationCannotReplayCommittedReceipt()
    {
        using var directory = new TemporaryDirectory();
        var first = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var committed = await first.ExecuteAsync(
            "job-protected-replay",
            "idem-protected-replay",
            DigestA,
            () => true,
            _ => ValueTask.FromResult(BridgeHostResult.Success(new JsonObject
            {
                ["job_id"] = "job-protected-replay",
                ["status"] = "committed",
            })),
            CancellationToken.None);
        var restarted = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var hostCalls = 0;

        var rejected = await restarted.ExecuteAsync(
            "job-protected-replay",
            "idem-protected-replay",
            DigestA,
            () => false,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Failed));
            },
            CancellationToken.None);

        Assert.Equal(BridgeHostOutcome.Ok, committed.Outcome);
        Assert.Equal(BridgeHostOutcome.Rejected, rejected.Outcome);
        Assert.Equal(0, hostCalls);
    }

    [Fact]
    public void LiveOwnerIsNotPoisonedAndCanStillAbandonPreparedReservation()
    {
        using var directory = new TemporaryDirectory();
        var ownerA = new CommitJournalProcessIdentity(301, 3001);
        var ownerB = new CommitJournalProcessIdentity(302, 3002);
        var first = new DurableCommitJournal(
            directory.Path,
            new FakeProcessProbe(ownerA));
        var prepared = first.Begin("job-live-owner", "idem-live-owner", DigestA);
        var second = new DurableCommitJournal(
            directory.Path,
            new FakeProcessProbe(
                ownerB,
                identity => identity == ownerA
                    ? CommitJournalProcessLiveness.Alive
                    : CommitJournalProcessLiveness.Unknown));

        var competing = second.Begin("job-live-owner", "idem-live-owner", DigestA);
        first.Abandon(
            "job-live-owner",
            "idem-live-owner",
            DigestA,
            Assert.IsType<string>(prepared.ReservationId));
        var retry = second.Begin("job-live-owner", "idem-live-owner", DigestA);

        Assert.Equal(CommitJournalDecisionKind.Unknown, competing.Kind);
        Assert.Equal(CommitJournalDecisionKind.Execute, retry.Kind);
    }

    [Fact]
    public void TamperedCommittedReceiptFailsClosed()
    {
        using var directory = new TemporaryDirectory();
        var journal = new DurableCommitJournal(directory.Path);
        var prepared = journal.Begin("job-tamper", "idem-tamper", DigestA);
        journal.MarkCommitted(
            "job-tamper",
            "idem-tamper",
            DigestA,
            Assert.IsType<string>(prepared.ReservationId),
            BridgeHostResult.Success(new JsonObject
            {
                ["job_id"] = "job-tamper",
                ["status"] = "committed",
            }));
        var entry = Assert.Single(Directory.EnumerateFiles(directory.Path, "*.json"));
        var content = File.ReadAllText(entry);
        File.WriteAllText(entry, content.Replace("committed", "tampered", StringComparison.Ordinal));

        Assert.Throws<InvalidDataException>(() => new DurableCommitJournal(directory.Path));
    }

    [Fact]
    public async Task CoordinatorAbandonsProvenSafeFailureAndAllowsRetry()
    {
        using var directory = new TemporaryDirectory();
        var hostCalls = 0;
        var coordinator = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));

        var failed = await coordinator.ExecuteAsync(
            "job-safe-failure",
            "idem-safe-failure",
            DigestA,
            () => true,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Rejected));
            },
            CancellationToken.None);
        var retry = await coordinator.ExecuteAsync(
            "job-safe-failure",
            "idem-safe-failure",
            DigestA,
            () => true,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(BridgeHostResult.Success(new JsonObject
                {
                    ["job_id"] = "job-safe-failure",
                    ["status"] = "committed",
                }));
            },
            CancellationToken.None);

        Assert.Equal(BridgeHostOutcome.Rejected, failed.Outcome);
        Assert.Equal(BridgeHostOutcome.Ok, retry.Outcome);
        Assert.Equal(2, hostCalls);
    }

    [Fact]
    public async Task JournalReservationFailureCausesZeroHostCalls()
    {
        using var directory = new TemporaryDirectory();
        var journal = new DurableCommitJournal(directory.Path);
        Directory.Delete(directory.Path, recursive: true);
        File.WriteAllText(directory.Path, "journal-root-is-now-a-file");
        var hostCalls = 0;
        var coordinator = new DurableCommitCoordinator(journal);

        var result = await coordinator.ExecuteAsync(
            "job-no-write",
            "idem-no-write",
            DigestA,
            () => true,
            _ =>
            {
                hostCalls++;
                return ValueTask.FromResult(BridgeHostResult.Success(new JsonObject()));
            },
            CancellationToken.None);

        Assert.Equal(BridgeHostOutcome.Failed, result.Outcome);
        Assert.Equal(0, hostCalls);
    }

    [Fact]
    public async Task TwoPreloadedProcessesCannotExecuteSameCommitConcurrently()
    {
        using var directory = new TemporaryDirectory();
        var first = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var second = new DurableCommitCoordinator(new DurableCommitJournal(directory.Path));
        var firstEnteredHost = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseFirstHost = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var hostCalls = 0;

        var firstTask = first.ExecuteAsync(
            "job-concurrent",
            "idem-concurrent",
            DigestA,
            () => true,
            async _ =>
            {
                Interlocked.Increment(ref hostCalls);
                firstEnteredHost.SetResult();
                await releaseFirstHost.Task;
                return BridgeHostResult.Success(new JsonObject
                {
                    ["job_id"] = "job-concurrent",
                    ["status"] = "committed",
                });
            },
            CancellationToken.None).AsTask();
        await firstEnteredHost.Task;

        var secondResult = await second.ExecuteAsync(
            "job-concurrent",
            "idem-concurrent",
            DigestA,
            () => true,
            _ =>
            {
                Interlocked.Increment(ref hostCalls);
                return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Failed));
            },
            CancellationToken.None);
        releaseFirstHost.SetResult();
        var firstResult = await firstTask;

        Assert.Equal(BridgeHostOutcome.UnknownCommitState, secondResult.Outcome);
        Assert.Equal(BridgeHostOutcome.Ok, firstResult.Outcome);
        Assert.Equal(1, hostCalls);
    }

    [Fact]
    public void CommitDigestBindsJobKeyPlanRevisionAndCheckpointButNotRefreshedApproval()
    {
        using var plan = JsonDocument.Parse("""{"job_id":"job-1","operations":[]}""");
        var baseline = new CommitHostRequest(
            plan.RootElement.Clone(),
            "job-1",
            "idem-1",
            "revision-1",
            "approval-1",
            CreateCheckpoint: false);

        var digest = CommitRequestDigest.Compute(baseline);

        Assert.Equal(64, digest.Length);
        Assert.NotEqual(digest, CommitRequestDigest.Compute(baseline with { JobId = "job-2" }));
        Assert.NotEqual(
            digest,
            CommitRequestDigest.Compute(baseline with { IdempotencyKey = "idem-2" }));
        Assert.NotEqual(
            digest,
            CommitRequestDigest.Compute(baseline with { ExpectedRevision = "revision-2" }));
        Assert.NotEqual(
            digest,
            CommitRequestDigest.Compute(baseline with { CreateCheckpoint = true }));
        using var changedPlan = JsonDocument.Parse(
            """{"job_id":"job-1","operations":[{"operation_id":"op-1"}]}""");
        Assert.NotEqual(
            digest,
            CommitRequestDigest.Compute(
                baseline with { Plan = changedPlan.RootElement.Clone() }));
        Assert.Equal(
            digest,
            CommitRequestDigest.Compute(baseline with { ApprovalToken = "approval-refreshed" }));

        using var reorderedPlan = JsonDocument.Parse(
            """ { "operations" : [ ], "job_id" : "job-1" } """);
        Assert.Equal(
            digest,
            CommitRequestDigest.Compute(
                baseline with { Plan = reorderedPlan.RootElement.Clone() }));
    }

    [Theory]
    [InlineData("")]
    [InlineData("relative")]
    public void RootMustBeAbsolute(string root)
    {
        Assert.Throws<ArgumentException>(() => new DurableCommitJournal(root));
    }

    private sealed class TemporaryDirectory : IDisposable
    {
        public TemporaryDirectory()
        {
            Path = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                $"cad-bridge-journal-tests-{Guid.NewGuid():N}");
            Directory.CreateDirectory(Path);
        }

        public string Path { get; }

        public void Dispose()
        {
            if (Directory.Exists(Path))
            {
                Directory.Delete(Path, recursive: true);
            }
            else if (File.Exists(Path))
            {
                File.Delete(Path);
            }
        }
    }

    private sealed class FakeProcessProbe : ICommitJournalProcessProbe
    {
        private readonly Func<CommitJournalProcessIdentity, CommitJournalProcessLiveness> _liveness;

        public FakeProcessProbe(
            CommitJournalProcessIdentity current,
            Func<CommitJournalProcessIdentity, CommitJournalProcessLiveness>? liveness = null)
        {
            Current = current;
            _liveness = liveness ?? (_ => CommitJournalProcessLiveness.Alive);
        }

        public CommitJournalProcessIdentity Current { get; }

        public CommitJournalProcessLiveness GetLiveness(CommitJournalProcessIdentity identity) =>
            _liveness(identity);
    }
}
