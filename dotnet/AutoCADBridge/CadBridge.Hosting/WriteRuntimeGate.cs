namespace CadBridge.Hosting;

/// <summary>Fail-closed deployment gate for the only live-verified writer tuple.</summary>
public static class WriteRuntimeGate
{
    public static bool IsEnabled(
        bool deploymentRequested,
        bool verifiedBuildTuple,
        int runtimeMajor,
        string? detectedCadVersion,
        bool undoVerificationPending) =>
        deploymentRequested &&
        verifiedBuildTuple &&
        runtimeMajor == 10 &&
        string.Equals(detectedCadVersion, "26.0", StringComparison.Ordinal) &&
        !undoVerificationPending;
}
