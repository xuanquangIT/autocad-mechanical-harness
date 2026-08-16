using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class DurableCheckpointRestoreCoordinatorTests
{
    private static readonly byte[] AuthenticationKey = Enumerable.Range(1, 32)
        .Select(value => (byte)value)
        .ToArray();

    [Fact]
    public async Task ExactAuthorizedRestoreStagesReplacesVerifiesAndConsumesCheckpoint()
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        using var coordinator = fixture.CreateCoordinator(lifecycle);

        var result = await coordinator.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, result.Outcome);
        Assert.Equal(DurableCheckpointRestoreState.Committed, result.State);
        Assert.Equal(fixture.PreRevision, result.RestoredRevision);
        Assert.Equal(fixture.CheckpointBytes, File.ReadAllBytes(fixture.TargetPath));
        Assert.Equal(
            DurableCheckpointState.Consumed,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(1, lifecycle.CloseCalls);
        Assert.Equal(1, lifecycle.ReopenCalls);
        Assert.All(lifecycle.DestructiveTokens, token => Assert.False(token.CanBeCanceled));
        Assert.Empty(Directory.EnumerateFiles(fixture.TargetDirectory, "*.stage.dwg"));
        Assert.Empty(Directory.EnumerateFiles(fixture.TargetDirectory, "*.backup.dwg"));

        var journal = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        var serialized = File.ReadAllText(journal);
        Assert.Equal("committed", ReadJournalState(journal));
        Assert.DoesNotContain(fixture.TargetPath, serialized, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain(fixture.DocumentId, serialized, StringComparison.Ordinal);
        Assert.DoesNotContain(fixture.ApprovalId, serialized, StringComparison.Ordinal);
    }

    [Fact]
    public async Task SameApprovalAndScopeReplaysAfterRestartWithoutLifecycleCalls()
    {
        using var fixture = new RestoreFixture();
        using (var first = fixture.CreateCoordinator(fixture.CreateLifecycle()))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.Completed,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        var replayLifecycle = fixture.CreateLifecycle();
        replayLifecycle.ThrowOnAnyCall = true;
        using var restarted = fixture.CreateCoordinator(replayLifecycle);
        var replay = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Replayed, replay.Outcome);
        Assert.Equal(DurableCheckpointRestoreState.Committed, replay.State);
        Assert.Equal(0, replayLifecycle.TotalCalls);
    }

    [Fact]
    public async Task SameApprovalWithChangedPostRevisionIsRejectedAsScopeConflict()
    {
        using var fixture = new RestoreFixture();
        var unavailableLifecycle = fixture.CreateLifecycle();
        unavailableLifecycle.ThrowOnInspect = true;
        using var coordinator = fixture.CreateCoordinator(unavailableLifecycle);
        var first = await coordinator.RestoreAsync(fixture.Request, CancellationToken.None);
        var changed = fixture.Request with
        {
            Authorization = fixture.Request.Authorization with
            {
                PostRevision = "sha256:another-post-revision",
            },
        };

        var conflict = await coordinator.RestoreAsync(changed, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, first.Outcome);
        Assert.Equal(DurableCheckpointRestoreOutcome.ScopeConflict, conflict.Outcome);
        Assert.Equal(0, unavailableLifecycle.CloseCalls);
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));

        unavailableLifecycle.ThrowOnInspect = false;
        var exactRetry = await coordinator.RestoreAsync(
            fixture.Request,
            CancellationToken.None);
        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, exactRetry.Outcome);
        Assert.Equal(
            DurableCheckpointState.Consumed,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
    }

    [Fact]
    public async Task RejectedPreReservationInputLeavesCheckpointRetryable()
    {
        using var fixture = new RestoreFixture();
        File.WriteAllBytes(fixture.TargetPath, Encoding.ASCII.GetBytes("not-a-dwg"));
        using var coordinator = fixture.CreateCoordinator(fixture.CreateLifecycle());

        var rejected = await coordinator.RestoreAsync(
            fixture.Request,
            CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Rejected, rejected.Outcome);
        Assert.Equal(
            DurableCheckpointState.Available,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Empty(Directory.EnumerateFiles(fixture.JournalRoot, "*.restore.json"));

        File.WriteAllBytes(fixture.TargetPath, fixture.PostBytes);
        var retry = await coordinator.RestoreAsync(fixture.Request, CancellationToken.None);
        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, retry.Outcome);
    }

    [Fact]
    public async Task StaleLivePostRevisionIsRejectedBeforeCloseAndCannotReplay()
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.CurrentRevision = "sha256:stale-live-revision";
        using var coordinator = fixture.CreateCoordinator(lifecycle);

        var result = await coordinator.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Quarantined, result.Outcome);
        Assert.Equal(DurableCheckpointRestoreState.Quarantined, result.State);
        Assert.Equal(0, lifecycle.CloseCalls);
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));
        Assert.Equal(
            DurableCheckpointState.Quarantined,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(
            "quarantined",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));
    }

    [Fact]
    public async Task CancellationAfterPreparedButBeforeClosePreservesExactRetryAttempt()
    {
        using var fixture = new RestoreFixture();
        using var cancellation = new CancellationTokenSource();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.OnInspect = () => cancellation.Cancel();
        using var coordinator = fixture.CreateCoordinator(lifecycle);

        var result = await coordinator.RestoreAsync(fixture.Request, cancellation.Token);

        Assert.Equal(DurableCheckpointRestoreOutcome.Cancelled, result.Outcome);
        Assert.Equal(DurableCheckpointRestoreState.Prepared, result.State);
        Assert.Equal(0, lifecycle.CloseCalls);
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));
        Assert.Equal(
            DurableCheckpointState.Restoring,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(
            "prepared",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));
        Assert.Single(Directory.EnumerateFiles(fixture.TargetDirectory, "*.stage.dwg"));

        using var restarted = fixture.CreateCoordinator(fixture.CreateLifecycle());
        var retry = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);
        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, retry.Outcome);
    }

    [Fact]
    public async Task CancellationRaisedAtCloseIsIgnoredUntilRestoreIsDurablyComplete()
    {
        using var fixture = new RestoreFixture();
        using var cancellation = new CancellationTokenSource();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.OnClose = () => cancellation.Cancel();
        using var coordinator = fixture.CreateCoordinator(lifecycle);

        var result = await coordinator.RestoreAsync(fixture.Request, cancellation.Token);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, result.Outcome);
        Assert.True(cancellation.IsCancellationRequested);
        Assert.Equal(1, lifecycle.CloseCalls);
        Assert.Equal(1, lifecycle.ReopenCalls);
        Assert.All(lifecycle.DestructiveTokens, token => Assert.False(token.CanBeCanceled));
    }

    [Fact]
    public async Task RestartCompletesExactReplacedStateAfterReopenFailure()
    {
        using var fixture = new RestoreFixture();
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.ThrowOnReopen = true;
        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            var interrupted = await first.RestoreAsync(fixture.Request, CancellationToken.None);
            Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, interrupted.Outcome);
            Assert.Equal(DurableCheckpointRestoreState.Replaced, interrupted.State);
        }

        Assert.Equal(fixture.CheckpointBytes, File.ReadAllBytes(fixture.TargetPath));
        Assert.Equal(
            "replaced",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));

        var recoveryLifecycle = fixture.CreateLifecycle(open: false);
        using var restarted = fixture.CreateCoordinator(recoveryLifecycle);
        var recordedTarget = restarted.ResolveRecordedTarget(fixture.Request.Authorization);
        Assert.Equal(fixture.TargetPath, recordedTarget);
        var recovered = await restarted.RestoreAsync(
            fixture.Request with { TargetPath = recordedTarget! },
            CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, recovered.Outcome);
        Assert.Equal(0, recoveryLifecycle.CloseCalls);
        Assert.Equal(1, recoveryLifecycle.ReopenCalls);
        Assert.Equal(
            DurableCheckpointState.Consumed,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
    }

    [Fact]
    public async Task RestartReopensExactPostFileWhenCloseFinishedButReplaceDidNot()
    {
        using var fixture = new RestoreFixture();
        FileStream? stageLock = null;
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.OnClose = () =>
        {
            var stage = Assert.Single(Directory.EnumerateFiles(
                fixture.TargetDirectory,
                "*.stage.dwg"));
            stageLock = new FileStream(
                stage,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read);
        };

        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            var interrupted = await first.RestoreAsync(fixture.Request, CancellationToken.None);
            Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, interrupted.Outcome);
            Assert.Equal(DurableCheckpointRestoreState.Prepared, interrupted.State);
        }

        Assert.NotNull(stageLock);
        stageLock.Dispose();
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));

        var recoveryLifecycle = fixture.CreateLifecycle(open: false);
        using var restarted = fixture.CreateCoordinator(recoveryLifecycle);
        var recovered = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, recovered.Outcome);
        Assert.Equal(1, recoveryLifecycle.CloseCalls);
        Assert.Equal(2, recoveryLifecycle.ReopenCalls);
        Assert.Equal(fixture.CheckpointBytes, File.ReadAllBytes(fixture.TargetPath));
    }

    [Fact]
    public async Task PreparedJournalAndExactReplacementHashesRecoverWithoutGuessing()
    {
        using var fixture = new RestoreFixture();
        FileStream? journalLock = null;
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.OnClose = () =>
        {
            var journal = Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"));
            journalLock = new FileStream(
                journal,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read);
        };

        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            var interrupted = await first.RestoreAsync(fixture.Request, CancellationToken.None);
            Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, interrupted.Outcome);
            Assert.Equal(DurableCheckpointRestoreState.Prepared, interrupted.State);
        }

        Assert.NotNull(journalLock);
        journalLock.Dispose();
        Assert.Equal(fixture.CheckpointBytes, File.ReadAllBytes(fixture.TargetPath));
        Assert.Equal(
            "prepared",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));

        var recoveryLifecycle = fixture.CreateLifecycle(open: false);
        using var restarted = fixture.CreateCoordinator(recoveryLifecycle);
        var recovered = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, recovered.Outcome);
        Assert.Equal(0, recoveryLifecycle.CloseCalls);
        Assert.Equal(1, recoveryLifecycle.ReopenCalls);
    }

    [Fact]
    public async Task VerifiedStateFinishesAfterCatalogWasConsumedButJournalCommitFailed()
    {
        using var fixture = new RestoreFixture();
        FileStream? journalLock = null;
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.OnInspect = () =>
        {
            if (firstLifecycle.InspectCalls != 3)
            {
                return;
            }

            var journal = Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"));
            journalLock = new FileStream(
                journal,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read);
        };

        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            var interrupted = await first.RestoreAsync(fixture.Request, CancellationToken.None);
            Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, interrupted.Outcome);
            Assert.Equal(DurableCheckpointRestoreState.Verified, interrupted.State);
        }

        Assert.NotNull(journalLock);
        journalLock.Dispose();
        Assert.Equal(
            DurableCheckpointState.Consumed,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(
            "verified",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));

        var recoveryLifecycle = fixture.CreateLifecycle();
        recoveryLifecycle.CurrentRevision = fixture.PreRevision;
        using var restarted = fixture.CreateCoordinator(recoveryLifecycle);
        var recovered = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Completed, recovered.Outcome);
        Assert.Equal(0, recoveryLifecycle.CloseCalls);
        Assert.Equal(0, recoveryLifecycle.ReopenCalls);
        Assert.Equal(
            "committed",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));
    }

    [Fact]
    public async Task ChangedTargetAfterReplacementIsQuarantinedOnRestart()
    {
        using var fixture = new RestoreFixture();
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.ThrowOnReopen = true;
        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        File.WriteAllBytes(fixture.TargetPath, DwgBytes("ambiguous-third-content"));
        var recoveryLifecycle = fixture.CreateLifecycle(open: false);
        using var restarted = fixture.CreateCoordinator(recoveryLifecycle);
        var recovered = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Quarantined, recovered.Outcome);
        Assert.Equal(0, recoveryLifecycle.TotalCalls);
        Assert.Equal(
            DurableCheckpointState.Quarantined,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(
            "quarantined",
            ReadJournalState(Assert.Single(Directory.EnumerateFiles(
                fixture.JournalRoot,
                "*.restore.json"))));
    }

    [Fact]
    public async Task WholesaleMetadataReplayCannotBypassMissingPhaseArtifacts()
    {
        using var fixture = new RestoreFixture();
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.ThrowOnInspect = true;
        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        var catalogPath = Assert.Single(Directory.EnumerateFiles(
            fixture.CheckpointRoot,
            "checkpoint-catalog.*.json"));
        var watermarkPath = Assert.Single(Directory.EnumerateFiles(
            fixture.CheckpointRoot,
            "checkpoint-catalog.*.watermark"));
        var journalPath = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        var oldCatalog = File.ReadAllBytes(catalogPath);
        var oldWatermark = File.ReadAllBytes(watermarkPath);
        var oldJournal = File.ReadAllBytes(journalPath);

        using (var continuation = fixture.CreateCoordinator(fixture.CreateLifecycle()))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.Completed,
                (await continuation.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        Assert.Empty(Directory.EnumerateFiles(fixture.TargetDirectory, "*.stage.dwg"));
        Assert.Empty(Directory.EnumerateFiles(fixture.TargetDirectory, "*.backup.dwg"));
        fixture.Catalog.Dispose();
        File.WriteAllBytes(catalogPath, oldCatalog);
        File.WriteAllBytes(watermarkPath, oldWatermark);
        File.WriteAllBytes(journalPath, oldJournal);
        fixture.RestartCatalog();

        var replayLifecycle = fixture.CreateLifecycle(open: false);
        using var restarted = fixture.CreateCoordinator(replayLifecycle);
        var result = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Quarantined, result.Outcome);
        Assert.Equal(0, replayLifecycle.TotalCalls);
        Assert.Equal(
            DurableCheckpointState.Quarantined,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(fixture.CheckpointBytes, File.ReadAllBytes(fixture.TargetPath));
    }

    [Fact]
    public async Task WrongSemanticPreRevisionAfterReopenQuarantinesCheckpoint()
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.ReopenedRevision = "sha256:not-the-checkpoint-revision";
        lifecycle.RevisionOnReopen = null;
        using var coordinator = fixture.CreateCoordinator(lifecycle);

        var result = await coordinator.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Quarantined, result.Outcome);
        Assert.Equal(
            DurableCheckpointState.Quarantined,
            fixture.Catalog.GetRequired(fixture.CheckpointId).State);
        Assert.Equal(1, lifecycle.CloseCalls);
        Assert.Equal(1, lifecycle.ReopenCalls);
    }

    [Fact]
    public async Task TamperedAuthenticatedJournalFailsClosedBeforeLifecycle()
    {
        using var fixture = new RestoreFixture();
        var firstLifecycle = fixture.CreateLifecycle();
        firstLifecycle.ThrowOnInspect = true;
        using (var first = fixture.CreateCoordinator(firstLifecycle))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        var journal = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        File.WriteAllText(
            journal,
            File.ReadAllText(journal).Replace("prepared", "replaced", StringComparison.Ordinal));
        var recoveryLifecycle = fixture.CreateLifecycle();

        Assert.Throws<InvalidDataException>(() => fixture.CreateCoordinator(recoveryLifecycle));
        Assert.Equal(0, recoveryLifecycle.TotalCalls);
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));
    }

    [Theory]
    [InlineData(DurableCheckpointRestoreState.Prepared)]
    [InlineData(DurableCheckpointRestoreState.Replaced)]
    [InlineData(DurableCheckpointRestoreState.Verified)]
    [InlineData(DurableCheckpointRestoreState.Committed)]
    public async Task ExpiredRb1CanResumeOnlyAnExactAuthenticatedRecordedPhase(
        DurableCheckpointRestoreState phase)
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        FileStream? journalLock = null;
        if (phase == DurableCheckpointRestoreState.Prepared)
        {
            lifecycle.ThrowOnInspect = true;
        }
        else if (phase == DurableCheckpointRestoreState.Replaced)
        {
            lifecycle.ThrowOnReopen = true;
        }
        else if (phase == DurableCheckpointRestoreState.Verified)
        {
            lifecycle.OnInspect = () =>
            {
                if (lifecycle.InspectCalls == 3)
                {
                    journalLock = new FileStream(
                        Assert.Single(Directory.EnumerateFiles(
                            fixture.JournalRoot,
                            "*.restore.json")),
                        FileMode.Open,
                        FileAccess.Read,
                        FileShare.Read);
                }
            };
        }

        const string secret = "expired-rb1-recovery-secret";
        var expiry = new DateTimeOffset(2026, 8, 15, 2, 5, 0, TimeSpan.Zero);
        var token = IssueRollbackToken(
            fixture,
            secret,
            fixture.ApprovalId,
            fixture.PostRevision,
            expiry);
        Assert.True(BridgeAuthorization.TryValidateRollbackAuthorization(
            token,
            secret,
            fixture.JobId,
            fixture.DocumentId,
            fixture.CheckpointId,
            fixture.PostRevision,
            expiry,
            out var originalClaims));
        Assert.NotNull(originalClaims);
        var recordedRequest = fixture.Request with
        {
            Authorization = fixture.Request.Authorization with
            {
                ApprovalTokenDigest = originalClaims.ApprovalTokenDigest,
            },
        };
        using var coordinator = fixture.CreateCoordinator(lifecycle);
        _ = await coordinator.RestoreAsync(recordedRequest, CancellationToken.None);
        journalLock?.Dispose();
        var journal = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        Assert.Equal(phase.ToString().ToLowerInvariant(), ReadJournalState(journal));

        Assert.True(BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(
            token,
            secret,
            fixture.JobId,
            fixture.DocumentId,
            fixture.CheckpointId,
            fixture.PostRevision,
            expiry.AddTicks(1),
            claims => coordinator.CanResumeExactRecordedAttempt(
                recordedRequest.Authorization with
                {
                    ApprovalId = claims.ApprovalId,
                    ApprovalTokenDigest = claims.ApprovalTokenDigest,
                }),
            out var claims));
        Assert.NotNull(claims);
        Assert.Equal(fixture.ApprovalId, claims.ApprovalId);

        var newApprovalToken = IssueRollbackToken(
            fixture,
            secret,
            "approval-rb1-new",
            fixture.PostRevision,
            expiry);
        Assert.False(BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(
            newApprovalToken,
            secret,
            fixture.JobId,
            fixture.DocumentId,
            fixture.CheckpointId,
            fixture.PostRevision,
            expiry.AddTicks(1),
            recoveryClaims => coordinator.CanResumeExactRecordedAttempt(
                recordedRequest.Authorization with
                {
                    ApprovalId = recoveryClaims.ApprovalId,
                    ApprovalTokenDigest = recoveryClaims.ApprovalTokenDigest,
                }),
            out _));

        var reissuedExpiry = expiry.AddMinutes(1);
        var reissuedToken = IssueRollbackToken(
            fixture,
            secret,
            fixture.ApprovalId,
            fixture.PostRevision,
            reissuedExpiry);
        Assert.False(BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(
            reissuedToken,
            secret,
            fixture.JobId,
            fixture.DocumentId,
            fixture.CheckpointId,
            fixture.PostRevision,
            reissuedExpiry.AddTicks(1),
            recoveryClaims => coordinator.CanResumeExactRecordedAttempt(
                recordedRequest.Authorization with
                {
                    ApprovalId = recoveryClaims.ApprovalId,
                    ApprovalTokenDigest = recoveryClaims.ApprovalTokenDigest,
                }),
            out _));

        const string changedRevision = "sha256:changed-post-revision";
        var changedScopeToken = IssueRollbackToken(
            fixture,
            secret,
            fixture.ApprovalId,
            changedRevision,
            expiry);
        Assert.False(BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(
            changedScopeToken,
            secret,
            fixture.JobId,
            fixture.DocumentId,
            fixture.CheckpointId,
            changedRevision,
            expiry.AddTicks(1),
            recoveryClaims => coordinator.CanResumeExactRecordedAttempt(
                fixture.Request.Authorization with
                {
                    ApprovalId = recoveryClaims.ApprovalId,
                    ApprovalTokenDigest = recoveryClaims.ApprovalTokenDigest,
                    PostRevision = changedRevision,
                }),
            out _));
    }

    [Fact]
    public async Task RecoveryProofRejectsTamperedAuthenticatedJournal()
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.ThrowOnInspect = true;
        using var coordinator = fixture.CreateCoordinator(lifecycle);
        Assert.Equal(
            DurableCheckpointRestoreOutcome.RecoveryRequired,
            (await coordinator.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);

        var journal = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        File.WriteAllText(
            journal,
            File.ReadAllText(journal).Replace("prepared", "replaced", StringComparison.Ordinal));

        Assert.Throws<InvalidDataException>(() =>
            coordinator.CanResumeExactRecordedAttempt(fixture.Request.Authorization));
        Assert.Equal(0, lifecycle.CloseCalls);
    }

    [Fact]
    public async Task RecoveryProofAndRecordedTargetSurfaceLockContentionForTypedHostRetry()
    {
        using var fixture = new RestoreFixture();
        var lifecycle = fixture.CreateLifecycle();
        lifecycle.ThrowOnInspect = true;
        using var coordinator = fixture.CreateCoordinator(lifecycle);
        Assert.Equal(
            DurableCheckpointRestoreOutcome.RecoveryRequired,
            (await coordinator.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);

        using var held = new FileStream(
            Path.Combine(fixture.JournalRoot, ".checkpoint-restore.execution.lock"),
            FileMode.Open,
            FileAccess.ReadWrite,
            FileShare.None);
        Assert.Throws<IOException>(() =>
            coordinator.CanResumeExactRecordedAttempt(fixture.Request.Authorization));
        Assert.Throws<IOException>(() =>
            coordinator.ResolveRecordedTarget(fixture.Request.Authorization));
        Assert.Equal(0, lifecycle.CloseCalls);
    }

    [Fact]
    public async Task CommittedReplayCleansCrashOrphansAndRetriesCleanupFailure()
    {
        using var fixture = new RestoreFixture();
        using (var first = fixture.CreateCoordinator(fixture.CreateLifecycle()))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.Completed,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        var journal = Assert.Single(Directory.EnumerateFiles(
            fixture.JournalRoot,
            "*.restore.json"));
        var stagePath = Path.Combine(
            fixture.TargetDirectory,
            ReadJournalPayloadString(journal, "stage_file_name"));
        var backupPath = Path.Combine(
            fixture.TargetDirectory,
            ReadJournalPayloadString(journal, "backup_file_name"));

        // Reconstruct the exact on-disk image left by a crash after the Committed journal write
        // but before best-effort cleanup from the original process.
        File.WriteAllBytes(stagePath, fixture.CheckpointBytes);
        Directory.CreateDirectory(backupPath);
        var blockedLifecycle = fixture.CreateLifecycle();
        blockedLifecycle.ThrowOnAnyCall = true;
        using (var blockedReplay = fixture.CreateCoordinator(blockedLifecycle))
        {
            var pending = await blockedReplay.RestoreAsync(
                fixture.Request,
                CancellationToken.None);
            Assert.Equal(DurableCheckpointRestoreOutcome.RecoveryRequired, pending.Outcome);
            Assert.Equal(DurableCheckpointRestoreState.Committed, pending.State);
            Assert.Equal(0, blockedLifecycle.TotalCalls);
            Assert.False(File.Exists(stagePath));
            Assert.True(Directory.Exists(backupPath));
        }

        Directory.Delete(backupPath);
        File.WriteAllBytes(backupPath, fixture.PostBytes);
        var replayLifecycle = fixture.CreateLifecycle();
        replayLifecycle.ThrowOnAnyCall = true;
        using var restarted = fixture.CreateCoordinator(replayLifecycle);
        var replay = await restarted.RestoreAsync(fixture.Request, CancellationToken.None);

        Assert.Equal(DurableCheckpointRestoreOutcome.Replayed, replay.Outcome);
        Assert.Equal(DurableCheckpointRestoreState.Committed, replay.State);
        Assert.Equal(0, replayLifecycle.TotalCalls);
        Assert.False(File.Exists(stagePath));
        Assert.False(File.Exists(backupPath));
    }

    [Theory]
    [InlineData("relative.dwg")]
    [InlineData("\\\\server\\share\\drawing.dwg")]
    [InlineData("\\\\?\\C:\\drawing.dwg")]
    public async Task NonAbsoluteNetworkAndDeviceTargetsAreRejected(string targetPath)
    {
        using var fixture = new RestoreFixture();
        using var coordinator = fixture.CreateCoordinator(fixture.CreateLifecycle());
        var request = fixture.Request with { TargetPath = targetPath };

        await Assert.ThrowsAsync<ArgumentException>(async () =>
            await coordinator.RestoreAsync(request, CancellationToken.None));
    }

    [Fact]
    public async Task DifferentCanonicalLocalPathCannotMatchCheckpointPathHash()
    {
        using var fixture = new RestoreFixture();
        using var coordinator = fixture.CreateCoordinator(fixture.CreateLifecycle());
        var wrongPath = Path.Combine(fixture.TargetDirectory, "another-customer-drawing.dwg");
        File.WriteAllBytes(wrongPath, fixture.PostBytes);

        await Assert.ThrowsAsync<ArgumentException>(async () =>
            await coordinator.RestoreAsync(
                fixture.Request with { TargetPath = wrongPath },
                CancellationToken.None));
        Assert.Empty(Directory.EnumerateFiles(fixture.JournalRoot, "*.restore.json"));
    }

    [Fact]
    public async Task RecordedTargetResolverRejectsReparseSubstitutionAfterReservation()
    {
        using var fixture = new RestoreFixture();
        var unavailable = fixture.CreateLifecycle();
        unavailable.ThrowOnInspect = true;
        using (var first = fixture.CreateCoordinator(unavailable))
        {
            Assert.Equal(
                DurableCheckpointRestoreOutcome.RecoveryRequired,
                (await first.RestoreAsync(fixture.Request, CancellationToken.None)).Outcome);
        }

        var backing = Path.Combine(fixture.TargetDirectory, "backing-customer-drawing.dwg");
        File.Move(fixture.TargetPath, backing);
        try
        {
            try
            {
                File.CreateSymbolicLink(fixture.TargetPath, backing);
            }
            catch (Exception exception) when (exception is IOException or
                UnauthorizedAccessException or PlatformNotSupportedException)
            {
                File.Move(backing, fixture.TargetPath);
                return;
            }

            using var restarted = fixture.CreateCoordinator(fixture.CreateLifecycle(open: false));
            Assert.Throws<InvalidDataException>(() =>
                restarted.ResolveRecordedTarget(fixture.Request.Authorization));
        }
        finally
        {
            if (File.Exists(fixture.TargetPath))
            {
                File.Delete(fixture.TargetPath);
            }

            if (File.Exists(backing))
            {
                File.Move(backing, fixture.TargetPath);
            }
        }
    }

    [Fact]
    public async Task AuthorizationMustMatchExactCheckpointAndPreRevision()
    {
        using var fixture = new RestoreFixture();
        using var coordinator = fixture.CreateCoordinator(fixture.CreateLifecycle());
        var request = fixture.Request with
        {
            Authorization = fixture.Request.Authorization with
            {
                PreRevision = "sha256:wrong-pre-revision",
            },
        };

        await Assert.ThrowsAsync<ArgumentException>(async () =>
            await coordinator.RestoreAsync(request, CancellationToken.None));
        Assert.Equal(fixture.PostBytes, File.ReadAllBytes(fixture.TargetPath));
    }

    private static string ReadJournalState(string path)
    {
        return ReadJournalPayloadString(path, "state");
    }

    private static string ReadJournalPayloadString(string path, string name)
    {
        using var document = JsonDocument.Parse(File.ReadAllBytes(path));
        return document.RootElement
            .GetProperty("payload")
            .GetProperty(name)
            .GetString() ?? string.Empty;
    }

    private static string IssueRollbackToken(
        RestoreFixture fixture,
        string secret,
        string approvalId,
        string currentRevision,
        DateTimeOffset expiresAt)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(new Dictionary<string, string>
        {
            ["schema_version"] = "1.13",
            ["approval_id"] = approvalId,
            ["job_id"] = fixture.JobId,
            ["document_id"] = fixture.DocumentId,
            ["checkpoint_id"] = fixture.CheckpointId,
            ["current_revision"] = currentRevision,
            ["approved_by"] = "engineer@example.com",
            ["approved_at"] = expiresAt.AddMinutes(-1).ToString("O"),
            ["expires_at"] = expiresAt.ToString("O"),
        });
        var encoded = Convert.ToBase64String(payload)
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
        var signature = Convert.ToHexString(HMACSHA256.HashData(
            Encoding.UTF8.GetBytes(secret),
            Encoding.ASCII.GetBytes(encoded))).ToLowerInvariant();
        return $"rb1.{encoded}.{signature}";
    }

    private static byte[] DwgBytes(string value) =>
        Encoding.ASCII.GetBytes("AC1032" + value.PadRight(64, '-'));

    private sealed class RestoreFixture : IDisposable
    {
        private readonly TemporaryDirectory _root = new();

        public RestoreFixture()
        {
            CheckpointRoot = Path.Combine(_root.Path, "checkpoints");
            JournalRoot = Path.Combine(_root.Path, "journal");
            TargetDirectory = Path.Combine(_root.Path, "customer-drawings");
            Directory.CreateDirectory(CheckpointRoot);
            Directory.CreateDirectory(TargetDirectory);
            TargetPath = Path.Combine(TargetDirectory, "private-customer-drawing.dwg");
            PostBytes = DwgBytes("post-commit-drawing");
            CheckpointBytes = DwgBytes("checkpoint-drawing");
            File.WriteAllBytes(TargetPath, PostBytes);

            Catalog = new DurableCheckpointCatalog(CheckpointRoot, AuthenticationKey);
            var checkpointFileName = CheckpointId + ".dwg";
            File.WriteAllBytes(Path.Combine(CheckpointRoot, checkpointFileName), CheckpointBytes);
            Catalog.RegisterCheckpoint(
                CheckpointId,
                JobId,
                DocumentId,
                PreRevision,
                TargetPath,
                checkpointFileName,
                new DateTimeOffset(2026, 8, 15, 2, 3, 4, TimeSpan.Zero));
            Request = new DurableCheckpointRestoreRequest(
                new ValidatedRollbackAuthorization(
                    ApprovalId,
                    ApprovalTokenDigest,
                    JobId,
                    DocumentId,
                    CheckpointId,
                    PreRevision,
                    PostRevision),
                TargetPath);
        }

        public string ApprovalId { get; } = "approval-rb1-001";

        public string ApprovalTokenDigest { get; } = new('a', 64);

        public string JobId { get; } = "job-restore-001";

        public string DocumentId { get; } = "document-restore-001";

        public string CheckpointId { get; } = "checkpoint-restore-001";

        public string PreRevision { get; } = "sha256:pre-revision-001";

        public string PostRevision { get; } = "sha256:post-revision-002";

        public string CheckpointRoot { get; }

        public string JournalRoot { get; }

        public string TargetDirectory { get; }

        public string TargetPath { get; }

        public byte[] PostBytes { get; }

        public byte[] CheckpointBytes { get; }

        public DurableCheckpointCatalog Catalog { get; private set; }

        public DurableCheckpointRestoreRequest Request { get; }

        public FakeLifecycle CreateLifecycle(bool open = true)
        {
            var lifecycle = new FakeLifecycle(
                TargetPath,
                DocumentId,
                PostRevision,
                PreRevision,
                open);
            lifecycle.RevisionOnReopen = () =>
                File.ReadAllBytes(TargetPath).SequenceEqual(PostBytes)
                    ? PostRevision
                    : PreRevision;
            return lifecycle;
        }

        public void RestartCatalog()
        {
            Catalog = new DurableCheckpointCatalog(CheckpointRoot, AuthenticationKey);
        }

        public DurableCheckpointRestoreCoordinator CreateCoordinator(
            IDurableRestoreDocumentLifecycle lifecycle) => new(
                Catalog,
                CheckpointRoot,
                JournalRoot,
                AuthenticationKey,
                lifecycle);

        public void Dispose()
        {
            Catalog.Dispose();
            _root.Dispose();
        }
    }

    private sealed class FakeLifecycle : IDurableRestoreDocumentLifecycle
    {
        private readonly string _targetPath;
        private readonly string _documentId;
        private bool _open;

        public FakeLifecycle(
            string targetPath,
            string documentId,
            string postRevision,
            string preRevision,
            bool open)
        {
            _targetPath = targetPath;
            _documentId = documentId;
            CurrentRevision = postRevision;
            ReopenedRevision = preRevision;
            _open = open;
        }

        public string CurrentRevision { get; set; }

        public string ReopenedRevision { get; set; }

        public Func<string>? RevisionOnReopen { get; set; }

        public bool ThrowOnInspect { get; set; }

        public bool ThrowOnReopen { get; set; }

        public bool ThrowOnAnyCall { get; set; }

        public Action? OnInspect { get; set; }

        public Action? OnClose { get; set; }

        public int InspectCalls { get; private set; }

        public int CloseCalls { get; private set; }

        public int ReopenCalls { get; private set; }

        public int TotalCalls => InspectCalls + CloseCalls + ReopenCalls;

        public List<CancellationToken> DestructiveTokens { get; } = [];

        public ValueTask<DurableRestoreDocumentSnapshot> InspectAsync(
            string targetPath,
            CancellationToken cancellationToken)
        {
            InspectCalls++;
            if (ThrowOnAnyCall || ThrowOnInspect)
            {
                throw new IOException("Injected inspection failure.");
            }

            Assert.Equal(_targetPath, targetPath);
            OnInspect?.Invoke();
            cancellationToken.ThrowIfCancellationRequested();
            return ValueTask.FromResult(new DurableRestoreDocumentSnapshot(
                _documentId,
                CurrentRevision,
                DurableCheckpointCatalog.ComputeOriginalPathHash(_targetPath),
                _open));
        }

        public ValueTask CloseWithoutSaveAsync(
            string targetPath,
            CancellationToken cancellationToken)
        {
            CloseCalls++;
            if (ThrowOnAnyCall)
            {
                throw new IOException("Injected close failure.");
            }

            Assert.Equal(_targetPath, targetPath);
            DestructiveTokens.Add(cancellationToken);
            _open = false;
            OnClose?.Invoke();
            return ValueTask.CompletedTask;
        }

        public ValueTask ReopenAsync(string targetPath, CancellationToken cancellationToken)
        {
            ReopenCalls++;
            if (ThrowOnAnyCall || ThrowOnReopen)
            {
                throw new IOException("Injected reopen failure.");
            }

            Assert.Equal(_targetPath, targetPath);
            DestructiveTokens.Add(cancellationToken);
            _open = true;
            CurrentRevision = RevisionOnReopen?.Invoke() ?? ReopenedRevision;
            return ValueTask.CompletedTask;
        }
    }

    private sealed class TemporaryDirectory : IDisposable
    {
        public TemporaryDirectory()
        {
            Path = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                $"cad-bridge-restore-tests-{Guid.NewGuid():N}");
            Directory.CreateDirectory(Path);
        }

        public string Path { get; }

        public void Dispose()
        {
            if (Directory.Exists(Path))
            {
                Directory.Delete(Path, recursive: true);
            }
        }
    }
}
