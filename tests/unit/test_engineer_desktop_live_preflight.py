from __future__ import annotations

from pathlib import Path

import pytest
from apps.engineer_desktop.__main__ import _build_context_after_live_preflight
from apps.mcp_server.context import _reinspect_unchanged_live_target, build_context

from cad_harness.application.manual_gate import (
    LIVE_SETUP_STEPS,
    MANUAL_STEP_INSTRUCTIONS,
    ManualStepId,
    require_live_setup_confirmations,
    required_live_setup_steps,
)
from cad_harness.config import AdapterSettings, Settings
from cad_harness.domain.errors import ApprovalRequiredError, ApprovalScopeMismatchError
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.ports.autocad_adapter import AdapterStatus
from cad_harness.domain.value_objects.units import Unit

_SETUP_STEP_IDS = tuple(step.value for step in LIVE_SETUP_STEPS)


def _settings(adapter_type: str) -> Settings:
    return Settings(adapter=AdapterSettings(type=adapter_type))  # type: ignore[arg-type]


def _status(adapter_type: str) -> AdapterStatus:
    return AdapterStatus(
        adapter_type=adapter_type,
        available=True,
        cad_version="25.0",
        version_supported=True,
        active_document_id="doc-1",
        process_id=4242,
    )


def _snapshot() -> DocumentSnapshot:
    return DocumentSnapshot(
        document_id="doc-1",
        revision="rev-1",
        path_hash="sha256:" + "1" * 64,
        display_name="fixture.dwg",
        units=Unit.MM,
    )


def _run_provider(kwargs: dict[str, object], adapter_type: str) -> tuple[ManualStepId, ...]:
    provider = kwargs["manual_confirmation_provider"]
    assert callable(provider)
    return provider(
        adapter_type,
        _status(adapter_type),
        _snapshot(),
        "demo-profile@1.0",
    )


def test_live_adapter_attaches_before_confirmation_but_context_is_not_returned_on_rejection() -> (
    None
):
    events: list[str] = []
    prompt_count = 0

    def reject_then_stop(prompt: str) -> str:
        nonlocal prompt_count
        prompt_count += 1
        events.append(prompt)
        if prompt_count == 1:
            return ManualStepId.CONFIRM_AUTOCAD_VERSION.value
        raise RuntimeError("stop after proving the gate did not advance")

    def attach_then_confirm(_path: Path | None, **kwargs: object) -> str:
        events.append("ATTACH_LIVE_TARGET")
        _run_provider(kwargs, "com")
        events.append("RETURN_CONTEXT")
        return "context"

    with pytest.raises(RuntimeError, match="stop after proving"):
        _build_context_after_live_preflight(
            None,
            input_fn=reject_then_stop,
            output_fn=lambda message: events.append(message),
            settings_loader=lambda _path: _settings("com"),
            context_builder=attach_then_confirm,
        )

    assert events[0] == "ATTACH_LIVE_TARGET"
    assert events[1].startswith("Pinned live target:")
    assert "RETURN_CONTEXT" not in events
    first_instruction = MANUAL_STEP_INSTRUCTIONS[ManualStepId.LOAD_COMPANY_STANDARDS]
    assert sum(first_instruction in event for event in events) == 2


def test_live_context_is_built_only_after_all_five_exact_confirmations() -> None:
    events: list[str] = []
    confirmations = iter(_SETUP_STEP_IDS)

    def confirm(prompt: str) -> str:
        events.append(prompt)
        return next(confirmations)

    def build_after_confirmation(path: Path | None, **kwargs: object) -> Path | None:
        _run_provider(kwargs, "dotnet_bridge")
        events.append("BUILD_CONTEXT")
        return path

    result = _build_context_after_live_preflight(
        Path("live.yaml"),
        input_fn=confirm,
        output_fn=lambda message: events.append(message),
        settings_loader=lambda _path: _settings("dotnet_bridge"),
        context_builder=build_after_confirmation,
    )

    assert result == Path("live.yaml")
    assert events[-1] == "BUILD_CONTEXT"
    assert any("approve_commit gate remains pending" in event for event in events)
    assert sum("Type the exact step id" in event for event in events) == 5


def test_com_preflight_never_requests_bridge_or_pipe_setup() -> None:
    events: list[str] = []
    expected_steps = required_live_setup_steps("com")
    confirmations = iter(step.value for step in expected_steps)
    context_arguments: dict[str, object] = {}

    def build(path: Path | None, **kwargs: object) -> str:
        context_arguments.update(path=path, **kwargs)
        context_arguments["confirmed"] = _run_provider(kwargs, "com")
        return "context"

    result = _build_context_after_live_preflight(
        Path("live-com.yaml"),
        input_fn=lambda prompt: events.append(prompt) or next(confirmations),
        output_fn=events.append,
        settings_loader=lambda _path: _settings("com"),
        context_builder=build,
    )

    assert result == "context"
    assert expected_steps == (ManualStepId.LOAD_COMPANY_STANDARDS,)
    assert context_arguments["confirmed"] == expected_steps
    assert "manual_confirmation_provider" in context_arguments
    assert events[0].startswith("Pinned live target: adapter=com; PID=4242;")
    assert "document=fixture.dwg" in events[0]
    assert "revision=rev-1" in events[0]
    assert sum("Type the exact step id" in event for event in events) == 1
    assert all(ManualStepId.INSTALL_BRIDGE_BUNDLE.value not in event for event in events)
    assert all(ManualStepId.GRANT_NAMED_PIPE_ACL.value not in event for event in events)


def test_confirmation_policy_is_exact_for_each_live_adapter() -> None:
    com_steps = required_live_setup_steps("COM")

    assert require_live_setup_confirmations("com", com_steps) == com_steps
    assert require_live_setup_confirmations("dotnet_bridge", LIVE_SETUP_STEPS) == LIVE_SETUP_STEPS
    assert required_live_setup_steps("fake") == ()
    with pytest.raises(ApprovalRequiredError):
        require_live_setup_confirmations("com", LIVE_SETUP_STEPS)


class _ReinspectionAdapter:
    def __init__(self, status: AdapterStatus, snapshot: DocumentSnapshot) -> None:
        self._status = status
        self._snapshot = snapshot

    def status(self) -> AdapterStatus:
        return self._status

    def inspect_document(self, _request: object) -> DocumentSnapshot:
        return self._snapshot


def test_live_target_is_reinspected_after_confirmation() -> None:
    status = _status("com")
    snapshot = _snapshot()

    actual_status, actual_snapshot = _reinspect_unchanged_live_target(
        _ReinspectionAdapter(status, snapshot),  # type: ignore[arg-type]
        status,
        snapshot,
    )

    assert actual_status == status
    assert actual_snapshot == snapshot


def test_document_switch_during_confirmation_fails_closed() -> None:
    original_status = _status("com")
    switched_status = original_status.model_copy(update={"active_document_id": "doc-2"})

    with pytest.raises(ApprovalScopeMismatchError, match="session changed"):
        _reinspect_unchanged_live_target(
            _ReinspectionAdapter(switched_status, _snapshot()),  # type: ignore[arg-type]
            original_status,
            _snapshot(),
        )


@pytest.mark.parametrize("adapter_type", ["fake", "dxf_preview"])
def test_offline_adapters_do_not_prompt(adapter_type: str) -> None:
    prompts: list[str] = []

    result = _build_context_after_live_preflight(
        None,
        input_fn=lambda prompt: prompts.append(prompt) or "unexpected",
        output_fn=prompts.append,
        settings_loader=lambda _path: _settings(adapter_type),
        context_builder=lambda _path, **kwargs: "context" if not kwargs else "unexpected",
    )

    assert result == "context"
    assert prompts == []


def test_noninteractive_read_only_bridge_does_not_require_persisted_confirmations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text(
        "\n".join(
            (
                "adapter:",
                "  type: dotnet_bridge",
                "storage:",
                f"  sqlite_path: {tmp_path / 'harness.db'}",
                f"  preview_directory: {tmp_path / 'previews'}",
                f"  checkpoint_directory: {tmp_path / 'checkpoints'}",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS", raising=False)
    monkeypatch.delenv("CAD_HARNESS_LIVE_WRITE_VERIFIED", raising=False)
    monkeypatch.delenv("CAD_HARNESS_LIVE_SESSION_PROOF", raising=False)

    context = build_context(config_path)

    assert context.settings.adapter.type == "dotnet_bridge"


def test_unsigned_bridge_instruction_is_limited_to_owned_disposable_acceptance() -> None:
    instruction = MANUAL_STEP_INSTRUCTIONS[ManualStepId.INSTALL_BRIDGE_BUNDLE]

    assert "development-unsigned" in instruction
    assert "PID-owned disposable" in instruction
