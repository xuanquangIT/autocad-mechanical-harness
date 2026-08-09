using System.Text.Json;
using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class UndoRollbackRegistryTests
{
    [Fact]
    public void ConfirmedRollbackReplaysOnlyForSameApprovalAndDigest()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var receipt = registry.Register(Receipt());
        var request = Request(receipt);

        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(request).Kind);
        var stored = registry.CompleteSuccess(request, Data("restored"));
        var replay = registry.Begin(request);

        Assert.Equal(UndoRollbackState.Consumed, registry.GetState(receipt.ReceiptId));
        Assert.Equal(UndoRollbackBeginKind.Replay, replay.Kind);
        Assert.Equal(stored, replay.Result);
        Assert.Equal("restored", replay.Result!.Data.GetProperty("outcome").GetString());
        Assert.Equal(
            UndoRollbackBeginKind.Rejected,
            registry.Begin(request with { CurrentRevision = "scope-changed" }).Kind);
        Assert.Equal(
            UndoRollbackBeginKind.Conflict,
            registry.Begin(request with { RequestDigest = "digest-changed" }).Kind);
    }

    [Fact]
    public void ExactScopeAndEpochAreRequiredAndRestartCannotReactivateReceipt()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var receipt = registry.Register(Receipt());
        var request = Request(receipt);

        foreach (var wrongScope in new[]
        {
            request with { UndoGroup = "undo-other" },
            request with { JobId = "job-other" },
            request with { DocumentId = "doc-other" },
            request with { CheckpointId = "checkpoint-other" },
            request with { CurrentRevision = "revision-other" },
            request with { ProcessEpoch = "epoch-other" },
        })
        {
            Assert.Equal(UndoRollbackBeginKind.Rejected, registry.Begin(wrongScope).Kind);
        }

        var restarted = new UndoRollbackRegistry("epoch-2");
        Assert.Equal(UndoRollbackBeginKind.Rejected, restarted.Begin(request).Kind);
    }

    [Fact]
    public void FreshCommitSupersedesAvailableReceiptForSameDocument()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var first = registry.Register(Receipt());
        var second = registry.Register(Receipt("receipt-2", "undo-2", "checkpoint-2", "revision-2"));

        Assert.Equal(UndoRollbackState.Superseded, registry.GetState(first.ReceiptId));
        Assert.Equal(UndoRollbackState.Available, registry.GetState(second.ReceiptId));
        Assert.Equal(UndoRollbackBeginKind.Rejected, registry.Begin(Request(first)).Kind);
        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(Request(second)).Kind);
    }

    [Fact]
    public void PostCommandUncertaintyQuarantinesAndNeverExecutesAgain()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var receipt = registry.Register(Receipt());
        var request = Request(receipt);

        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(request).Kind);
        registry.QuarantineAfterCommandUncertainty(request);

        Assert.Equal(UndoRollbackState.Quarantined, registry.GetState(receipt.ReceiptId));
        Assert.Equal(UndoRollbackBeginKind.Rejected, registry.Begin(request).Kind);
        Assert.Equal(
            UndoRollbackBeginKind.Rejected,
            registry.Begin(request with { ApprovalId = "approval-fresh", RequestDigest = "digest-fresh" }).Kind);
    }

    [Fact]
    public void PreCommandCancellationReleasesReceiptWithoutBurningApprovalId()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var receipt = registry.Register(Receipt());
        var request = Request(receipt);

        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(request).Kind);
        registry.CancelBeforeCommand(request);

        Assert.Equal(UndoRollbackState.Available, registry.GetState(receipt.ReceiptId));
        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(request).Kind);
    }

    [Fact]
    public void InterveningCommandInvalidatesOnlyAvailableReceiptForThatDocument()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var target = registry.Register(Receipt());
        var unrelated = registry.Register(Receipt(
            "receipt-unrelated",
            "undo-unrelated",
            "checkpoint-unrelated",
            "revision-unrelated") with
        { DocumentId = "doc-unrelated" });

        Assert.True(registry.InvalidateAvailableForDocument(target.DocumentId));
        Assert.False(registry.InvalidateAvailableForDocument(target.DocumentId));
        Assert.False(registry.InvalidateAvailableForDocument("doc-missing"));
        Assert.Equal(UndoRollbackState.Superseded, registry.GetState(target.ReceiptId));
        Assert.Equal(UndoRollbackState.Available, registry.GetState(unrelated.ReceiptId));
        Assert.Equal(UndoRollbackBeginKind.Rejected, registry.Begin(Request(target)).Kind);
        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(Request(unrelated)).Kind);
    }

    [Fact]
    public void InterveningCommandDoesNotAlterExecutingOrConsumedReceipt()
    {
        var registry = new UndoRollbackRegistry("epoch-1");
        var receipt = registry.Register(Receipt());
        var request = Request(receipt);

        Assert.Equal(UndoRollbackBeginKind.Execute, registry.Begin(request).Kind);
        Assert.False(registry.InvalidateAvailableForDocument(receipt.DocumentId));
        Assert.Equal(UndoRollbackState.Executing, registry.GetState(receipt.ReceiptId));

        registry.CompleteSuccess(request, Data("restored"));
        Assert.False(registry.InvalidateAvailableForDocument(receipt.DocumentId));
        Assert.Equal(UndoRollbackState.Consumed, registry.GetState(receipt.ReceiptId));
    }

    [Theory]
    [InlineData("revision-before", 1, UndoRollbackRevisionFenceDecision.Restored)]
    [InlineData("revision-before", 2, UndoRollbackRevisionFenceDecision.Restored)]
    [InlineData("revision-after", 1, UndoRollbackRevisionFenceDecision.RetryOnlyWhenUnchangedOnAttempt1)]
    [InlineData("revision-after", 2, UndoRollbackRevisionFenceDecision.Quarantine)]
    [InlineData("revision-third", 1, UndoRollbackRevisionFenceDecision.Quarantine)]
    [InlineData("revision-third", 2, UndoRollbackRevisionFenceDecision.Quarantine)]
    public void RevisionFenceExhaustivelyAllowsOnlyOneUnchangedRetry(
        string observed,
        int attempt,
        UndoRollbackRevisionFenceDecision expected)
    {
        Assert.Equal(
            expected,
            UndoRollbackRevisionFence.Decide(
                observed,
                "revision-after",
                "revision-before",
                attempt));
    }

    [Fact]
    public void RevisionFenceQuarantinesAttemptOutsideBound()
    {
        Assert.Equal(
            UndoRollbackRevisionFenceDecision.Quarantine,
            UndoRollbackRevisionFence.Decide(
                "revision-after",
                "revision-after",
                "revision-before",
                3));
    }

    private static UndoRollbackReceipt Receipt(
        string receiptId = "receipt-1",
        string undoGroup = "undo-1",
        string checkpointId = "checkpoint-1",
        string newRevision = "revision-1") =>
        new(
            receiptId,
            undoGroup,
            "job-1",
            "doc-1",
            checkpointId,
            "revision-before",
            newRevision,
            "epoch-1");

    private static UndoRollbackRequest Request(UndoRollbackReceipt receipt) =>
        new(
            receipt.ReceiptId,
            receipt.UndoGroup,
            receipt.JobId,
            receipt.DocumentId,
            receipt.CheckpointId,
            receipt.NewRevision,
            receipt.ProcessEpoch,
            "approval-1",
            "digest-1");

    private static JsonElement Data(string outcome) =>
        JsonSerializer.SerializeToElement(new { outcome });
}
