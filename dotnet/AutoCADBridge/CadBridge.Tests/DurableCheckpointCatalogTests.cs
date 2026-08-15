using System.Text;
using System.Text.Json;
using CadBridge.Hosting;
using Xunit;

namespace CadBridge.Tests;

public sealed class DurableCheckpointCatalogTests
{
    private static readonly byte[] AuthenticationKey = Enumerable.Range(1, 32)
        .Select(value => checked((byte)value))
        .ToArray();

    [Fact]
    public void RegisteredCheckpointRoundTripsAcrossRestartWithoutSourcePathLeakage()
    {
        using var directory = new TemporaryDirectory();
        var sourcePath = Path.Combine(
            directory.Path,
            "customer-private",
            "secret-original-drawing.dwg");
        DurableCheckpointRecord registered;
        using (var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey))
        {
            WriteDwg(directory.Path, "checkpoint-001.dwg", "first-checkpoint");
            registered = catalog.RegisterCheckpoint(
                "checkpoint-001",
                "job-001",
                "document-001",
                "sha256:pre-revision-001",
                sourcePath,
                "checkpoint-001.dwg",
                new DateTimeOffset(2026, 8, 15, 1, 2, 3, TimeSpan.Zero));

            Assert.Equal(DurableCheckpointCatalog.CurrentCatalogSchema, registered.CatalogSchema);
            Assert.Equal(DurableCheckpointState.Available, registered.State);
            Assert.Equal("AC1032", registered.DwgVersion);
            Assert.Equal(64, registered.Sha256.Length);
            Assert.Equal(64, registered.OriginalPathHash.Length);
            Assert.False(Path.IsPathFullyQualified(registered.CheckpointFileName));
        }

        var serialized = File.ReadAllText(CatalogPath(directory.Path));
        Assert.DoesNotContain(sourcePath, serialized, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain(
            "secret-original-drawing.dwg",
            serialized,
            StringComparison.OrdinalIgnoreCase);
        Assert.Empty(Directory.EnumerateFiles(directory.Path, "*.tmp"));

        using var restarted = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        Assert.Equal(registered, restarted.GetRequired("checkpoint-001"));
    }

    [Fact]
    public void RestoringStateSurvivesRestartAndIsNeverAutomaticallyReplayed()
    {
        using var directory = new TemporaryDirectory();
        using (var first = CreateCatalogWithCheckpoint(directory, "checkpoint-restore"))
        {
            Assert.Equal(
                DurableCheckpointState.Restoring,
                first.BeginRestore("checkpoint-restore").State);
        }

        using var restarted = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        Assert.Equal(
            DurableCheckpointState.Restoring,
            restarted.GetRequired("checkpoint-restore").State);
        Assert.Equal(
            DurableCheckpointState.Consumed,
            restarted.Complete("checkpoint-restore").State);
    }

    [Fact]
    public void TamperedAuthenticatedEnvelopeFailsClosed()
    {
        using var directory = new TemporaryDirectory();
        using (var catalog = CreateCatalogWithCheckpoint(directory, "checkpoint-tamper"))
        {
            Assert.Single(catalog.Snapshot());
        }

        var path = CatalogPath(directory.Path);
        var content = File.ReadAllText(path);
        File.WriteAllText(
            path,
            content.Replace("available", "restoring", StringComparison.Ordinal));

        Assert.Throws<InvalidDataException>(() =>
            new DurableCheckpointCatalog(directory.Path, AuthenticationKey));
    }

    [Fact]
    public void TruncatedAuthenticatedEnvelopeFailsClosed()
    {
        using var directory = new TemporaryDirectory();
        using (var catalog = CreateCatalogWithCheckpoint(directory, "checkpoint-truncated"))
        {
            Assert.Single(catalog.Snapshot());
        }

        var path = CatalogPath(directory.Path);
        var content = File.ReadAllText(path);
        File.WriteAllText(path, content[..(content.Length / 2)]);

        Assert.Throws<InvalidDataException>(() =>
            new DurableCheckpointCatalog(directory.Path, AuthenticationKey));
    }

    [Fact]
    public void WrongAuthenticationKeyFailsClosed()
    {
        using var directory = new TemporaryDirectory();
        using (var catalog = CreateCatalogWithCheckpoint(directory, "checkpoint-key"))
        {
            Assert.Single(catalog.Snapshot());
        }

        var differentKey = Enumerable.Repeat((byte)0xA5, 32).ToArray();
        Assert.Throws<InvalidDataException>(() =>
            new DurableCheckpointCatalog(directory.Path, differentKey));
    }

    [Fact]
    public void AuthenticationKeyMustContainAtLeastThirtyTwoBytes()
    {
        using var directory = new TemporaryDirectory();

        Assert.Throws<ArgumentException>(() =>
            new DurableCheckpointCatalog(directory.Path, new byte[31]));
        Assert.Empty(Directory.EnumerateFileSystemEntries(directory.Path));
    }

    [Fact]
    public void DuplicateCheckpointIdAndFilenameAreRejectedWithoutOverwrite()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        WriteDwg(directory.Path, "checkpoint-a.dwg", "checkpoint-a");
        WriteDwg(directory.Path, "checkpoint-b.dwg", "checkpoint-b");
        Register(catalog, directory.Path, "checkpoint-a", "checkpoint-a.dwg");

        Assert.Throws<InvalidOperationException>(() =>
            Register(catalog, directory.Path, "checkpoint-a", "checkpoint-b.dwg"));
        Assert.Throws<InvalidOperationException>(() =>
            Register(catalog, directory.Path, "checkpoint-b", "checkpoint-a.dwg"));
        var only = Assert.Single(catalog.Snapshot());
        Assert.Equal("checkpoint-a", only.CheckpointId);
        Assert.Equal("checkpoint-a.dwg", only.CheckpointFileName);
    }

    [Fact]
    public void TraversalAbsoluteAndNonDwgCheckpointNamesAreRejected()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        var absolute = Path.Combine(directory.Path, "outside.dwg");

        Assert.Throws<ArgumentException>(() =>
            Register(catalog, directory.Path, "checkpoint-a", "..\\outside.dwg"));
        Assert.Throws<ArgumentException>(() =>
            Register(catalog, directory.Path, "checkpoint-b", absolute));
        Assert.Throws<ArgumentException>(() =>
            Register(catalog, directory.Path, "checkpoint-c", "checkpoint-c.dxf"));
        Assert.Empty(catalog.Snapshot());
    }

    [Theory]
    [InlineData("")]
    [InlineData("relative-checkpoint-root")]
    [InlineData("\\\\server\\share\\cad-harness-checkpoints")]
    [InlineData("\\\\?\\C:\\cad-harness-checkpoints")]
    public void RootMustBeAbsoluteLocalAndNonDevice(string root)
    {
        Assert.Throws<ArgumentException>(() =>
            new DurableCheckpointCatalog(root, AuthenticationKey));
    }

    [Fact]
    public void ReparsePointRootIsRejectedWhenLinksAreSupported()
    {
        using var parent = new TemporaryDirectory();
        var target = Path.Combine(parent.Path, "target");
        var link = Path.Combine(parent.Path, "root-link");
        Directory.CreateDirectory(target);
        if (!TryCreateDirectorySymbolicLink(link, target))
        {
            return;
        }

        Assert.Throws<InvalidDataException>(() =>
            new DurableCheckpointCatalog(link, AuthenticationKey));
    }

    [Fact]
    public void ReparsePointCheckpointFileIsRejectedWhenLinksAreSupported()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        var target = Path.Combine(directory.Path, "real-checkpoint.bin");
        File.WriteAllBytes(target, DwgBytes("real-checkpoint"));
        var link = Path.Combine(directory.Path, "linked-checkpoint.dwg");
        if (!TryCreateFileSymbolicLink(link, target))
        {
            return;
        }

        Assert.Throws<InvalidDataException>(() =>
            Register(catalog, directory.Path, "checkpoint-link", "linked-checkpoint.dwg"));
        Assert.Empty(catalog.Snapshot());
    }

    [Fact]
    public void MissingAndHashMismatchedActiveArtifactsAreQuarantinedAtStartup()
    {
        using var directory = new TemporaryDirectory();
        using (var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey))
        {
            WriteDwg(directory.Path, "checkpoint-missing.dwg", "will-be-missing");
            WriteDwg(directory.Path, "checkpoint-mismatch.dwg", "will-be-modified");
            Register(
                catalog,
                directory.Path,
                "checkpoint-missing",
                "checkpoint-missing.dwg");
            Register(
                catalog,
                directory.Path,
                "checkpoint-mismatch",
                "checkpoint-mismatch.dwg");
        }

        File.Delete(Path.Combine(directory.Path, "checkpoint-missing.dwg"));
        WriteDwg(directory.Path, "checkpoint-mismatch.dwg", "modified-after-registration");

        using (var restarted = new DurableCheckpointCatalog(directory.Path, AuthenticationKey))
        {
            Assert.All(
                restarted.Snapshot(),
                record => Assert.Equal(DurableCheckpointState.Quarantined, record.State));
            Assert.Throws<InvalidOperationException>(() =>
                restarted.BeginRestore("checkpoint-missing"));
            Assert.Throws<InvalidOperationException>(() =>
                restarted.BeginRestore("checkpoint-mismatch"));
        }

        using var secondRestart = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        Assert.All(
            secondRestart.Snapshot(),
            record => Assert.Equal(DurableCheckpointState.Quarantined, record.State));
    }

    [Fact]
    public void RestoreStateTransitionsHaveExactIdempotencyAndNeverRevertConsumed()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = CreateCatalogWithCheckpoint(directory, "checkpoint-state");

        var generation = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Restoring,
            catalog.BeginRestore("checkpoint-state").State);
        Assert.Equal(generation + 1, ReadGeneration(directory.Path));
        var restoringGeneration = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Restoring,
            catalog.BeginRestore("checkpoint-state").State);
        Assert.Equal(restoringGeneration, ReadGeneration(directory.Path));

        Assert.Equal(
            DurableCheckpointState.Available,
            catalog.CancelBeforeReplacement("checkpoint-state").State);
        var availableGeneration = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Available,
            catalog.CancelBeforeReplacement("checkpoint-state").State);
        Assert.Equal(availableGeneration, ReadGeneration(directory.Path));

        catalog.BeginRestore("checkpoint-state");
        Assert.Equal(
            DurableCheckpointState.Consumed,
            catalog.Complete("checkpoint-state").State);
        var consumedGeneration = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Consumed,
            catalog.Complete("checkpoint-state").State);
        Assert.Equal(consumedGeneration, ReadGeneration(directory.Path));
        Assert.Throws<InvalidOperationException>(() =>
            catalog.BeginRestore("checkpoint-state"));
        Assert.Throws<InvalidOperationException>(() =>
            catalog.CancelBeforeReplacement("checkpoint-state"));
        Assert.Throws<InvalidOperationException>(() =>
            catalog.Quarantine("checkpoint-state"));
        Assert.Throws<InvalidOperationException>(() => catalog.Expire("checkpoint-state"));
    }

    [Fact]
    public void QuarantineAndExpirationAreTerminalAndIdempotent()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        WriteDwg(directory.Path, "checkpoint-quarantine.dwg", "quarantine");
        WriteDwg(directory.Path, "checkpoint-expire.dwg", "expire");
        Register(
            catalog,
            directory.Path,
            "checkpoint-quarantine",
            "checkpoint-quarantine.dwg");
        Register(
            catalog,
            directory.Path,
            "checkpoint-expire",
            "checkpoint-expire.dwg");

        Assert.Equal(
            DurableCheckpointState.Quarantined,
            catalog.Quarantine("checkpoint-quarantine").State);
        var quarantinedGeneration = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Quarantined,
            catalog.Quarantine("checkpoint-quarantine").State);
        Assert.Equal(quarantinedGeneration, ReadGeneration(directory.Path));

        Assert.Equal(
            DurableCheckpointState.Expired,
            catalog.Expire("checkpoint-expire").State);
        var expiredGeneration = ReadGeneration(directory.Path);
        Assert.Equal(
            DurableCheckpointState.Expired,
            catalog.Expire("checkpoint-expire").State);
        Assert.Equal(expiredGeneration, ReadGeneration(directory.Path));
        Assert.Throws<InvalidOperationException>(() =>
            catalog.BeginRestore("checkpoint-quarantine"));
        Assert.Throws<InvalidOperationException>(() =>
            catalog.BeginRestore("checkpoint-expire"));
    }

    [Fact]
    public void CatalogReplayIsRejectedAcrossRestartByAuthenticatedWatermark()
    {
        using var directory = new TemporaryDirectory();
        byte[] previousGeneration;
        using (var catalog = CreateCatalogWithCheckpoint(directory, "checkpoint-replay"))
        {
            previousGeneration = File.ReadAllBytes(CatalogPath(directory.Path));
            catalog.BeginRestore("checkpoint-replay");
        }

        File.WriteAllBytes(CatalogPath(directory.Path), previousGeneration);

        Assert.Throws<InvalidDataException>(() =>
            new DurableCheckpointCatalog(directory.Path, AuthenticationKey));
    }

    [Fact]
    public void FailedAtomicReplacementDoesNotReportOrRetainFalseSuccess()
    {
        using var directory = new TemporaryDirectory();
        using var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        WriteDwg(directory.Path, "checkpoint-write-failure.dwg", "write-failure");
        var catalogPath = CatalogPath(directory.Path);

        using (new FileStream(
                   catalogPath,
                   FileMode.Open,
                   FileAccess.Read,
                   FileShare.Read))
        {
            Assert.ThrowsAny<IOException>(() => Register(
                catalog,
                directory.Path,
                "checkpoint-write-failure",
                "checkpoint-write-failure.dwg"));
        }

        Assert.Empty(catalog.Snapshot());
        Assert.Empty(Directory.EnumerateFiles(directory.Path, "*.tmp"));
    }

    [Fact]
    public async Task ConcurrentCatalogInstancesDoNotLoseUpdatesOrDuplicateRestore()
    {
        using var directory = new TemporaryDirectory();
        using var first = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        WriteDwg(directory.Path, "checkpoint-concurrent-a.dwg", "concurrent-a");
        WriteDwg(directory.Path, "checkpoint-concurrent-b.dwg", "concurrent-b");
        using var second = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);

        await Task.WhenAll(
            Task.Run(() => Register(
                first,
                directory.Path,
                "checkpoint-concurrent-a",
                "checkpoint-concurrent-a.dwg")),
            Task.Run(() => Register(
                second,
                directory.Path,
                "checkpoint-concurrent-b",
                "checkpoint-concurrent-b.dwg")));

        Assert.Equal(2, first.Snapshot().Count);
        Assert.Equal(2, second.Snapshot().Count);
        var transitions = await Task.WhenAll(
            Task.Run(() => first.BeginRestore("checkpoint-concurrent-a")),
            Task.Run(() => second.BeginRestore("checkpoint-concurrent-a")));
        Assert.All(
            transitions,
            record => Assert.Equal(DurableCheckpointState.Restoring, record.State));
        Assert.Equal(
            DurableCheckpointState.Restoring,
            first.GetRequired("checkpoint-concurrent-a").State);
    }

    [Fact]
    public void OriginalPathHashIsStableForEquivalentWindowsPathCase()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var lower = DurableCheckpointCatalog.ComputeOriginalPathHash(
            @"C:\customers\private\drawing.dwg");
        var upper = DurableCheckpointCatalog.ComputeOriginalPathHash(
            @"C:\CUSTOMERS\PRIVATE\DRAWING.DWG");

        Assert.Equal(lower, upper);
        Assert.Equal(64, lower.Length);
    }

    private static DurableCheckpointCatalog CreateCatalogWithCheckpoint(
        TemporaryDirectory directory,
        string checkpointId)
    {
        var catalog = new DurableCheckpointCatalog(directory.Path, AuthenticationKey);
        var fileName = checkpointId + ".dwg";
        WriteDwg(directory.Path, fileName, checkpointId);
        Register(catalog, directory.Path, checkpointId, fileName);
        return catalog;
    }

    private static DurableCheckpointRecord Register(
        DurableCheckpointCatalog catalog,
        string root,
        string checkpointId,
        string checkpointFileName) =>
        catalog.RegisterCheckpoint(
            checkpointId,
            "job-" + checkpointId,
            "document-" + checkpointId,
            "sha256:pre-" + checkpointId,
            Path.Combine(root, "private-source", checkpointId + ".dwg"),
            checkpointFileName,
            new DateTimeOffset(2026, 8, 15, 1, 2, 3, TimeSpan.Zero));

    private static void WriteDwg(string root, string fileName, string payload) =>
        File.WriteAllBytes(Path.Combine(root, fileName), DwgBytes(payload));

    private static byte[] DwgBytes(string payload) =>
        Encoding.ASCII.GetBytes("AC1032" + payload.PadRight(32, '-'));

    private static string CatalogPath(string root) =>
        Assert.Single(Directory.EnumerateFiles(root, "checkpoint-catalog.*.json"));

    private static long ReadGeneration(string root)
    {
        using var document = JsonDocument.Parse(File.ReadAllBytes(CatalogPath(root)));
        return document.RootElement
            .GetProperty("payload")
            .GetProperty("generation")
            .GetInt64();
    }

    private static bool TryCreateDirectorySymbolicLink(string link, string target)
    {
        try
        {
            Directory.CreateSymbolicLink(link, target);
            return true;
        }
        catch (Exception exception) when (exception is UnauthorizedAccessException or
            IOException or PlatformNotSupportedException)
        {
            return false;
        }
    }

    private static bool TryCreateFileSymbolicLink(string link, string target)
    {
        try
        {
            File.CreateSymbolicLink(link, target);
            return true;
        }
        catch (Exception exception) when (exception is UnauthorizedAccessException or
            IOException or PlatformNotSupportedException)
        {
            return false;
        }
    }

    private sealed class TemporaryDirectory : IDisposable
    {
        public TemporaryDirectory()
        {
            Path = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                $"cad-bridge-checkpoint-catalog-tests-{Guid.NewGuid():N}");
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
}
