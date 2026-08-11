using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using CadBridge.Execution;
using CadBridge.Hosting;
using CadBridge.Inspection;
using AcApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace CadBridge.Plugin;

/// <summary>
/// Production bridge host bound to AutoCAD's active document through command-context marshalling.
/// The write surface is fail-closed until the deployed AutoCAD build has passed the documented live
/// transaction, metadata, and one-step undo verification gate.
/// </summary>
public sealed class AutoCadBridgeHost : IBridgeHost, IDisposable
{
#if CADBRIDGE_VERIFIED_R26_WRITE
    private const bool VerifiedBuildTuple = true;
#else
    private const bool VerifiedBuildTuple = false;
#endif
    private const string LiveWriteGateEnvironmentVariable = "CAD_HARNESS_LIVE_WRITE_VERIFIED";
    private const string CheckpointRootEnvironmentVariable = "CAD_HARNESS_CHECKPOINT_ROOT";
    private const string CommitJournalRootEnvironmentVariable =
        "CAD_HARNESS_COMMIT_JOURNAL_ROOT";
    private const string ApprovalSecretEnvironmentVariable = "CAD_HARNESS_APPROVAL_SECRET";
    private const int MaximumBlockDepth = 10;

    private readonly DocumentCollection _documents;
    private readonly AutoCadCommandContextMarshaller _commandContext;
    private readonly SemaphoreSlim _commitGate = new(1, 1);
    private readonly DurableCommitCoordinator? _commitCoordinator;
    private readonly string _processEpoch = Guid.NewGuid().ToString("N");
    private readonly UndoRollbackRegistry _undoRollbackRegistry;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<Document, string>
        _rollbackDocuments = new();
    private readonly bool _writeEnabled;
    private readonly OperationFailureDiagnostics _operationDiagnostics = new();
    private bool _disposed;

    public AutoCadBridgeHost(
        DocumentCollection documents,
        bool? writeVerified = null,
        DurableCommitJournal? commitJournal = null)
    {
        ArgumentNullException.ThrowIfNull(documents);
        _documents = documents;
        _commandContext = new AutoCadCommandContextMarshaller(documents);
        _undoRollbackRegistry = new UndoRollbackRegistry(_processEpoch);
        var detectedCadVersion = DetectCadVersion();
        var deploymentRequestedWrite = writeVerified ?? string.Equals(
            Environment.GetEnvironmentVariable(LiveWriteGateEnvironmentVariable),
            "1",
            StringComparison.Ordinal);
        _writeEnabled = WriteRuntimeGate.IsEnabled(
            deploymentRequestedWrite,
            VerifiedBuildTuple,
            Environment.Version.Major,
            detectedCadVersion,
            AutoCadCommandContextUndoGroup.RequiresLiveUndoGroupingVerification);
        _commitCoordinator = _writeEnabled
            ? new DurableCommitCoordinator(
                commitJournal ?? new DurableCommitJournal(GetCommitJournalRoot()))
            : null;

        var capabilities = new List<string>
        {
            "inspect_document",
            "inspect_selection",
        };
        if (_writeEnabled)
        {
            capabilities.AddRange(
            [
                "commit",
                "atomic_transaction",
                "document_lock",
                "undo_group",
                "stable_metadata",
                "rollback_undo_group",
            ]);
        }

        Descriptor = new BridgeHostDescriptor(
            "AutoCAD",
            detectedCadVersion,
            capabilities,
            _writeEnabled ? AutoCadOperationDispatcher.SupportedOperationTypes : []);
    }

    public BridgeHostDescriptor Descriptor { get; }

    public async ValueTask<BridgeHostStatus> GetStatusAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        try
        {
            return await _commandContext.ExecuteAsync(
                token =>
                {
                    token.ThrowIfCancellationRequested();
                    var document = _documents.MdiActiveDocument;
                    if (document is null)
                    {
                        return ValueTask.FromResult(new BridgeHostStatus(
                            Available: false,
                            Message: "No active AutoCAD document is open."));
                    }

                    using var bound = new AutoCadInspectionDocument(document, maxBlockDepth: 0);
                    var message = _writeEnabled
                        ? "Read and atomic write capabilities are enabled for this verified deployment."
                        : "Read-only mode; live atomic-write verification has not been recorded.";
                    var lastFailure = _operationDiagnostics.LastFailure;
                    if (lastFailure is not null)
                    {
                        message = $"{message} Last operation failure: {lastFailure}.";
                    }

                    return ValueTask.FromResult(new BridgeHostStatus(
                        Available: true,
                        ActiveDocumentId: bound.DocumentId,
                        Message: message));
                },
                cancellationToken);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch
        {
            return new BridgeHostStatus(
                Available: false,
                Message: "The active AutoCAD document could not be inspected safely.");
        }
    }

    public ValueTask<BridgeHostResult> InspectDocumentAsync(
        InspectDocumentHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        return InDocumentContextAsync(
            request.DocumentId,
            MaximumBlockDepth,
            (document, service, converter, recordStage, token) =>
            {
                recordStage("inspect_document.snapshot");
                var snapshot = service.InspectDocument(token);
                recordStage("inspect_document.convert");
                return BridgeHostResult.Success(converter.ToDocumentSnapshot(
                    snapshot,
                    request.IncludeLayers,
                    request.IncludeStyles,
                    token));
            },
            cancellationToken);
    }

    public ValueTask<BridgeHostResult> InspectSemanticDrawingAsync(
        SemanticDrawingHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        var includeEntity = EntityScopePredicate(request.Scope);
        return InDocumentContextAsync(
            request.Source.Ref,
            MaximumBlockDepth,
            (_, service, converter, recordStage, token) =>
            {
                recordStage("semantic.snapshot");
                var snapshot = service.InspectDocumentBounded(
                    request.MaxEntities,
                    includeEntity,
                    token);
                recordStage("semantic.convert");
                var data = request.ResponseContract switch
                {
                    SemanticDrawingResponseContract.DrawingSummary =>
                        converter.ToDrawingSummary(snapshot, request, token),
                    SemanticDrawingResponseContract.DrawingModel =>
                        converter.ToDrawingModel(snapshot, request, token),
                    _ => throw new InvalidOperationException(
                        "The semantic response contract is not supported."),
                };
                return BridgeHostResult.Success(data);
            },
            cancellationToken);
    }

    public ValueTask<BridgeHostResult> InspectSelectionAsync(
        InspectSelectionHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        return InDocumentContextAsync(
            request.DocumentId,
            maxBlockDepth: MaximumBlockDepth,
            (_, service, converter, recordStage, token) =>
            {
                recordStage("selection.snapshot");
                var snapshot = service.InspectSelection(request.MaxEntities, token);
                recordStage("selection.convert");
                return BridgeHostResult.Success(converter.ToSelectionSnapshot(
                    snapshot,
                    request.MaxEntities,
                    token));
            },
            cancellationToken);
    }

    private static Func<InspectionEntity, bool> EntityScopePredicate(
        SemanticReadScopeHostRequest? scope)
    {
        if (scope is null)
        {
            return _ => true;
        }

        return scope.Kind switch
        {
            "model_space" => entity => string.Equals(
                entity.Space,
                "model_space",
                StringComparison.OrdinalIgnoreCase),
            "layer" => entity => string.Equals(
                entity.Space,
                "model_space",
                StringComparison.OrdinalIgnoreCase) && string.Equals(
                entity.Layer,
                scope.LayerName,
                StringComparison.OrdinalIgnoreCase),
            "layout" => entity => string.Equals(
                entity.Space,
                $"layout:{scope.LayoutName}",
                StringComparison.OrdinalIgnoreCase),
            "selection" => SelectionPredicate(scope.EntityRefs),
            _ => throw new ArgumentException("Unsupported semantic read scope.", nameof(scope)),
        };
    }

    private static Func<InspectionEntity, bool> SelectionPredicate(
        IReadOnlyList<string> entityReferences)
    {
        var references = entityReferences.ToHashSet(StringComparer.Ordinal);
        return entity => references.Contains(
            AutoCadContractConverter.ToWireEntityReference(entity.EntityRef));
    }

    public ValueTask<BridgeHostResult> PreviewAsync(
        PreviewHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Rejected));
    }

    public ValueTask<BridgeHostResult> ValidateRevisionAsync(
        RevisionValidationHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        return InDocumentContextAsync(
            request.DocumentId,
            maxBlockDepth: MaximumBlockDepth,
            (_, service, _, _, token) =>
            {
                var revision = service.InspectDocument(token).Revision;
                return BridgeHostResult.Success(new JsonObject
                {
                    ["valid"] = string.Equals(
                        revision,
                        request.ExpectedRevision,
                        StringComparison.Ordinal),
                });
            },
            cancellationToken);
    }

    public async ValueTask<BridgeHostResult> CommitAsync(
        CommitHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        ThrowIfDisposed();
        if (!_writeEnabled)
        {
            return new BridgeHostResult(BridgeHostOutcome.Rejected);
        }

        var digest = CommitRequestDigest.Compute(request);
        var undoGroup = request.CreateCheckpoint
            ? $"undo-{_processEpoch}-{Guid.NewGuid():N}"
            : null;
        Document? committedDocument = null;
        await _commitGate.WaitAsync(cancellationToken);
        try
        {
            var result = await (_commitCoordinator ?? throw new InvalidOperationException(
                    "A write-enabled host requires a durable commit coordinator."))
                .ExecuteAsync(
                    request.JobId,
                    request.IdempotencyKey,
                    digest,
                    () => IsCommitAuthorized(request),
                    token => CommitOnceAsync(
                        request,
                        undoGroup,
                        document => committedDocument = document,
                        token),
                    cancellationToken);
            if (result.Outcome == BridgeHostOutcome.Ok &&
                committedDocument is not null &&
                undoGroup is not null)
            {
                try
                {
                    if (TryRegisterUndoRollback(
                            request,
                            result.Data,
                            committedDocument,
                            undoGroup) &&
                        !TrackRollbackDocument(committedDocument, request))
                    {
                        InvalidateRollbackForRequest(request);
                    }
                }
                catch
                {
                    // The commit journal already contains a proven success.  Failure to create
                    // an optional rollback receipt must never replace that primary outcome.
                    InvalidateRollbackForRequest(request);
                }
            }

            return result;
        }
        finally
        {
            _commitGate.Release();
        }
    }

    public async ValueTask<BridgeHostResult> RollbackAsync(
        RollbackHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        ThrowIfDisposed();
        if (!_writeEnabled || request.UndoGroup is null ||
            !BridgeAuthorization.TryValidateRollbackAuthorization(
                request.RollbackApprovalToken,
                Environment.GetEnvironmentVariable(ApprovalSecretEnvironmentVariable) ?? string.Empty,
                request.JobId,
                request.DocumentId,
                request.CheckpointId,
                request.CurrentRevision,
                DateTimeOffset.UtcNow,
                out var claims) ||
            claims is null)
        {
            return new BridgeHostResult(BridgeHostOutcome.Rejected);
        }

        var rollbackRequest = new UndoRollbackRequest(
            request.UndoGroup,
            request.UndoGroup,
            request.JobId,
            request.DocumentId,
            request.CheckpointId,
            request.CurrentRevision,
            _processEpoch,
            claims.ApprovalId,
            ComputeRollbackDigest(request));

        await _commitGate.WaitAsync(cancellationToken);
        try
        {
            var begin = _undoRollbackRegistry.Begin(rollbackRequest);
            if (begin.Kind == UndoRollbackBeginKind.Replay && begin.Result is not null)
            {
                return BridgeHostResult.Success(
                    JsonNode.Parse(begin.Result.Data.GetRawText())?.AsObject()
                    ?? throw new InvalidDataException("Stored rollback result is invalid."));
            }

            if (begin.Kind == UndoRollbackBeginKind.Conflict)
            {
                return new BridgeHostResult(BridgeHostOutcome.IdempotencyKeyReused);
            }

            if (begin.Kind != UndoRollbackBeginKind.Execute || begin.Receipt is null)
            {
                return new BridgeHostResult(BridgeHostOutcome.Rejected);
            }

            var diagnostic = _operationDiagnostics.Begin("rollback.command_context");
            try
            {
                var data = await ExecuteUndoRollbackAsync(
                    request,
                    begin.Receipt,
                    diagnostic.RecordStage,
                    cancellationToken);
                var element = JsonSerializer.SerializeToElement(data);
                _undoRollbackRegistry.CompleteSuccess(rollbackRequest, element);
                return BridgeHostResult.Success(data);
            }
            catch (StaleRevisionException)
            {
                _undoRollbackRegistry.QuarantineAfterCommandUncertainty(rollbackRequest);
                diagnostic.PublishFailure("rollback.precheck:StaleRevisionException");
                return new BridgeHostResult(BridgeHostOutcome.StaleDocumentRevision);
            }
            catch (OperationCanceledException)
            {
                _undoRollbackRegistry.QuarantineAfterCommandUncertainty(rollbackRequest);
                throw;
            }
            catch (Exception error)
            {
                _undoRollbackRegistry.QuarantineAfterCommandUncertainty(rollbackRequest);
                diagnostic.PublishFailure(FailureLabel(diagnostic.Stage, error));
                return new BridgeHostResult(BridgeHostOutcome.Failed);
            }
        }
        finally
        {
            _commitGate.Release();
        }
    }

    public ValueTask<BridgeHostResult> ExportAsync(
        ExportHostRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        cancellationToken.ThrowIfCancellationRequested();
        // The host deliberately advertises no export capability. Python owns the path allowlist.
        return ValueTask.FromResult(new BridgeHostResult(BridgeHostOutcome.Rejected));
    }

    private bool TryRegisterUndoRollback(
        CommitHostRequest request,
        JsonObject? data,
        Document document,
        string undoGroup)
    {
        if (data is null ||
            !TryReadCommitIdentity(request, out var documentId, out _, out _) ||
            data["checkpoint_id"]?.GetValue<string>() is not { Length: > 0 } checkpointId ||
            data["previous_revision"]?.GetValue<string>() is not { Length: > 0 } previousRevision ||
            data["new_revision"]?.GetValue<string>() is not { Length: > 0 } newRevision ||
            !string.Equals(data["undo_group"]?.GetValue<string>(), undoGroup, StringComparison.Ordinal))
        {
            return false;
        }

        _undoRollbackRegistry.Register(new UndoRollbackReceipt(
            undoGroup,
            undoGroup,
            request.JobId,
            documentId,
            checkpointId,
            previousRevision,
            newRevision,
            _processEpoch));
        return true;
    }

    private bool TrackRollbackDocument(Document document, CommitHostRequest request)
    {
        if (!TryReadCommitIdentity(request, out var documentId, out _, out _))
        {
            return false;
        }

        try
        {
            if (_rollbackDocuments.TryAdd(document, documentId))
            {
                document.CommandWillStart += OnTrackedDocumentCommandWillStart;
            }
            else
            {
                _rollbackDocuments[document] = documentId;
            }

            return true;
        }
        catch
        {
            _rollbackDocuments.TryRemove(document, out _);
            return false;
        }
    }

    private void InvalidateRollbackForRequest(CommitHostRequest request)
    {
        if (TryReadCommitIdentity(request, out var documentId, out _, out _))
        {
            _undoRollbackRegistry.InvalidateAvailableForDocument(documentId);
        }
    }

    private void OnTrackedDocumentCommandWillStart(object sender, CommandEventArgs eventArgs)
    {
        _ = eventArgs;
        if (sender is Document document &&
            _rollbackDocuments.TryGetValue(document, out var documentId))
        {
            _undoRollbackRegistry.InvalidateAvailableForDocument(documentId);
        }
    }

    private async ValueTask<JsonObject> ExecuteUndoRollbackAsync(
        RollbackHostRequest request,
        UndoRollbackReceipt receipt,
        Action<string> recordStage,
        CancellationToken cancellationToken)
    {
        return await _commandContext.ExecuteAsync(
            token =>
            {
                token.ThrowIfCancellationRequested();
                recordStage("rollback.document.bind");
                var document = _documents.MdiActiveDocument
                    ?? throw new InvalidOperationException("No active AutoCAD document is open.");
                recordStage("rollback.document.lock");
                using var documentLock = document.LockDocument();
                recordStage("rollback.precheck.inspect");
                using (var bound = new AutoCadInspectionDocument(document, MaximumBlockDepth))
                {
                    var snapshot = new BridgeInspectionService(bound).InspectDocument(token);
                    if (!string.Equals(snapshot.DocumentId, request.DocumentId, StringComparison.Ordinal) ||
                        !string.Equals(snapshot.Revision, receipt.NewRevision, StringComparison.Ordinal))
                    {
                        throw new StaleRevisionException();
                    }
                }

                token.ThrowIfCancellationRequested();
                for (var attempt = 1; attempt <= 2; attempt++)
                {
                    recordStage($"rollback.undo.command.{attempt}");
                    document.Editor.Command("_.UNDO", "1");

                    recordStage($"rollback.postcheck.inspect.{attempt}");
                    using var restoredBound = new AutoCadInspectionDocument(
                        document,
                        MaximumBlockDepth);
                    var restored = new BridgeInspectionService(restoredBound)
                        .InspectDocument(CancellationToken.None);
                    if (!string.Equals(
                            restored.DocumentId,
                            request.DocumentId,
                            StringComparison.Ordinal))
                    {
                        recordStage("rollback.postcheck.document");
                        throw new InvalidOperationException(
                            "The rollback command switched to an unexpected document.");
                    }

                    var revisionDecision = UndoRollbackRevisionFence.Decide(
                        restored.Revision,
                        receipt.NewRevision,
                        receipt.PreviousRevision,
                        attempt);
                    if (revisionDecision == UndoRollbackRevisionFenceDecision.Restored)
                    {
                        return ValueTask.FromResult(new JsonObject
                        {
                            ["schema_version"] = "1.12",
                            ["job_id"] = request.JobId,
                            ["restored_revision"] = restored.Revision,
                            ["checkpoint_id"] = request.CheckpointId,
                            ["method"] = "undo_group",
                        });
                    }

                    if (revisionDecision ==
                        UndoRollbackRevisionFenceDecision.RetryOnlyWhenUnchangedOnAttempt1)
                    {
                        continue;
                    }

                    if (!string.Equals(
                            restored.Revision,
                            receipt.NewRevision,
                            StringComparison.Ordinal))
                    {
                        recordStage("rollback.postcheck.unexpected_revision");
                        throw new InvalidOperationException(
                            "The rollback command reached an unexpected document revision.");
                    }

                    recordStage("rollback.postcheck.revision");
                    throw new InvalidOperationException(
                        "The bounded undo sequence did not restore the exact pre-commit revision.");
                }

                throw new InvalidOperationException("The rollback revision fence was exhausted.");
            },
            cancellationToken);
    }

    private static string ComputeRollbackDigest(RollbackHostRequest request)
    {
        var canonical = string.Join(
            "\u001f",
            "cad-bridge-rollback-undo-v1",
            request.JobId,
            request.DocumentId,
            request.CheckpointId,
            request.CurrentRevision,
            request.UndoGroup);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        foreach (var document in _rollbackDocuments.Keys)
        {
            try
            {
                document.CommandWillStart -= OnTrackedDocumentCommandWillStart;
            }
            catch
            {
                // The document may already be closed during plugin unload.
            }
        }

        _rollbackDocuments.Clear();
        _commitGate.Dispose();
    }

    private async ValueTask<BridgeHostResult> CommitOnceAsync(
        CommitHostRequest request,
        string? undoGroup,
        Action<Document> onCommitted,
        CancellationToken cancellationToken)
    {
        var diagnostic = _operationDiagnostics.Begin("commit.authorize");
        if (!TryReadCommitIdentity(
                request,
                out var documentId,
                out var planHash,
                out var planExpectedRevision) ||
            !string.Equals(planExpectedRevision, request.ExpectedRevision, StringComparison.Ordinal) ||
            !BridgeAuthorization.TryValidateCommitAuthorization(
                request.Plan,
                request.ApprovalToken,
                Environment.GetEnvironmentVariable(ApprovalSecretEnvironmentVariable) ?? string.Empty,
                request.JobId,
                request.ExpectedRevision,
                DateTimeOffset.UtcNow,
                out _))
        {
            return new BridgeHostResult(BridgeHostOutcome.Rejected);
        }

        Document document;
        try
        {
            diagnostic.RecordStage("commit.document_bind");
            document = await _commandContext.ExecuteAsync(
                token =>
                {
                    token.ThrowIfCancellationRequested();
                    var active = _documents.MdiActiveDocument
                        ?? throw new InvalidOperationException("No active AutoCAD document is open.");
                    using var bound = new AutoCadInspectionDocument(active, maxBlockDepth: 0);
                    if (!string.Equals(bound.DocumentId, documentId, StringComparison.Ordinal))
                    {
                        throw new DocumentMismatchException();
                    }

                    return ValueTask.FromResult(active);
                },
                cancellationToken);
        }
        catch (DocumentMismatchException)
        {
            return new BridgeHostResult(BridgeHostOutcome.StaleDocumentRevision);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch
        {
            return new BridgeHostResult(BridgeHostOutcome.Failed);
        }

        IReadOnlyList<AutoCadPlanOperation> operations;
        try
        {
            diagnostic.RecordStage("commit.plan_parse");
            operations = AutoCadOperationDispatcher.ParseOperations(request.Plan);
        }
        catch
        {
            return new BridgeHostResult(BridgeHostOutcome.Rejected);
        }

        var dispatcher = new AutoCadOperationDispatcher();
        var executor = new AtomicJobExecutor(
            _commandContext,
            new AutoCadAtomicDocumentHost(document));
        string? previousRevision = null;
        CheckpointArtifact? checkpointArtifact = null;
        DocumentInspectionSnapshot? committedSnapshot = null;
        CreatedEntityMeasurementSnapshot? measurementSnapshot = null;
        var stale = false;

        diagnostic.RecordStage("commit.atomic_execute");
        var execution = await executor.ExecuteAsync(
            operations,
            dispatcher.DispatchAsync,
            token =>
            {
                token.ThrowIfCancellationRequested();
                if (document.Database.Insunits != UnitsValue.Millimeters)
                {
                    throw new InvalidOperationException(
                        "Atomic writes require a millimetre AutoCAD document.");
                }

                using var bound = new AutoCadInspectionDocument(document, MaximumBlockDepth);
                var service = new BridgeInspectionService(bound);
                var snapshot = service.InspectDocument(token);
                previousRevision = snapshot.Revision;
                if (!string.Equals(snapshot.DocumentId, documentId, StringComparison.Ordinal) ||
                    !string.Equals(snapshot.Revision, request.ExpectedRevision, StringComparison.Ordinal))
                {
                    stale = true;
                    throw new StaleRevisionException();
                }

                if (request.CreateCheckpoint)
                {
                    checkpointArtifact = CreateCheckpoint(document, request.JobId, token);
                }

                return ValueTask.CompletedTask;
            },
            dispatcher.ValidateBeforeCommitAsync,
            _ =>
            {
                using var bound = new AutoCadInspectionDocument(document, MaximumBlockDepth);
                var service = new BridgeInspectionService(bound);
                committedSnapshot = service.InspectDocument(CancellationToken.None);
                var references = dispatcher.Entities
                    .Where(entity => !entity.Deleted)
                    .Select(entity => new StableEntityReference(entity.ObjectId.Handle.ToString()))
                    .Distinct()
                    .ToArray();
                measurementSnapshot = service.MeasureCreatedEntities(
                    references,
                    CancellationToken.None);
                return ValueTask.CompletedTask;
            },
            cancellationToken);

        if (stale)
        {
            return new BridgeHostResult(BridgeHostOutcome.StaleDocumentRevision);
        }

        if (execution.Outcome == AtomicExecutionOutcome.UnknownCommitState)
        {
            return new BridgeHostResult(BridgeHostOutcome.UnknownCommitState);
        }

        if (!execution.IsCommitted || previousRevision is null || committedSnapshot is null ||
            measurementSnapshot is null)
        {
            diagnostic.PublishFailure(
                $"commit.atomic.{execution.Trace.Stage}:{execution.FailureKind}");
            DeleteCheckpointAfterProvenPreCommitFailure(checkpointArtifact);
            return new BridgeHostResult(BridgeHostOutcome.Failed);
        }

        var result = BridgeHostResult.Success(BuildCommitResult(
            request,
            planHash,
            previousRevision,
            checkpointArtifact?.Id,
            undoGroup,
            dispatcher,
            committedSnapshot,
            measurementSnapshot));
        onCommitted(document);
        return result;
    }

    private async ValueTask<BridgeHostResult> InDocumentContextAsync(
        string? expectedDocumentId,
        int maxBlockDepth,
        Func<Document, BridgeInspectionService, AutoCadContractConverter, Action<string>, CancellationToken, BridgeHostResult> action,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(action);
        ThrowIfDisposed();
        var diagnostic = _operationDiagnostics.Begin("command_context");
        try
        {
            return await _commandContext.ExecuteAsync(
                token =>
                {
                    token.ThrowIfCancellationRequested();
                    var document = _documents.MdiActiveDocument
                        ?? throw new InvalidOperationException("No active AutoCAD document is open.");
                    diagnostic.RecordStage("document.bind");
                    using var bound = new AutoCadInspectionDocument(document, maxBlockDepth);
                    if (expectedDocumentId is not null && !string.Equals(
                            expectedDocumentId,
                            bound.DocumentId,
                            StringComparison.Ordinal))
                    {
                        return ValueTask.FromResult(
                            new BridgeHostResult(BridgeHostOutcome.StaleDocumentRevision));
                    }

                    var service = new BridgeInspectionService(bound);
                    var converter = new AutoCadContractConverter(CreateContractContext(document));
                    diagnostic.RecordStage("operation.execute");
                    var result = action(
                        document,
                        service,
                        converter,
                        diagnostic.RecordStage,
                        token);
                    return ValueTask.FromResult(result);
                },
                cancellationToken);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception error)
        {
            // Expose only the bounded exception type through status. Paths, drawing content,
            // exception messages and stack traces never cross the local bridge boundary.
            diagnostic.PublishFailure(FailureLabel(diagnostic.Stage, error));
            return new BridgeHostResult(BridgeHostOutcome.Failed);
        }
    }

    private static string FailureLabel(string stage, Exception error)
    {
        var label = $"{stage}:{error.GetType().Name}";
        if (error is Autodesk.AutoCAD.Runtime.Exception autoCadError)
        {
            label = $"{label}[{autoCadError.ErrorStatus}]";
        }

        if (error is ArgumentException { ParamName: { Length: > 0 and <= 64 } parameter } &&
            parameter.All(character => char.IsAsciiLetterOrDigit(character) || character is '_' or '.'))
        {
            label = $"{label}({parameter})";
        }

        return label;
    }

    private static AutoCadContractContext CreateContractContext(Document document)
    {
        var database = document.Database;
        var (unitCode, factor) = UnitContext(database.Insunits);
        var rawName = string.IsNullOrWhiteSpace(database.Filename)
            ? document.Name
            : database.Filename;
        var displayName = Path.GetFileName(rawName);
        if (string.IsNullOrWhiteSpace(displayName))
        {
            displayName = "Untitled.dwg";
        }

        var normalizedPath = string.IsNullOrWhiteSpace(rawName)
            ? "untitled"
            : rawName.Replace('\\', '/').Trim().ToUpperInvariant();
        var activeSpace = database.TileMode ? "model" : "paper";
        var activeLayout = database.TileMode ? null : LayoutManager.Current.CurrentLayout;
        return new AutoCadContractContext(
            displayName,
            AutoCadContractConverter.ComputePathHash(normalizedPath),
            unitCode,
            factor,
            activeSpace,
            activeLayout);
    }

    private static (string UnitCode, double? ToMillimetresFactor) UnitContext(UnitsValue value) =>
        value switch
        {
            UnitsValue.Millimeters => ("mm", 1.0),
            UnitsValue.Centimeters => ("cm", 10.0),
            UnitsValue.Meters => ("m", 1000.0),
            UnitsValue.Inches => ("in", 25.4),
            _ => (value.ToString(), null),
        };

    private static JsonObject BuildCommitResult(
        CommitHostRequest request,
        string planHash,
        string previousRevision,
        string? checkpointId,
        string? undoGroup,
        AutoCadOperationDispatcher dispatcher,
        DocumentInspectionSnapshot committedSnapshot,
        CreatedEntityMeasurementSnapshot measurementSnapshot)
    {
        var entitiesByHandle = committedSnapshot.Entities.ToDictionary(
            entity => entity.EntityRef.Handle,
            StringComparer.Ordinal);
        var measurementsByHandle = measurementSnapshot.Measurements.ToDictionary(
            measurement => measurement.EntityRef.Handle,
            StringComparer.Ordinal);
        var operationCounts = dispatcher.Entities
            .Where(entity => !entity.Deleted)
            .GroupBy(entity => entity.OperationId, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
        var results = new JsonArray();
        foreach (var operationEntity in dispatcher.Entities)
        {
            var handle = operationEntity.ObjectId.Handle.ToString().ToUpperInvariant();
            JsonObject measurements;
            string entityType;
            if (operationEntity.Deleted)
            {
                entityType = "ERASED";
                measurements = new JsonObject { ["deleted"] = true };
            }
            else
            {
                var entity = entitiesByHandle[handle];
                entityType = entity.EntityType;
                measurements = ToMeasurements(
                    entity,
                    measurementsByHandle[handle],
                    operationCounts[operationEntity.OperationId]);
            }

            results.Add(new JsonObject
            {
                ["operation_id"] = operationEntity.OperationId,
                ["feature_id"] = operationEntity.FeatureId,
                ["entity_ref"] = $"acad:handle:{handle}",
                ["entity_type"] = entityType,
                ["measurements"] = measurements,
            });
        }

        return new JsonObject
        {
            ["schema_version"] = "1.12",
            ["job_id"] = request.JobId,
            ["plan_hash"] = planHash,
            ["status"] = "committed",
            ["entity_results"] = results,
            ["previous_revision"] = previousRevision,
            ["new_revision"] = committedSnapshot.Revision,
            ["checkpoint_id"] = checkpointId,
            ["undo_group"] = undoGroup,
        };
    }

    private static JsonObject ToMeasurements(
        InspectionEntity entity,
        ActualEntityMeasurement measurement,
        int operationEntityCount)
    {
        var result = new JsonObject
        {
            ["count"] = operationEntityCount,
            ["visible"] = entity.Visible,
            ["layer"] = entity.Layer,
        };
        if (measurement.Length.HasValue)
        {
            result["length_mm"] = measurement.Length.Value;
        }

        if (measurement.Area.HasValue)
        {
            result["area_mm2"] = measurement.Area.Value;
        }

        if (measurement.Radius.HasValue)
        {
            result["radius_mm"] = measurement.Radius.Value;
            result["diameter_mm"] = measurement.Radius.Value * 2.0;
        }

        if (measurement.Bounds is { } bounds)
        {
            result["width_mm"] = bounds.Maximum.X - bounds.Minimum.X;
            result["height_mm"] = bounds.Maximum.Y - bounds.Minimum.Y;
        }

        var geometry = entity.Geometry;
        if (geometry.Points.Count > 0)
        {
            result["center_mm"] = PointArray(geometry.Points[0]);
        }

        if (geometry.Kind == InspectionGeometryKind.Polyline)
        {
            result["closed"] = geometry.Closed;
            result["vertex_count"] = geometry.Points.Count;
        }

        if (geometry.Kind == InspectionGeometryKind.Arc &&
            geometry.StartAngleRadians.HasValue && geometry.EndAngleRadians.HasValue)
        {
            var sweep = (geometry.EndAngleRadians.Value - geometry.StartAngleRadians.Value) %
                (Math.PI * 2.0);
            if (sweep < 0.0)
            {
                sweep += Math.PI * 2.0;
            }

            result["sweep_deg"] = sweep * 180.0 / Math.PI;
        }

        if (geometry.Kind == InspectionGeometryKind.Dimension && geometry.Measurement.HasValue)
        {
            result["measurement_mm"] = geometry.Measurement.Value;
            result["style_name"] = geometry.DimensionStyle;
        }

        return result;
    }

    private static JsonArray PointArray(InspectionPoint point) =>
        [JsonValue.Create(point.X), JsonValue.Create(point.Y)];

    private static bool TryReadCommitIdentity(
        CommitHostRequest request,
        out string documentId,
        out string planHash,
        out string expectedRevision)
    {
        documentId = string.Empty;
        planHash = string.Empty;
        expectedRevision = string.Empty;
        try
        {
            var jobId = request.Plan.GetProperty("job_id").GetString();
            documentId = request.Plan.GetProperty("document_id").GetString() ?? string.Empty;
            planHash = request.Plan.GetProperty("plan_hash").GetString() ?? string.Empty;
            expectedRevision = request.Plan.GetProperty("expected_revision").GetString() ?? string.Empty;
            return string.Equals(jobId, request.JobId, StringComparison.Ordinal) &&
                documentId.Length > 0 && planHash.Length > 0 && expectedRevision.Length > 0;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
        catch (KeyNotFoundException)
        {
            return false;
        }
    }

    private static bool IsCommitAuthorized(CommitHostRequest request) =>
        TryReadCommitIdentity(
            request,
            out _,
            out _,
            out var planExpectedRevision) &&
        string.Equals(planExpectedRevision, request.ExpectedRevision, StringComparison.Ordinal) &&
        BridgeAuthorization.TryValidateCommitAuthorization(
            request.Plan,
            request.ApprovalToken,
            Environment.GetEnvironmentVariable(ApprovalSecretEnvironmentVariable) ?? string.Empty,
            request.JobId,
            request.ExpectedRevision,
            DateTimeOffset.UtcNow,
            out _);

    private static string GetCommitJournalRoot()
    {
        var configured = Environment.GetEnvironmentVariable(
            CommitJournalRootEnvironmentVariable);
        return string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AutoCADMechanicalHarness",
                "bridge-commit-journal-v1")
            : configured;
    }

    private static CheckpointArtifact CreateCheckpoint(
        Document document,
        string jobId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var configuredRoot = Environment.GetEnvironmentVariable(CheckpointRootEnvironmentVariable);
        var root = string.IsNullOrWhiteSpace(configuredRoot)
            ? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AutoCADMechanicalHarness",
                "checkpoints")
            : configuredRoot;
        if (!Path.IsPathFullyQualified(root))
        {
            throw new InvalidOperationException("The checkpoint root must be an absolute local path.");
        }

        root = Path.GetFullPath(root);
        Directory.CreateDirectory(root);
        var checkpointId = $"checkpoint-{Guid.NewGuid():N}";
        var jobDigest = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(jobId)))[..16].ToLowerInvariant();
        var target = Path.GetFullPath(Path.Combine(root, $"{checkpointId}-{jobDigest}.dwg"));
        if (!string.Equals(Path.GetDirectoryName(target), root, StringComparison.OrdinalIgnoreCase) ||
            File.Exists(target))
        {
            throw new InvalidOperationException("The checkpoint target is not a new allowlisted file.");
        }

        try
        {
            using var checkpoint = document.Database.Wblock();
            checkpoint.SaveAs(target, DwgVersion.Current);
            cancellationToken.ThrowIfCancellationRequested();
            return new CheckpointArtifact(checkpointId, target);
        }
        catch
        {
            if (File.Exists(target))
            {
                File.Delete(target);
            }

            throw;
        }
    }

    private static void DeleteCheckpointAfterProvenPreCommitFailure(
        CheckpointArtifact? checkpoint)
    {
        if (checkpoint is not null && File.Exists(checkpoint.Path))
        {
            File.Delete(checkpoint.Path);
        }
    }

    private static string DetectCadVersion()
    {
        var raw = Convert.ToString(AcApplication.Version, CultureInfo.InvariantCulture) ?? string.Empty;
        // The boundary lookarounds deliberately use the regular backtracking engine.
        // RegexOptions.NonBacktracking rejects lookarounds with NotSupportedException,
        // which previously faulted the production bridge during AutoCAD start-up before
        // the secured pipe could be created.  ``raw`` is a short, trusted AutoCAD value,
        // and this bounded numeric pattern has no ambiguous/repeating branches.
        var match = Regex.Match(
            raw,
            @"(?<![0-9])(?<version>[0-9]+\.[0-9]+)(?![0-9])",
            RegexOptions.CultureInvariant);
        return match.Success ? match.Groups["version"].Value : "unknown";
    }

    private void ThrowIfDisposed() =>
        ObjectDisposedException.ThrowIf(_disposed, this);

    private sealed class StaleRevisionException : Exception;

    private sealed class DocumentMismatchException : Exception;

    private sealed record CheckpointArtifact(string Id, string Path);
}

internal sealed class AutoCadBridgeHostFactory : IAutoCadBridgeHostFactory
{
    public IBridgeHost CreateHost() => new AutoCadBridgeHost(AcApplication.DocumentManager);
}
