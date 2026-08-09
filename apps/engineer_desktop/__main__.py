"""Launch the human-only engineer approval desktop."""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from apps.engineer_desktop.approval_window import create_approval_window
from apps.engineer_desktop.controller import EngineerDesktopController
from apps.mcp_server.context import build_context
from cad_harness.application.manual_gate import LIVE_SETUP_STEPS, ManualGate, ManualStepId
from cad_harness.config import Settings, load_settings


def _run_live_setup_preflight(
    settings: Settings,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> tuple[ManualStepId, ...]:
    """Require the five setup confirmations before a live adapter can attach."""
    if settings.adapter.type not in {"com", "dotnet_bridge"}:
        return ()

    gate = ManualGate.live_autocad()
    confirmed_steps: list[ManualStepId] = []
    for expected_step_id in LIVE_SETUP_STEPS:
        step = gate.current_step
        if step is None or step.step_id is not expected_step_id:
            raise RuntimeError("Live AutoCAD manual gate sequence is inconsistent")

        while True:
            output_fn(gate.notification())
            confirmation = input_fn(
                f"Type the exact step id '{step.step_id.value}' to confirm: "
            ).strip()
            if confirmation != step.step_id.value:
                output_fn("Confirmation rejected; the current manual step has not advanced.")
                continue
            gate.confirm(step.step_id)
            gate.run_next(lambda: None)
            confirmed_steps.append(step.step_id)
            break

    final_step = gate.current_step
    if final_step is None or final_step.step_id is not ManualStepId.APPROVE_COMMIT:
        raise RuntimeError("Live AutoCAD preflight did not reach the commit approval gate")
    output_fn(gate.notification())
    output_fn(
        "The approve_commit gate remains pending and can only be completed with "
        "the human approval button in the Engineer Desktop UI."
    )
    return tuple(confirmed_steps)


def _build_context_after_live_preflight(
    config_path: Path | None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
    settings_loader: Callable[[Path | None], Settings] = load_settings,
    context_builder: Callable[..., Any] = build_context,
) -> Any:
    """Load configuration and finish live setup gates before adapter construction."""
    settings = settings_loader(config_path)
    confirmed_steps = _run_live_setup_preflight(settings, input_fn=input_fn, output_fn=output_fn)
    return context_builder(config_path, manual_confirmations=confirmed_steps)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review and approve one CAD harness job")
    parser.add_argument("job_id", help="Explicit harness job identifier to review")
    parser.add_argument("--engineer", default=getpass.getuser(), help="Engineer identity")
    parser.add_argument("--config", type=Path, default=None, help="Harness YAML configuration")
    parser.add_argument("--pilot-case-id", help="Attach click-marked effort to this pilot case")
    parser.add_argument(
        "--register-pilot-baseline",
        action="store_true",
        help="Persist the measured baseline before opening the pilot case",
    )
    parser.add_argument("--pilot-group", choices=("B", "D", "E"))
    parser.add_argument("--pilot-work-label", choices=("ve_moi", "sua_ban_co_san"))
    parser.add_argument("--pilot-manual-minutes", type=float)
    parser.add_argument("--pilot-measured-by", help="Opaque engineer identifier")
    parser.add_argument("--pilot-biased", action="store_true")
    parser.add_argument("--pilot-manual-single-session", action="store_true")
    args = parser.parse_args()

    context = _build_context_after_live_preflight(args.config)
    if args.register_pilot_baseline:
        required = {
            "--pilot-case-id": args.pilot_case_id,
            "--pilot-group": args.pilot_group,
            "--pilot-work-label": args.pilot_work_label,
            "--pilot-manual-minutes": args.pilot_manual_minutes,
            "--pilot-measured-by": args.pilot_measured_by,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing or not args.pilot_manual_single_session:
            parser.error(
                "baseline registration requires "
                + ", ".join([*missing, "--pilot-manual-single-session"])
            )
        context.metrics_service.record_baseline(
            case_id=args.pilot_case_id,
            capability_group=args.pilot_group,
            work_label=args.pilot_work_label,
            raw_manual_minutes=args.pilot_manual_minutes,
            measured_by=args.pilot_measured_by,
            biased=args.pilot_biased,
            measured_in_single_session=True,
        )
    controller = EngineerDesktopController(
        context.service,
        metrics_service=context.metrics_service if args.pilot_case_id else None,
        pilot_case_id=args.pilot_case_id,
        pilot_job_id=args.job_id if args.pilot_case_id else None,
    )
    application = QApplication.instance() or QApplication([])
    window = create_approval_window(
        controller,
        job_id=args.job_id,
        engineer_id=args.engineer,
    )
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
