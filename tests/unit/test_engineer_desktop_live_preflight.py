from __future__ import annotations

from pathlib import Path

import pytest
from apps.engineer_desktop.__main__ import _build_context_after_live_preflight
from apps.mcp_server.context import build_context

from cad_harness.application.manual_gate import MANUAL_STEP_INSTRUCTIONS, ManualStepId
from cad_harness.config import AdapterSettings, Settings
from cad_harness.domain.errors import ApprovalRequiredError

_SETUP_STEP_IDS = tuple(step.value for step in tuple(ManualStepId)[:5])


def _settings(adapter_type: str) -> Settings:
    return Settings(adapter=AdapterSettings(type=adapter_type))  # type: ignore[arg-type]


def test_live_context_is_not_built_after_wrong_confirmation() -> None:
    events: list[str] = []
    prompt_count = 0

    def reject_then_stop(prompt: str) -> str:
        nonlocal prompt_count
        prompt_count += 1
        events.append(prompt)
        if prompt_count == 1:
            return ManualStepId.CONFIRM_AUTOCAD_VERSION.value
        raise RuntimeError("stop after proving the gate did not advance")

    with pytest.raises(RuntimeError, match="stop after proving"):
        _build_context_after_live_preflight(
            None,
            input_fn=reject_then_stop,
            output_fn=lambda message: events.append(message),
            settings_loader=lambda _path: _settings("com"),
            context_builder=lambda _path, **_kwargs: events.append("BUILD_CONTEXT"),
        )

    assert "BUILD_CONTEXT" not in events
    first_instruction = MANUAL_STEP_INSTRUCTIONS[ManualStepId.OPEN_TARGET_DRAWING]
    assert sum(first_instruction in event for event in events) == 2


def test_live_context_is_built_only_after_all_five_exact_confirmations() -> None:
    events: list[str] = []
    confirmations = iter(_SETUP_STEP_IDS)

    def confirm(prompt: str) -> str:
        events.append(prompt)
        return next(confirmations)

    result = _build_context_after_live_preflight(
        Path("live.yaml"),
        input_fn=confirm,
        output_fn=lambda message: events.append(message),
        settings_loader=lambda _path: _settings("dotnet_bridge"),
        context_builder=lambda path, **_kwargs: events.append("BUILD_CONTEXT") or path,
    )

    assert result == Path("live.yaml")
    assert events[-1] == "BUILD_CONTEXT"
    assert any("approve_commit gate remains pending" in event for event in events)
    assert sum("Type the exact step id" in event for event in events) == 5


@pytest.mark.parametrize("adapter_type", ["fake", "dxf_preview"])
def test_offline_adapters_do_not_prompt(adapter_type: str) -> None:
    prompts: list[str] = []

    result = _build_context_after_live_preflight(
        None,
        input_fn=lambda prompt: prompts.append(prompt) or "unexpected",
        output_fn=prompts.append,
        settings_loader=lambda _path: _settings(adapter_type),
        context_builder=lambda _path, **_kwargs: "context",
    )

    assert result == "context"
    assert prompts == []


def test_noninteractive_live_context_fails_before_adapter_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text("adapter:\n  type: com\n", encoding="utf-8")
    monkeypatch.delenv("CAD_HARNESS_MANUAL_GATE_CONFIRMATIONS", raising=False)

    with pytest.raises(ApprovalRequiredError) as error:
        build_context(config_path)

    assert ManualStepId.OPEN_TARGET_DRAWING.value in str(error.value.required_action)
