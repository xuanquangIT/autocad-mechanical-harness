using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class WriteRuntimeGateTests
{
    [Theory]
    [InlineData(false, true, 10, "26.0", false)]
    [InlineData(true, false, 10, "26.0", false)]
    [InlineData(true, true, 8, "26.0", false)]
    [InlineData(true, true, 10, "25.0", false)]
    [InlineData(true, true, 10, "26.0", true)]
    [InlineData(true, true, 10, null, false)]
    public void AnyUnverifiedTupleComponentFailsClosed(
        bool deploymentRequested,
        bool verifiedBuildTuple,
        int runtimeMajor,
        string? detectedCadVersion,
        bool undoVerificationPending)
    {
        Assert.False(WriteRuntimeGate.IsEnabled(
            deploymentRequested,
            verifiedBuildTuple,
            runtimeMajor,
            detectedCadVersion,
            undoVerificationPending));
    }

    [Fact]
    public void ExactVerifiedR26Net10TupleCanEnableWrite()
    {
        Assert.True(WriteRuntimeGate.IsEnabled(
            deploymentRequested: true,
            verifiedBuildTuple: true,
            runtimeMajor: 10,
            detectedCadVersion: "26.0",
            undoVerificationPending: false));
    }
}
