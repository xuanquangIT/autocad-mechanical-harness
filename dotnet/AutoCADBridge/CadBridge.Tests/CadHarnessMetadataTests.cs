using CadBridge.Execution;
using CadBridge.Metadata;
using Xunit;

namespace CadBridge.Tests;

public sealed class CadHarnessMetadataTests
{
    [Fact]
    public async Task AttachmentUsesTheCallersActiveTransactionAndFixedTypedMetadata()
    {
        var writer = new RecordingWriter();
        var service = new MetadataAttachmentService(writer);
        var transaction = new TestTransaction { IsActive = true };
        var entity = new TestEntity();

        await service.AttachImmediatelyAfterCreationAsync(
            transaction,
            entity,
            "feature:plate-01",
            "operation_outline-01",
            CancellationToken.None);

        Assert.Same(transaction, writer.Transaction);
        Assert.Same(entity, writer.Entity);
        Assert.Equal("feature:plate-01", writer.Metadata?.FeatureId);
        Assert.Equal("operation_outline-01", writer.Metadata?.OperationId);
        Assert.Equal("CADHARNESS", CadHarnessMetadataRegistry.ApplicationName);
        Assert.Equal(0, transaction.CommitCalls);
        Assert.Equal(0, transaction.AbortCalls);
    }

    [Theory]
    [InlineData("")]
    [InlineData("-starts-with-separator")]
    [InlineData("ends-with-separator-")]
    [InlineData("contains space")]
    [InlineData("contains/slash")]
    [InlineData("contains\nuntrusted")]
    public void InvalidOpaqueIdentifiersAreRejectedBeforeWriterAccess(string invalid)
    {
        Assert.ThrowsAny<ArgumentException>(() => CadHarnessMetadata.Create(invalid, "operation-1"));
        Assert.ThrowsAny<ArgumentException>(() => CadHarnessMetadata.Create("feature-1", invalid));
    }

    [Fact]
    public async Task InactiveTransactionAndPreCancelledRequestNeverReachWriter()
    {
        var writer = new RecordingWriter();
        var service = new MetadataAttachmentService(writer);
        var entity = new TestEntity();

        await Assert.ThrowsAsync<InvalidOperationException>(async () =>
            await service.AttachImmediatelyAfterCreationAsync(
                new TestTransaction { IsActive = false },
                entity,
                "feature-1",
                "operation-1",
                CancellationToken.None));

        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        await Assert.ThrowsAsync<OperationCanceledException>(async () =>
            await service.AttachImmediatelyAfterCreationAsync(
                new TestTransaction { IsActive = true },
                entity,
                "feature-1",
                "operation-1",
                cancellation.Token));

        Assert.Equal(0, writer.AttachCalls);
    }

    private sealed class TestEntity : IMetadataEntityReference;

    private sealed class TestTransaction : IAtomicTransaction, IActiveMetadataTransactionAccess
    {
        public bool IsActive { get; init; }

        public int CommitCalls { get; private set; }

        public int AbortCalls { get; private set; }

        public void Commit() => CommitCalls++;

        public void Abort() => AbortCalls++;

        public void Dispose()
        {
        }
    }

    private sealed class RecordingWriter : IMetadataWriter
    {
        public int AttachCalls { get; private set; }

        public IActiveMetadataTransactionAccess? Transaction { get; private set; }

        public IMetadataEntityReference? Entity { get; private set; }

        public CadHarnessMetadata? Metadata { get; private set; }

        public ValueTask AttachAsync(
            IActiveMetadataTransactionAccess activeTransaction,
            IMetadataEntityReference newlyCreatedEntity,
            CadHarnessMetadata metadata,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            AttachCalls++;
            Transaction = activeTransaction;
            Entity = newlyCreatedEntity;
            Metadata = metadata;
            return ValueTask.CompletedTask;
        }

        public ValueTask<CadHarnessMetadata?> ReadAsync(
            IActiveMetadataTransactionAccess activeTransaction,
            IMetadataEntityReference entity,
            CancellationToken cancellationToken) =>
            ValueTask.FromResult<CadHarnessMetadata?>(null);
    }
}
