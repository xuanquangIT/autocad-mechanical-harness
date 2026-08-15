from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = (
    ROOT
    / "dotnet"
    / "AutoCADBridge"
    / "CadBridge.Plugin"
    / "AutoCadDurableRestoreDocumentLifecycle.cs"
)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


def test_lifecycle_uses_only_typed_exact_document_apis() -> None:
    required = {
        "IDurableRestoreDocumentLifecycle",
        "ExecuteInApplicationContext",
        "AppContextOpenDocument",
        "DocumentExtension.CloseAndDiscard",
        "document.Name",
        "document.IsNamedDrawing",
        "AutoCadInspectionDocument",
        "BridgeInspectionService",
        "DurableCheckpointCatalog.ComputeOriginalPathHash",
    }
    forbidden = {
        "MdiActiveDocument",
        "CurrentDocument",
        "Path.GetFileName",
        "SendStringToExecute",
        "Editor.Command",
        "System.Reflection",
        "dynamic ",
    }

    assert required <= {token for token in required if token in SOURCE}
    assert not {token for token in forbidden if token in SOURCE}


def test_lifecycle_serializes_dispatch_and_surfaces_callback_completion() -> None:
    assert "new(1, 1)" in SOURCE
    assert "_lifecycleGate.WaitAsync(cancellationToken)" in SOURCE
    assert "TaskCreationOptions.RunContinuationsAsynchronously" in SOURCE
    assert "TrySetException(exception)" in SOURCE
    assert "await invocation.Completion.ConfigureAwait(false)" in SOURCE


def test_lifecycle_rejects_noncanonical_remote_or_reparse_targets() -> None:
    required_guards = {
        "Path.IsPathFullyQualified",
        "Path.GetFullPath",
        "NormalizationForm.FormC",
        'Path.GetExtension(fullPath), ".dwg"',
        "DriveType.Network",
        "FileAttributes.ReparsePoint",
        "FileAttributes.Device",
        "FileAttributes.ReadOnly",
    }

    assert required_guards <= {token for token in required_guards if token in SOURCE}


def test_close_has_no_cancellation_observation_after_destructive_boundary() -> None:
    close_call = SOURCE.index("DocumentExtension.CloseAndDiscard(document);")
    prior_cancellation = SOURCE.rfind("token.ThrowIfCancellationRequested();", 0, close_call)
    callback_return = SOURCE.index("return true;", close_call)

    assert prior_cancellation != -1
    assert "ThrowIfCancellationRequested" not in SOURCE[close_call:callback_return]
