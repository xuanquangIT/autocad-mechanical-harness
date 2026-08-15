from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST_PATH = PROJECT_ROOT / "dotnet" / "AutoCADBridge" / "CadBridge.Plugin" / "AutoCadBridgeHost.cs"


def _source() -> str:
    return HOST_PATH.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_checkpoint_restore_is_separately_gated_and_fail_closed() -> None:
    source = _source()
    constructor = _between(
        source,
        "public AutoCadBridgeHost(",
        "public BridgeHostDescriptor Descriptor",
    )

    assert '"CAD_HARNESS_DURABLE_RESTORE_VERIFIED"' in source
    assert "_durableRestore = _writeEnabled && VerifiedBuildTuple && string.Equals(" in constructor
    assert (
        "Environment.GetEnvironmentVariable(DurableRestoreGateEnvironmentVariable)" in constructor
    )
    assert '"1",\n            StringComparison.Ordinal)' in constructor
    assert "? TryCreateDurableRestoreSubsystem(documents)\n            : null;" in constructor
    capability_guard = constructor.index("if (_durableRestore is not null)")
    assert constructor.index('capabilities.Add("checkpoint_restore")') > capability_guard

    initializer = _between(
        source,
        "private static DurableRestoreSubsystem? TryCreateDurableRestoreSubsystem(",
        "private static byte[] DeriveDurableAuthenticationKey(",
    )
    assert '"whole-dwg-checkpoints-v1"' in initializer
    assert '"whole-dwg-restore-journal-v1"' in initializer
    assert "Path.IsPathFullyQualified(baseRoot)" in initializer
    assert "RejectNetworkOrDevicePath(canonicalBaseRoot);" in initializer
    assert "RejectExistingReparseComponents(canonicalBaseRoot);" in initializer
    assert "catch\n        {" in initializer
    assert "coordinator?.Dispose();" in initializer
    assert "catalog?.Dispose();" in initializer
    assert "return null;" in initializer

    derivation = _between(
        source,
        "private static byte[] DeriveDurableAuthenticationKey(",
        "private static string RequireCanonicalWritableLocalDwg(",
    )
    assert '"cad-harness-durable-key-derivation-v1\\0" + purpose' in derivation
    assert "HMACSHA256.HashData(secretBytes, context)" in derivation
    assert derivation.count("CryptographicOperations.ZeroMemory(") == 2


def test_durable_checkpoint_is_registered_under_lock_before_operations() -> None:
    source = _source()
    commit = _between(
        source,
        "private async ValueTask<BridgeHostResult> CommitOnceAsync(",
        "private async ValueTask<BridgeHostResult> InDocumentContextAsync(",
    )
    inspect_index = commit.index("snapshot = service.InspectDocument(token);")
    exact_revision_index = commit.index(
        "!string.Equals(snapshot.Revision, request.ExpectedRevision, StringComparison.Ordinal)"
    )
    checkpoint_index = commit.index("checkpointArtifact = CreateCheckpoint(")
    operations_index = commit.index("dispatcher.ValidateBeforeCommitAsync")
    assert inspect_index < exact_revision_index < checkpoint_index < operations_index

    checkpoint = _between(
        source,
        "private static CheckpointArtifact CreateDurableCheckpoint(",
        "private void RetireCheckpointAfterProvenPreCommitFailure(",
    )
    path_index = checkpoint.index("RequireCanonicalWritableLocalDwg(document)")
    save_index = checkpoint.index("document.Database.SaveAs(")
    assert "bBakAndRename: false" in checkpoint
    assert "DwgVersion.Current" in checkpoint
    assert "securityParameters" in checkpoint
    name_fence_index = checkpoint.index(
        "!string.Equals(document.Name, originalName, StringComparison.Ordinal)"
    )
    fingerprint_fence_index = checkpoint.index(
        "document.Database.FingerprintGuid", name_fence_index
    )
    artifact_index = checkpoint.index("var artifact = new FileInfo(target);")
    register_index = checkpoint.index("subsystem.Catalog.RegisterCheckpoint(")
    return_index = checkpoint.index(
        "return new CheckpointArtifact(checkpointId, target, IsDurable: true);"
    )
    assert (
        path_index
        < save_index
        < name_fence_index
        < fingerprint_fence_index
        < artifact_index
        < register_index
        < return_index
    )

    retire = _between(
        source,
        "private void RetireCheckpointAfterProvenPreCommitFailure(",
        "private static void TryDeleteCheckpointArtifact(",
    )
    first_transition = min(retire.index("catalog.Expire("), retire.index("catalog.Quarantine("))
    assert first_transition < retire.rindex("TryDeleteCheckpointArtifact(checkpoint.Path);")
    unknown_index = commit.index("AtomicExecutionOutcome.UnknownCommitState")
    cleanup_index = commit.index("RetireCheckpointAfterProvenPreCommitFailure(")
    assert unknown_index < cleanup_index


def test_durable_rollback_uses_rb1_exact_document_fences_and_shared_writer_gate() -> None:
    source = _source()
    rollback = _between(
        source,
        "public async ValueTask<BridgeHostResult> RollbackAsync(",
        "private async ValueTask<BridgeHostResult> ExecuteSessionRollbackAsync(",
    )
    gate_index = rollback.index("await _commitGate.WaitAsync(cancellationToken);")
    authorization_index = rollback.index("BridgeAuthorization.TryValidateRollbackAuthorization(")
    recovery_index = rollback.index("ValidateDurableRecoveryAuthorization(")
    branch_index = rollback.index("return request.UndoGroup is null")
    assert gate_index < authorization_index < recovery_index < branch_index
    assert "? await ExecuteDurableRollbackAsync(request, claims, cancellationToken)" in rollback
    assert ": await ExecuteSessionRollbackAsync(request, claims, cancellationToken);" in rollback

    recovery = _between(
        source,
        "private DurableRecoveryAuthorizationOutcome ValidateDurableRecoveryAuthorization(",
        "private async ValueTask<BridgeHostResult> ExecuteSessionRollbackAsync(",
    )
    assert "BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(" in recovery
    assert "subsystem.Coordinator.CanResumeExactRecordedAttempt(" in recovery
    assert (
        recovery.index("BridgeAuthorization.TryValidateRollbackRecoveryAuthorization(")
        < recovery.index("subsystem.Catalog.GetRequired(request.CheckpointId)")
        < recovery.index("subsystem.Coordinator.CanResumeExactRecordedAttempt(")
    )
    assert "checkpoint.PreRevision" in recovery
    assert "recoveryClaims.ApprovalTokenDigest" in recovery
    assert "catch (InvalidDataException)" in recovery
    assert "TryQuarantineDurableCheckpoint(" in recovery
    contention = _between(recovery, "catch (IOException)", "catch\n        {")
    assert "DurableRecoveryAuthorizationOutcome.RecoveryRequired" in contention
    assert "TryQuarantineDurableCheckpoint(" not in contention
    generic_catch = recovery.index("catch\n        {")
    assert "TryQuarantineDurableCheckpoint(" not in recovery[generic_catch:]

    durable = _between(
        source,
        "private async ValueTask<BridgeHostResult> ExecuteDurableRollbackAsync(",
        "private async ValueTask<string> ResolveUniqueDurableRollbackTargetAsync(",
    )
    assert "checkpoint = subsystem.Catalog.GetRequired(request.CheckpointId);" in durable
    assert "checkpoint.PreRevision" in durable
    assert "new ValidatedRollbackAuthorization(" in durable
    assert "claims.ApprovalTokenDigest" in durable
    protected_resolver_index = durable.index(
        "subsystem.Coordinator.ResolveRecordedTarget(authorization)"
    )
    open_document_resolver_index = durable.index("ResolveUniqueDurableRollbackTargetAsync(")
    assert protected_resolver_index < open_document_resolver_index
    assert "subsystem.Coordinator.RestoreAsync(" in durable
    assert "DurableCheckpointRestoreOutcome.Completed" in durable
    assert "DurableCheckpointRestoreOutcome.Replayed" in durable
    assert '["method"] = "checkpoint_restore"' in durable
    assert "TryQuarantineDurableCheckpoint(" in durable
    cancelled_catch = _between(
        durable,
        "catch (OperationCanceledException)",
        "catch (InvalidDataException)",
    )
    assert "TryQuarantineDurableCheckpoint(" not in cancelled_catch
    generic_failure = durable[durable.index("catch\n        {", durable.index("RestoreAsync(")) :]
    generic_failure = generic_failure[: generic_failure.index("if ((restore.Outcome")]
    assert "TryQuarantineDurableCheckpoint(" not in generic_failure
    outcome_mapping = durable[durable.index("if (restore.Outcome ==") :]
    assert "restore.Outcome == DurableCheckpointRestoreOutcome.Quarantined" in outcome_mapping
    assert "DurableCheckpointRestoreOutcome.RecoveryRequired =>" in outcome_mapping
    assert "BridgeHostOutcome.RollbackRecoveryRequired" in outcome_mapping
    assert "DurableCheckpointRestoreOutcome.Cancelled =>" in outcome_mapping
    assert "DurableCheckpointRestoreOutcome.Rejected =>" in outcome_mapping
    assert "DurableCheckpointRestoreOutcome.ScopeConflict =>" in outcome_mapping

    resolver = _between(
        source,
        "private async ValueTask<string> ResolveUniqueDurableRollbackTargetAsync(",
        "private static void TryQuarantineDurableCheckpoint(",
    )
    assert "MdiActiveDocument" not in resolver
    assert "foreach (Document document in _documents)" in resolver
    assert "snapshot.DocumentId" in resolver
    assert "request.CurrentRevision" in resolver
    assert "matchingDocuments != 1" in resolver
    assert "RequireCanonicalWritableLocalDwg(document)" in resolver
    assert "DurableCheckpointCatalog.ComputeOriginalPathHash(candidatePath)" in resolver

    dispose = _between(
        source,
        "public void Dispose()",
        "private async ValueTask<BridgeHostResult> CommitOnceAsync(",
    )
    assert "_durableRestore?.Dispose();" in dispose
    subsystem = _between(
        source,
        "private sealed class DurableRestoreSubsystem : IDisposable",
        "internal sealed class AutoCadBridgeHostFactory",
    )
    assert subsystem.index("Coordinator.Dispose();") < subsystem.index("Catalog.Dispose();")


def test_fresh_and_post_proof_rb1_share_typed_target_lock_contention() -> None:
    source = _source()
    rollback = _between(
        source,
        "public async ValueTask<BridgeHostResult> RollbackAsync(",
        "private async ValueTask<BridgeHostResult> ExecuteSessionRollbackAsync(",
    )
    normal = rollback.index("BridgeAuthorization.TryValidateRollbackAuthorization(")
    recovery = rollback.index("ValidateDurableRecoveryAuthorization(")
    shared_durable_branch = rollback.index(
        "? await ExecuteDurableRollbackAsync(request, claims, cancellationToken)"
    )
    assert normal < recovery < shared_durable_branch

    durable = _between(
        source,
        "private async ValueTask<BridgeHostResult> ExecuteDurableRollbackAsync(",
        "private async ValueTask<string> ResolveUniqueDurableRollbackTargetAsync(",
    )
    target_resolution = durable[: durable.index("DurableCheckpointRestoreResult restore;")]
    contention = _between(target_resolution, "catch (IOException)", "catch\n        {")
    assert "BridgeHostOutcome.RollbackRecoveryRequired" in contention
    assert "TryQuarantineDurableCheckpoint(" not in contention
    assert "Coordinator.RestoreAsync(" not in target_resolution
