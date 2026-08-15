using System.Text.Json;
using System.Text.Json.Nodes;
using CadBridge.Contracts;
using CadBridge.Ipc;

namespace CadBridge.Hosting;

public sealed record BridgeHostDescriptor(
    string CadApplication,
    string CadVersion,
    IReadOnlyList<string> Capabilities,
    IReadOnlyList<string> SupportedOperationTypes);

public sealed record BridgeHostStatus(
    bool Available,
    string? ActiveDocumentId = null,
    string? Message = null);

public sealed record InspectDocumentHostRequest(
    string? DocumentId,
    bool IncludeLayers,
    bool IncludeStyles);

public sealed record SemanticDrawingSourceHostRequest(
    string Kind,
    string Format,
    string Ref);

public sealed record SemanticReadScopeHostRequest(
    string Kind,
    string? LayerName,
    string? LayoutName,
    IReadOnlyList<string> EntityRefs);

public enum SemanticDrawingResponseContract
{
    DrawingSummary,
    DrawingModel,
}

public sealed record SemanticDrawingHostRequest(
    SemanticDrawingSourceHostRequest Source,
    SemanticReadScopeHostRequest? Scope,
    int MaxEntities,
    int MaxBlockNestingDepth,
    bool IncludeGeometry,
    SemanticDrawingResponseContract ResponseContract);

public sealed record InspectSelectionHostRequest(string DocumentId, int MaxEntities);

public sealed record RevisionValidationHostRequest(string DocumentId, string ExpectedRevision);

public sealed record PreviewHostRequest(JsonElement Plan, string JobId);

public sealed record CommitHostRequest(
    JsonElement Plan,
    string JobId,
    string IdempotencyKey,
    string ExpectedRevision,
    string ApprovalToken,
    bool CreateCheckpoint);

public sealed record RollbackHostRequest(
    string JobId,
    string DocumentId,
    string CheckpointId,
    string CurrentRevision,
    string RollbackApprovalToken,
    string? UndoGroup);

public sealed record ExportHostRequest(
    string DocumentId,
    string Format,
    string TargetPath,
    bool Overwrite);

public enum BridgeHostOutcome
{
    Ok,
    Rejected,
    Conflict,
    IdempotencyKeyReused,
    Failed,
    StaleDocumentRevision,
    UnknownCommitState,
    RollbackRecoveryRequired,
}

/// <summary>
/// A closed host outcome. The host may return contract data, but cannot choose arbitrary wire error
/// codes, statuses, messages, commands, or CLR types.
/// </summary>
public sealed record BridgeHostResult(BridgeHostOutcome Outcome, JsonObject? Data = null)
{
    public static BridgeHostResult Success(JsonObject data) =>
        new(BridgeHostOutcome.Ok, data ?? throw new ArgumentNullException(nameof(data)));
}

/// <summary>
/// Typed boundary implemented by the Autodesk-dependent host assembly. Every operation is explicit;
/// there is no string-command, reflection, dynamic, or arbitrary method dispatch surface.
/// </summary>
public interface IBridgeHost
{
    BridgeHostDescriptor Descriptor { get; }

    ValueTask<BridgeHostStatus> GetStatusAsync(CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> InspectDocumentAsync(
        InspectDocumentHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> InspectSemanticDrawingAsync(
        SemanticDrawingHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> InspectSelectionAsync(
        InspectSelectionHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> PreviewAsync(
        PreviewHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> ValidateRevisionAsync(
        RevisionValidationHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> CommitAsync(
        CommitHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> RollbackAsync(
        RollbackHostRequest request,
        CancellationToken cancellationToken);

    ValueTask<BridgeHostResult> ExportAsync(
        ExportHostRequest request,
        CancellationToken cancellationToken);
}

/// <summary>
/// Strict router suitable for direct use as a <see cref="PipeRequestHandler"/>. Cancellation requests
/// are deliberately excluded because <see cref="PipeRequestProcessor"/> owns their terminal protocol.
/// </summary>
public sealed class BridgeRequestRouter
{
    private const int IdentifierLimit = 256;

    private static readonly HashSet<string> EnvelopeProperties =
    [
        "schema_version",
        "method",
        "request_id",
        "params",
        "idempotency_key",
        "job_id",
    ];

    private static readonly HashSet<string> KnownCapabilities =
    [
        "inspect_document",
        "inspect_selection",
        "preview",
        "commit",
        "export",
        "atomic_transaction",
        "document_lock",
        "undo_group",
        "stable_metadata",
        "rollback_undo_group",
        "checkpoint_restore",
        "in_viewport_preview",
    ];

    private static readonly HashSet<string> KnownOperationTypes =
    [
        "create_line",
        "create_polyline",
        "create_closed_polyline",
        "create_circle",
        "create_circles",
        "create_arc",
        "create_text",
        "create_centerline",
        "create_centermark",
        "create_linear_dimension",
        "create_aligned_dimension",
        "create_diameter_dimension",
        "create_radius_dimension",
        "create_angular_dimension",
        "create_hatch",
        "update_entity",
        "delete_entity",
    ];

    private static readonly HashSet<string> PlanProperties =
    [
        "schema_version",
        "plan_id",
        "job_id",
        "document_id",
        "expected_revision",
        "canonical_units",
        "profile_ref",
        "operations",
        "validation_expectations",
        "plan_hash",
    ];

    private static readonly HashSet<string> OperationProperties =
    [
        "operation_id",
        "feature_id",
        "type",
        "layer",
        "geometry",
        "expected",
        "target_entity_ref",
    ];

    private static readonly HashSet<string> ValidationExpectationProperties =
    [
        "rule_id",
        "feature_id",
        "operation_id",
        "expected",
    ];

    private readonly IBridgeHost _host;
    private readonly string[] _capabilities;
    private readonly string[] _supportedOperationTypes;
    private readonly HashSet<string> _capabilitySet;
    private readonly HashSet<string> _operationTypeSet;
    private readonly string _cadApplication;
    private readonly string _cadVersion;

    public BridgeRequestRouter(IBridgeHost host)
    {
        ArgumentNullException.ThrowIfNull(host);
        var descriptor = host.Descriptor;
        ArgumentNullException.ThrowIfNull(descriptor);
        ValidateDescriptor(descriptor);
        _host = host;
        _capabilities = descriptor.Capabilities.Order(StringComparer.Ordinal).ToArray();
        _supportedOperationTypes = descriptor.SupportedOperationTypes
            .Order(StringComparer.Ordinal)
            .ToArray();
        _capabilitySet = new HashSet<string>(_capabilities, StringComparer.Ordinal);
        _operationTypeSet = new HashSet<string>(_supportedOperationTypes, StringComparer.Ordinal);
        _cadApplication = descriptor.CadApplication;
        _cadVersion = descriptor.CadVersion;
    }

    public PipeRequestHandler Handler => HandleAsync;

    public async ValueTask<PipeHandlerResult> HandleAsync(
        JsonElement request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!TryReadEnvelope(request, out var method, out var parameters))
        {
            return InvalidParameters();
        }

        PipeHandlerResult result = method switch
        {
            "handshake" => HandleHandshake(parameters),
            "status" => await HandleStatusAsync(parameters, cancellationToken).ConfigureAwait(false),
            "inspect_document" => await HandleInspectDocumentAsync(parameters, cancellationToken).ConfigureAwait(false),
            "inspect_selection" => await HandleInspectSelectionAsync(parameters, cancellationToken).ConfigureAwait(false),
            "preview" => await HandlePreviewAsync(parameters, cancellationToken).ConfigureAwait(false),
            "validate_revision" => await HandleValidateRevisionAsync(parameters, cancellationToken).ConfigureAwait(false),
            "commit" => await HandleCommitAsync(request, parameters, cancellationToken).ConfigureAwait(false),
            "rollback" => await HandleRollbackAsync(request, parameters, cancellationToken).ConfigureAwait(false),
            "export" => await HandleExportAsync(parameters, cancellationToken).ConfigureAwait(false),
            // A direct call is rejected. The processor intercepts a real cancel request before here.
            "cancel" => Unsupported("Cancellation is owned by the IPC processor."),
            _ => Unsupported("The bridge method is not supported."),
        };
        cancellationToken.ThrowIfCancellationRequested();
        return result;
    }

    private PipeHandlerResult HandleHandshake(JsonElement parameters)
    {
        if (!HasExactProperties(parameters, ["schema_version"]) ||
            !TryGetString(parameters, "schema_version", 3, 32, out var schemaVersion) ||
            !string.Equals(schemaVersion, IpcContract.CurrentSchemaVersion, StringComparison.Ordinal))
        {
            return InvalidParameters();
        }

        return PipeHandlerResult.Ok(new JsonObject
        {
            ["schema_version"] = IpcContract.CurrentSchemaVersion,
            ["capabilities"] = new JsonArray(
                _capabilities.Select(value => JsonValue.Create(value)).ToArray()),
            ["supported_operations"] = new JsonArray(
                _supportedOperationTypes.Select(value => JsonValue.Create(value)).ToArray()),
            ["cad_application"] = _cadApplication,
            ["cad_version"] = _cadVersion,
        });
    }

    private async ValueTask<PipeHandlerResult> HandleStatusAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!HasExactProperties(parameters, []))
        {
            return InvalidParameters();
        }

        var status = await _host.GetStatusAsync(cancellationToken).ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
        if (!IsSafeOptionalText(status.ActiveDocumentId, IdentifierLimit) ||
            !IsSafeOptionalText(status.Message, 512))
        {
            return PipeHandlerResult.Failed(Error(
                "INTERNAL_ERROR",
                "The bridge host returned an invalid status result."));
        }

        return PipeHandlerResult.Ok(new JsonObject
        {
            ["available"] = status.Available,
            ["cad_application"] = _cadApplication,
            ["cad_version"] = _cadVersion,
            ["active_document_id"] = status.ActiveDocumentId,
            ["message"] = status.Message,
        });
    }

    private async ValueTask<PipeHandlerResult> HandleInspectDocumentAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!RequireCapability("inspect_document"))
        {
            return CapabilityMissing();
        }

        if (parameters.TryGetProperty("response_contract", out _))
        {
            if (!TryReadSemanticDrawingRequest(parameters, out var semanticRequest))
            {
                return InvalidParameters();
            }

            return Map(await _host.InspectSemanticDrawingAsync(
                semanticRequest,
                cancellationToken).ConfigureAwait(false));
        }

        if (!HasExactProperties(parameters, ["document_id", "include_layers", "include_styles"]) ||
            !TryGetOptionalString(parameters, "document_id", IdentifierLimit, out var documentId) ||
            !TryGetBoolean(parameters, "include_layers", out var includeLayers) ||
            !TryGetBoolean(parameters, "include_styles", out var includeStyles))
        {
            return InvalidParameters();
        }

        return Map(await _host.InspectDocumentAsync(
            new InspectDocumentHostRequest(documentId, includeLayers, includeStyles),
            cancellationToken).ConfigureAwait(false));
    }

    private static bool TryReadSemanticDrawingRequest(
        JsonElement parameters,
        out SemanticDrawingHostRequest request)
    {
        request = default!;
        if (!HasExactProperties(
                parameters,
                [
                    "source",
                    "scope",
                    "max_entities",
                    "max_block_nesting_depth",
                    "include_geometry",
                    "response_contract",
                ]) ||
            !parameters.TryGetProperty("source", out var sourceElement) ||
            !TryReadSemanticSource(sourceElement, out var source) ||
            !parameters.TryGetProperty("scope", out var scopeElement) ||
            !TryReadSemanticScope(scopeElement, out var scope) ||
            !TryGetInteger(parameters, "max_entities", 1, 100_000, out var maxEntities) ||
            !TryGetInteger(parameters, "max_block_nesting_depth", 1, 10, out var maxDepth) ||
            !TryGetBoolean(parameters, "include_geometry", out var includeGeometry) ||
            !TryGetString(parameters, "response_contract", 1, 32, out var responseContractText) ||
            !TryParseSemanticResponseContract(responseContractText, out var responseContract) ||
            (scope?.EntityRefs.Count ?? 0) > maxEntities ||
            (responseContract == SemanticDrawingResponseContract.DrawingModel && scope is null))
        {
            return false;
        }

        request = new SemanticDrawingHostRequest(
            source,
            scope,
            maxEntities,
            maxDepth,
            includeGeometry,
            responseContract);
        return true;
    }

    private static bool TryReadSemanticSource(
        JsonElement element,
        out SemanticDrawingSourceHostRequest source)
    {
        source = default!;
        if (!HasExactProperties(element, ["kind", "format", "ref"]) ||
            !TryGetString(element, "kind", 1, 32, out var kind) ||
            !string.Equals(kind, "active_document", StringComparison.Ordinal) ||
            !TryGetString(element, "format", 1, 16, out var rawFormat) ||
            !TryGetString(element, "ref", 1, IdentifierLimit, out var reference))
        {
            return false;
        }

        var format = rawFormat.Trim().TrimStart('.').ToLowerInvariant();
        if (format is not ("dwg" or "dxf"))
        {
            return false;
        }

        source = new SemanticDrawingSourceHostRequest(kind, format, reference);
        return true;
    }

    private static bool TryReadSemanticScope(
        JsonElement element,
        out SemanticReadScopeHostRequest? scope)
    {
        scope = null;
        if (element.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        if (!HasExactProperties(element, ["kind", "layer_name", "layout_name", "entity_refs"]) ||
            !TryGetString(element, "kind", 1, 32, out var kind) ||
            kind is not ("model_space" or "selection" or "layer" or "layout") ||
            !TryGetOptionalString(element, "layer_name", IdentifierLimit, out var layerName) ||
            !TryGetOptionalString(element, "layout_name", IdentifierLimit, out var layoutName) ||
            !TryGetStringArray(element, "entity_refs", IdentifierLimit, 100_000, out var entityRefs))
        {
            return false;
        }

        var selectorsValid = kind switch
        {
            "model_space" => layerName is null && layoutName is null && entityRefs.Count == 0,
            "selection" => layerName is null && layoutName is null && entityRefs.Count > 0,
            "layer" => layerName is not null && layoutName is null && entityRefs.Count == 0,
            "layout" => layerName is null && layoutName is not null && entityRefs.Count == 0,
            _ => false,
        };
        if (!selectorsValid || entityRefs.Count != entityRefs.Distinct(StringComparer.Ordinal).Count())
        {
            return false;
        }

        scope = new SemanticReadScopeHostRequest(kind, layerName, layoutName, entityRefs);
        return true;
    }

    private static bool TryParseSemanticResponseContract(
        string value,
        out SemanticDrawingResponseContract responseContract)
    {
        responseContract = value switch
        {
            "drawing_summary" => SemanticDrawingResponseContract.DrawingSummary,
            "drawing_model" => SemanticDrawingResponseContract.DrawingModel,
            _ => default,
        };
        return value is "drawing_summary" or "drawing_model";
    }

    private async ValueTask<PipeHandlerResult> HandleInspectSelectionAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!RequireCapability("inspect_selection") ||
            !HasExactProperties(parameters, ["document_id", "max_entities"]) ||
            !TryGetString(parameters, "document_id", 1, IdentifierLimit, out var documentId) ||
            !TryGetInteger(parameters, "max_entities", 1, 100_000, out var maxEntities))
        {
            return RequireCapability("inspect_selection") ? InvalidParameters() : CapabilityMissing();
        }

        return Map(await _host.InspectSelectionAsync(
            new InspectSelectionHostRequest(documentId, maxEntities),
            cancellationToken).ConfigureAwait(false));
    }

    private async ValueTask<PipeHandlerResult> HandlePreviewAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!RequireCapability("preview"))
        {
            return CapabilityMissing();
        }

        if (!TryReadPlan(parameters, out var jobId))
        {
            return InvalidParameters();
        }

        return Map(await _host.PreviewAsync(
            new PreviewHostRequest(parameters.Clone(), jobId),
            cancellationToken).ConfigureAwait(false));
    }

    private async ValueTask<PipeHandlerResult> HandleValidateRevisionAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!HasExactProperties(parameters, ["document_id", "expected_revision"]) ||
            !TryGetString(parameters, "document_id", 1, IdentifierLimit, out var documentId) ||
            !TryGetString(parameters, "expected_revision", 1, IdentifierLimit, out var revision))
        {
            return InvalidParameters();
        }

        return Map(await _host.ValidateRevisionAsync(
            new RevisionValidationHostRequest(documentId, revision),
            cancellationToken).ConfigureAwait(false));
    }

    private async ValueTask<PipeHandlerResult> HandleCommitAsync(
        JsonElement envelope,
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!RequireCapability("commit"))
        {
            return CapabilityMissing();
        }

        if (!HasExactProperties(
                parameters,
                ["plan", "idempotency_key", "expected_revision", "approval_token", "create_checkpoint"]) ||
            !parameters.TryGetProperty("plan", out var plan) ||
            !TryReadPlan(plan, out var jobId) ||
            !TryGetString(parameters, "idempotency_key", 1, 128, out var idempotencyKey) ||
            !TryGetString(parameters, "expected_revision", 1, IdentifierLimit, out var expectedRevision) ||
            !TryGetString(parameters, "approval_token", 1, 4096, out var approvalToken) ||
            !TryGetBoolean(parameters, "create_checkpoint", out var createCheckpoint) ||
            !TryGetString(envelope, "job_id", 1, 64, out var envelopeJobId) ||
            !TryGetString(envelope, "idempotency_key", 1, 128, out var envelopeIdempotencyKey) ||
            !string.Equals(jobId, envelopeJobId, StringComparison.Ordinal) ||
            !string.Equals(idempotencyKey, envelopeIdempotencyKey, StringComparison.Ordinal))
        {
            return InvalidParameters();
        }

        return Map(await _host.CommitAsync(
            new CommitHostRequest(
                plan.Clone(),
                jobId,
                idempotencyKey,
                expectedRevision,
                approvalToken,
                createCheckpoint),
            cancellationToken).ConfigureAwait(false));
    }

    private async ValueTask<PipeHandlerResult> HandleRollbackAsync(
        JsonElement envelope,
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!HasExactProperties(parameters, ["job_id", "document_id", "checkpoint_id", "current_revision", "rollback_approval_token", "undo_group"]) ||
            !TryGetString(parameters, "job_id", 1, 64, out var jobId) ||
            !TryGetString(parameters, "document_id", 1, IdentifierLimit, out var documentId) ||
            !TryGetString(parameters, "checkpoint_id", 1, IdentifierLimit, out var checkpointId) ||
            !TryGetString(parameters, "current_revision", 1, IdentifierLimit, out var currentRevision) ||
            !TryGetString(parameters, "rollback_approval_token", 1, 16_384, out var rollbackApprovalToken) ||
            !TryGetOptionalString(parameters, "undo_group", IdentifierLimit, out var undoGroup) ||
            !TryGetString(envelope, "job_id", 1, 64, out var envelopeJobId) ||
            !string.Equals(jobId, envelopeJobId, StringComparison.Ordinal))
        {
            return InvalidParameters();
        }

        var requiredCapability = undoGroup is null
            ? "checkpoint_restore"
            : "rollback_undo_group";
        if (!RequireCapability(requiredCapability))
        {
            return CapabilityMissing();
        }

        return Map(await _host.RollbackAsync(
            new RollbackHostRequest(
                jobId,
                documentId,
                checkpointId,
                currentRevision,
                rollbackApprovalToken,
                undoGroup),
            cancellationToken).ConfigureAwait(false));
    }

    private async ValueTask<PipeHandlerResult> HandleExportAsync(
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (!RequireCapability("export"))
        {
            return CapabilityMissing();
        }

        if (!HasExactProperties(parameters, ["document_id", "format", "target_path", "overwrite"]) ||
            !TryGetString(parameters, "document_id", 1, IdentifierLimit, out var documentId) ||
            !TryGetString(parameters, "format", 1, 8, out var format) ||
            format is not ("dwg" or "dxf" or "pdf") ||
            !TryGetString(parameters, "target_path", 1, 32_768, out var targetPath) ||
            !TryGetBoolean(parameters, "overwrite", out var overwrite))
        {
            return InvalidParameters();
        }

        return Map(await _host.ExportAsync(
            new ExportHostRequest(documentId, format, targetPath, overwrite),
            cancellationToken).ConfigureAwait(false));
    }

    private bool TryReadPlan(JsonElement plan, out string jobId)
    {
        jobId = string.Empty;
        if (!HasExactProperties(plan, PlanProperties) ||
            !TryGetString(plan, "schema_version", 3, 32, out var schemaVersion) ||
            !string.Equals(schemaVersion, IpcContract.CurrentSchemaVersion, StringComparison.Ordinal) ||
            !TryGetString(plan, "plan_id", 1, 64, out _) ||
            !TryGetString(plan, "job_id", 1, 64, out jobId) ||
            !TryGetString(plan, "document_id", 1, IdentifierLimit, out _) ||
            !TryGetString(plan, "expected_revision", 1, IdentifierLimit, out _) ||
            !TryGetString(plan, "canonical_units", 1, 16, out var units) ||
            units != "mm" ||
            !TryGetString(plan, "profile_ref", 1, IdentifierLimit, out _) ||
            !plan.TryGetProperty("operations", out var operations) ||
            operations.ValueKind != JsonValueKind.Array ||
            !plan.TryGetProperty("validation_expectations", out var expectations) ||
            expectations.ValueKind != JsonValueKind.Array ||
            !TryGetOptionalString(plan, "plan_hash", IdentifierLimit, out _))
        {
            return false;
        }

        foreach (var operation in operations.EnumerateArray())
        {
            if (!HasExactProperties(operation, OperationProperties) ||
                !TryGetString(operation, "operation_id", 1, 64, out _) ||
                !TryGetString(operation, "feature_id", 1, 64, out _) ||
                !TryGetString(operation, "type", 1, 64, out var type) ||
                !KnownOperationTypes.Contains(type) ||
                !_operationTypeSet.Contains(type) ||
                !TryGetString(operation, "layer", 1, IdentifierLimit, out _) ||
                !HasObjectProperty(operation, "geometry") ||
                !HasObjectProperty(operation, "expected") ||
                !TryGetOptionalString(operation, "target_entity_ref", IdentifierLimit, out _))
            {
                return false;
            }
        }

        foreach (var expectation in expectations.EnumerateArray())
        {
            if (!HasExactProperties(expectation, ValidationExpectationProperties) ||
                !TryGetString(expectation, "rule_id", 1, 64, out _) ||
                !TryGetOptionalString(expectation, "feature_id", 64, out _) ||
                !TryGetOptionalString(expectation, "operation_id", 64, out _) ||
                !HasObjectProperty(expectation, "expected"))
            {
                return false;
            }
        }

        return true;
    }

    private static void ValidateDescriptor(BridgeHostDescriptor descriptor)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(descriptor.CadApplication);
        ArgumentException.ThrowIfNullOrWhiteSpace(descriptor.CadVersion);
        ArgumentNullException.ThrowIfNull(descriptor.Capabilities);
        ArgumentNullException.ThrowIfNull(descriptor.SupportedOperationTypes);
        if (descriptor.CadApplication.Length > IdentifierLimit ||
            descriptor.CadVersion.Length > IdentifierLimit ||
            !IsSafeOptionalText(descriptor.CadApplication, IdentifierLimit) ||
            !IsSafeOptionalText(descriptor.CadVersion, IdentifierLimit) ||
            descriptor.Capabilities.Count != descriptor.Capabilities.Distinct(StringComparer.Ordinal).Count() ||
            descriptor.SupportedOperationTypes.Count != descriptor.SupportedOperationTypes.Distinct(StringComparer.Ordinal).Count() ||
            descriptor.Capabilities.Any(value => !KnownCapabilities.Contains(value)) ||
            descriptor.SupportedOperationTypes.Any(value => !KnownOperationTypes.Contains(value)))
        {
            throw new ArgumentException("The bridge host descriptor is invalid.", nameof(descriptor));
        }

        var capabilities = descriptor.Capabilities.ToHashSet(StringComparer.Ordinal);
        if (capabilities.Contains("commit") &&
            !new[] { "atomic_transaction", "document_lock", "undo_group", "stable_metadata" }
                .All(capabilities.Contains))
        {
            throw new ArgumentException(
                "A commit host must declare every atomic write guarantee.",
                nameof(descriptor));
        }

        if (!capabilities.Contains("commit") && descriptor.SupportedOperationTypes.Count != 0)
        {
            throw new ArgumentException(
                "Operation types cannot be advertised without the commit capability.",
                nameof(descriptor));
        }
    }

    private static bool TryReadEnvelope(
        JsonElement request,
        out string method,
        out JsonElement parameters)
    {
        method = string.Empty;
        parameters = default;
        return HasOnlyKnownProperties(request, EnvelopeProperties) &&
            TryGetString(request, "schema_version", 3, 32, out var schemaVersion) &&
            string.Equals(schemaVersion, IpcContract.CurrentSchemaVersion, StringComparison.Ordinal) &&
            TryGetString(request, "method", 1, 64, out method) &&
            TryGetString(request, "request_id", 1, 64, out _) &&
            request.TryGetProperty("params", out parameters) &&
            parameters.ValueKind == JsonValueKind.Object &&
            IsOptionalBoundedString(request, "job_id", 64) &&
            IsOptionalBoundedString(request, "idempotency_key", 128);
    }

    private bool RequireCapability(string capability) => _capabilitySet.Contains(capability);

    private static PipeHandlerResult Map(BridgeHostResult? result)
    {
        if (result is null)
        {
            return PipeHandlerResult.Failed(Error(
                "INTERNAL_ERROR",
                "The bridge host returned no terminal outcome."));
        }

        return result.Outcome switch
        {
            BridgeHostOutcome.Ok when result.Data is not null => PipeHandlerResult.Ok(result.Data),
            BridgeHostOutcome.Ok => PipeHandlerResult.Failed(Error(
                "INTERNAL_ERROR",
                "The bridge host returned no result data.")),
            BridgeHostOutcome.Rejected => PipeHandlerResult.Rejected(Error(
                "INVALID_FEATURE_PARAMETERS",
                "The bridge host rejected the request.")),
            BridgeHostOutcome.Conflict => PipeHandlerResult.Conflict(Error(
                "WRITER_LEASE_CONFLICT",
                "The bridge host detected a write conflict.",
                retryable: true)),
            BridgeHostOutcome.IdempotencyKeyReused => PipeHandlerResult.Conflict(Error(
                "IDEMPOTENCY_KEY_REUSED",
                "The idempotency key was already used for a different request.")),
            BridgeHostOutcome.StaleDocumentRevision => PipeHandlerResult.Conflict(Error(
                "STALE_DOCUMENT_REVISION",
                "The target document revision changed.",
                requiredAction: "Read the document again and compile a new plan.")),
            BridgeHostOutcome.UnknownCommitState => PipeHandlerResult.Failed(Error(
                "UNKNOWN_COMMIT_STATE",
                "The commit outcome is unknown.",
                requiredAction: "Reconcile the job before any further commit attempt.")),
            BridgeHostOutcome.RollbackRecoveryRequired => PipeHandlerResult.Failed(Error(
                "ROLLBACK_RECOVERY_REQUIRED",
                "The journaled whole-DWG restore requires an exact retry.",
                retryable: true,
                requiredAction:
                    "Retry the exact rollback with its original in-memory approval and scope.")),
            _ => PipeHandlerResult.Failed(Error(
                "COM_CALL_FAILED",
                "The AutoCAD bridge operation failed safely.")),
        };
    }

    private static PipeHandlerResult InvalidParameters() => PipeHandlerResult.Rejected(Error(
        "INVALID_FEATURE_PARAMETERS",
        "The bridge method parameters do not match the strict contract."));

    private static PipeHandlerResult CapabilityMissing() => PipeHandlerResult.Rejected(Error(
        "ADAPTER_CAPABILITY_MISSING",
        "The bridge host did not declare the required capability."));

    private static PipeHandlerResult Unsupported(string message) => PipeHandlerResult.Rejected(Error(
        "UNSUPPORTED_FEATURE",
        message));

    private static IpcError Error(
        string code,
        string message,
        bool retryable = false,
        string? requiredAction = null) =>
        new(code, message)
        {
            Retryable = retryable,
            RequiredAction = requiredAction,
        };

    private static bool HasExactProperties(JsonElement element, IEnumerable<string> expected)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        var expectedSet = expected.ToHashSet(StringComparer.Ordinal);
        var actual = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
        {
            if (!actual.Add(property.Name))
            {
                return false;
            }
        }

        return actual.SetEquals(expectedSet);
    }

    private static bool HasOnlyKnownProperties(JsonElement element, HashSet<string> known)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        var actual = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
        {
            if (!known.Contains(property.Name) || !actual.Add(property.Name))
            {
                return false;
            }
        }

        return true;
    }

    private static bool HasObjectProperty(JsonElement element, string propertyName) =>
        element.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.Object;

    private static bool TryGetString(
        JsonElement element,
        string propertyName,
        int minimumLength,
        int maximumLength,
        out string value)
    {
        value = string.Empty;
        if (!element.TryGetProperty(propertyName, out var property) ||
            property.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        value = property.GetString() ?? string.Empty;
        return value.Length >= minimumLength &&
            value.Length <= maximumLength &&
            !value.Any(char.IsControl);
    }

    private static bool TryGetOptionalString(
        JsonElement element,
        string propertyName,
        int maximumLength,
        out string? value)
    {
        value = null;
        if (!element.TryGetProperty(propertyName, out var property))
        {
            return false;
        }

        if (property.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        if (property.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        value = property.GetString();
        return value is not null && value.Length is >= 1 && value.Length <= maximumLength &&
            !value.Any(char.IsControl);
    }

    private static bool IsOptionalBoundedString(
        JsonElement element,
        string propertyName,
        int maximumLength)
    {
        if (!element.TryGetProperty(propertyName, out var property) ||
            property.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        return property.ValueKind == JsonValueKind.String &&
            property.GetString() is { Length: >= 1 } value &&
            value.Length <= maximumLength &&
            !value.Any(char.IsControl);
    }

    private static bool IsSafeOptionalText(string? value, int maximumLength) =>
        value is null ||
        (value.Length is >= 1 && value.Length <= maximumLength && !value.Any(char.IsControl));

    private static bool TryGetBoolean(
        JsonElement element,
        string propertyName,
        out bool value)
    {
        value = false;
        if (!element.TryGetProperty(propertyName, out var property) ||
            property.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
        {
            return false;
        }

        value = property.GetBoolean();
        return true;
    }

    private static bool TryGetInteger(
        JsonElement element,
        string propertyName,
        int minimum,
        int maximum,
        out int value)
    {
        value = 0;
        return element.TryGetProperty(propertyName, out var property) &&
            property.ValueKind == JsonValueKind.Number &&
            property.TryGetInt32(out value) &&
            value >= minimum &&
            value <= maximum;
    }

    private static bool TryGetStringArray(
        JsonElement element,
        string propertyName,
        int maximumItemLength,
        int maximumItems,
        out IReadOnlyList<string> values)
    {
        values = [];
        if (!element.TryGetProperty(propertyName, out var property) ||
            property.ValueKind != JsonValueKind.Array ||
            property.GetArrayLength() > maximumItems)
        {
            return false;
        }

        var parsed = new List<string>(property.GetArrayLength());
        foreach (var item in property.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String ||
                item.GetString() is not { Length: >= 1 } value ||
                value.Length > maximumItemLength ||
                value.Any(char.IsControl))
            {
                return false;
            }

            parsed.Add(value);
        }

        values = parsed;
        return true;
    }
}
