using System.Text;
using Autodesk.AutoCAD.ApplicationServices;
using CadBridge.Hosting;
using CadBridge.Inspection;

namespace CadBridge.Plugin;

/// <summary>
/// Exact-path AutoCAD document lifecycle used by whole-DWG checkpoint restoration. Every call is
/// serialized and marshalled to AutoCAD's application context. This implementation never selects
/// the active document and never resolves a drawing from only its file name.
/// </summary>
public sealed class AutoCadDurableRestoreDocumentLifecycle : IDurableRestoreDocumentLifecycle
{
    private const int MaximumBlockDepth = 10;
    private static readonly StringComparison PathComparison = OperatingSystem.IsWindows()
        ? StringComparison.OrdinalIgnoreCase
        : StringComparison.Ordinal;

    private readonly DocumentCollection _documents;
    private readonly SemaphoreSlim _lifecycleGate = new(1, 1);

    public AutoCadDurableRestoreDocumentLifecycle(DocumentCollection documents)
    {
        ArgumentNullException.ThrowIfNull(documents);
        _documents = documents;
    }

    public ValueTask<DurableRestoreDocumentSnapshot> InspectAsync(
        string targetPath,
        CancellationToken cancellationToken) =>
        InApplicationContextAsync(
            targetPath,
            static (lifecycle, canonicalPath, token) =>
                lifecycle.InspectInApplicationContext(canonicalPath, token),
            cancellationToken);

    public async ValueTask CloseWithoutSaveAsync(
        string targetPath,
        CancellationToken cancellationToken)
    {
        _ = await InApplicationContextAsync(
            targetPath,
            static (lifecycle, canonicalPath, token) =>
            {
                var document = lifecycle.FindExactOpenDocument(canonicalPath)
                    ?? throw new InvalidOperationException(
                        "The exact restore target is not open in AutoCAD.");
                EnsureWritableDocument(document);

                // This is the final cancellation boundary. Once CloseAndDiscard starts, the
                // coordinator must continue through replacement and reopen without cancellation.
                token.ThrowIfCancellationRequested();
                DocumentExtension.CloseAndDiscard(document);
                return true;
            },
            cancellationToken);
    }

    public async ValueTask ReopenAsync(
        string targetPath,
        CancellationToken cancellationToken)
    {
        _ = await InApplicationContextAsync(
            targetPath,
            static (lifecycle, canonicalPath, token) =>
            {
                var existing = lifecycle.FindExactOpenDocument(canonicalPath);
                if (existing is not null)
                {
                    EnsureWritableDocument(existing);
                    return true;
                }

                EnsureWritableFile(canonicalPath);
                token.ThrowIfCancellationRequested();
                lifecycle._documents.AppContextOpenDocument(canonicalPath);

                // Do not observe caller cancellation after AutoCAD has opened the document. The
                // resulting exact collection member must be validated to a terminal outcome for
                // coordinator recovery.
                var reopened = lifecycle.FindExactOpenDocument(canonicalPath)
                    ?? throw new InvalidOperationException(
                        "AutoCAD did not bind the reopened restore target.");
                lifecycle.EnsureExactDocument(reopened, canonicalPath);
                EnsureWritableDocument(reopened);
                return true;
            },
            cancellationToken);
    }

    private DurableRestoreDocumentSnapshot InspectInApplicationContext(
        string canonicalPath,
        CancellationToken cancellationToken)
    {
        var originalPathHash = DurableCheckpointCatalog.ComputeOriginalPathHash(canonicalPath);
        var document = FindExactOpenDocument(canonicalPath);
        if (document is null)
        {
            return new DurableRestoreDocumentSnapshot(
                DocumentId: string.Empty,
                Revision: string.Empty,
                OriginalPathHash: originalPathHash,
                IsOpen: false);
        }

        EnsureWritableDocument(document);
        cancellationToken.ThrowIfCancellationRequested();
        using var documentLock = document.LockDocument();
        using var bound = new AutoCadInspectionDocument(document, MaximumBlockDepth);
        var snapshot = new BridgeInspectionService(bound).InspectDocument(cancellationToken);
        return new DurableRestoreDocumentSnapshot(
            snapshot.DocumentId,
            snapshot.Revision,
            originalPathHash,
            IsOpen: true);
    }

    private async ValueTask<TResult> InApplicationContextAsync<TResult>(
        string targetPath,
        Func<AutoCadDurableRestoreDocumentLifecycle, string, CancellationToken, TResult> action,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(action);
        var canonicalPath = RequireCanonicalLocalDwg(targetPath);
        EnsureWritableFile(canonicalPath);
        cancellationToken.ThrowIfCancellationRequested();
        await _lifecycleGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            // Revalidate under the serialized gate, then bind cancellation before dispatch. The
            // callback also observes this token before touching the AutoCAD document collection.
            canonicalPath = RequireCanonicalLocalDwg(canonicalPath);
            EnsureWritableFile(canonicalPath);
            cancellationToken.ThrowIfCancellationRequested();
            var invocation = new ApplicationContextInvocation<TResult>(
                () =>
                {
                    var callbackPath = RequireCanonicalLocalDwg(canonicalPath);
                    EnsureWritableFile(callbackPath);
                    return action(this, callbackPath, cancellationToken);
                },
                cancellationToken);
            _documents.ExecuteInApplicationContext(
                static state =>
                    ((ApplicationContextInvocation<TResult>)(state ??
                        throw new InvalidOperationException(
                            "AutoCAD omitted application-context state."))).Invoke(),
                invocation);
            return await invocation.Completion.ConfigureAwait(false);
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    private Document? FindExactOpenDocument(string canonicalPath)
    {
        Document? match = null;
        foreach (Document document in _documents)
        {
            var documentName = document.Name;
            if (string.IsNullOrWhiteSpace(documentName) ||
                !Path.IsPathFullyQualified(documentName))
            {
                continue;
            }

            string resolvedDocumentPath;
            try
            {
                resolvedDocumentPath = Path.GetFullPath(documentName).Normalize(
                    NormalizationForm.FormC);
            }
            catch (Exception exception) when (exception is ArgumentException or
                NotSupportedException)
            {
                continue;
            }

            if (!string.Equals(resolvedDocumentPath, canonicalPath, PathComparison))
            {
                continue;
            }

            if (!documentName.IsNormalized(NormalizationForm.FormC) ||
                !string.Equals(documentName, resolvedDocumentPath, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "The open restore target does not have an exact canonical path.");
            }

            EnsureExactDocument(document, canonicalPath);
            if (match is not null)
            {
                throw new InvalidOperationException(
                    "More than one AutoCAD document resolves to the restore target.");
            }

            match = document;
        }

        return match;
    }

    private void EnsureExactDocument(Document document, string canonicalPath)
    {
        if (!document.IsNamedDrawing || string.IsNullOrWhiteSpace(document.Name) ||
            !Path.IsPathFullyQualified(document.Name))
        {
            throw new InvalidOperationException(
                "The restore target must be a saved, named AutoCAD drawing.");
        }

        var resolved = Path.GetFullPath(document.Name).Normalize(NormalizationForm.FormC);
        if (!document.Name.IsNormalized(NormalizationForm.FormC) ||
            !string.Equals(document.Name, resolved, StringComparison.Ordinal) ||
            !string.Equals(resolved, canonicalPath, PathComparison))
        {
            throw new InvalidOperationException(
                "AutoCAD returned a document outside the exact restore target path.");
        }
    }

    private static string RequireCanonicalLocalDwg(string targetPath)
    {
        if (string.IsNullOrWhiteSpace(targetPath) ||
            !Path.IsPathFullyQualified(targetPath) ||
            !targetPath.IsNormalized(NormalizationForm.FormC))
        {
            throw new ArgumentException(
                "The restore target must be a canonical absolute local DWG path.",
                nameof(targetPath));
        }

        RejectNetworkOrDevicePath(targetPath, nameof(targetPath));
        string fullPath;
        try
        {
            fullPath = Path.GetFullPath(targetPath).Normalize(NormalizationForm.FormC);
        }
        catch (Exception exception) when (exception is ArgumentException or
            NotSupportedException)
        {
            throw new ArgumentException("The restore target path is invalid.", nameof(targetPath),
                exception);
        }

        if (!string.Equals(targetPath, fullPath, StringComparison.Ordinal) ||
            !string.Equals(Path.GetExtension(fullPath), ".dwg", StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException(
                "The restore target must be an exact canonical DWG path.",
                nameof(targetPath));
        }

        RejectNetworkOrDevicePath(fullPath, nameof(targetPath));
        var directory = Path.GetDirectoryName(fullPath)
            ?? throw new ArgumentException(
                "The restore target has no parent directory.",
                nameof(targetPath));
        if (!Directory.Exists(directory) || !File.Exists(fullPath))
        {
            throw new FileNotFoundException("The exact restore target DWG does not exist.");
        }

        RejectExistingReparseComponents(fullPath);
        var attributes = File.GetAttributes(fullPath);
        if ((attributes & (FileAttributes.Directory | FileAttributes.Device |
                FileAttributes.ReparsePoint)) != 0)
        {
            throw new InvalidDataException(
                "The restore target must be a regular local DWG file.");
        }

        return fullPath;
    }

    private static void EnsureWritableFile(string canonicalPath)
    {
        var attributes = File.GetAttributes(canonicalPath);
        if ((attributes & FileAttributes.ReadOnly) != 0)
        {
            throw new InvalidOperationException(
                "The restore target DWG is not writable.");
        }
    }

    private static void EnsureWritableDocument(Document document)
    {
        if (document.IsReadOnly)
        {
            throw new InvalidOperationException(
                "The exact AutoCAD restore target is open read-only.");
        }
    }

    private static void RejectNetworkOrDevicePath(string path, string parameterName)
    {
        if (path.StartsWith("\\\\", StringComparison.Ordinal) ||
            path.StartsWith("//", StringComparison.Ordinal) ||
            path.StartsWith("\\??\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\.\\", StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Network and device restore paths are not allowed.",
                parameterName);
        }

        if (OperatingSystem.IsWindows())
        {
            var root = Path.GetPathRoot(path);
            if (!string.IsNullOrEmpty(root) && new DriveInfo(root).DriveType == DriveType.Network)
            {
                throw new ArgumentException(
                    "Mapped network restore paths are not allowed.",
                    parameterName);
            }
        }
    }

    private static void RejectExistingReparseComponents(string path)
    {
        var current = path;
        while (!string.IsNullOrEmpty(current))
        {
            if ((File.Exists(current) || Directory.Exists(current)) &&
                (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException(
                    "Restore paths must not contain reparse-point components.");
            }

            var parent = Path.GetDirectoryName(current);
            if (string.IsNullOrEmpty(parent) ||
                string.Equals(parent, current, PathComparison))
            {
                break;
            }

            current = parent;
        }
    }

    private sealed class ApplicationContextInvocation<TResult>
    {
        private readonly Func<TResult> _action;
        private readonly CancellationToken _cancellationToken;
        private readonly TaskCompletionSource<TResult> _completion = new(
            TaskCreationOptions.RunContinuationsAsynchronously);

        public ApplicationContextInvocation(
            Func<TResult> action,
            CancellationToken cancellationToken)
        {
            _action = action;
            _cancellationToken = cancellationToken;
        }

        public Task<TResult> Completion => _completion.Task;

        public void Invoke()
        {
            try
            {
                _cancellationToken.ThrowIfCancellationRequested();
                _completion.TrySetResult(_action());
            }
            catch (OperationCanceledException exception) when (
                exception.CancellationToken == _cancellationToken)
            {
                _completion.TrySetCanceled(_cancellationToken);
            }
            catch (Exception exception)
            {
                _completion.TrySetException(exception);
            }
        }
    }
}
