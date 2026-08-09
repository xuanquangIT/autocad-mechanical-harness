using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class OperationFailureDiagnosticsTests
{
    [Fact]
    public async Task ConcurrentRequestsPublishTheirOwnCompletedFailureStage()
    {
        var diagnostics = new OperationFailureDiagnostics();
        var firstStarted = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var secondCompleted = new TaskCompletionSource(
            TaskCreationOptions.RunContinuationsAsynchronously);

        var first = Task.Run(async () =>
        {
            var request = diagnostics.Begin("first.start");
            request.RecordStage("first.snapshot");
            firstStarted.SetResult();
            await secondCompleted.Task;
            Assert.Equal("first.snapshot", request.Stage);
            request.PublishFailure($"{request.Stage}:FirstFailure");
        });
        var second = Task.Run(async () =>
        {
            await firstStarted.Task;
            var request = diagnostics.Begin("second.start");
            request.RecordStage("second.convert");
            Assert.Equal("second.convert", request.Stage);
            request.PublishFailure($"{request.Stage}:SecondFailure");
            secondCompleted.SetResult();
        });

        await Task.WhenAll(first, second);

        Assert.Equal("first.snapshot:FirstFailure", diagnostics.LastFailure);
    }

    [Fact]
    public void SuccessfulRequestDoesNotClearAnotherRequestsFailure()
    {
        var diagnostics = new OperationFailureDiagnostics();
        var failed = diagnostics.Begin("failed.stage");
        failed.PublishFailure("failed.stage:Failure");

        var successful = diagnostics.Begin("successful.stage");
        successful.RecordStage("successful.complete");

        Assert.Equal("failed.stage:Failure", diagnostics.LastFailure);
    }
}
