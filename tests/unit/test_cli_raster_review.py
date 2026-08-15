from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from apps.cli import __main__ as cli

from cad_harness.application.services.raster_trace_service import RasterTraceService
from cad_harness.comprehension.raster_trace import LocalRasterTracer
from cad_harness.domain.errors import ApprovalRequiredError, ApprovalScopeMismatchError
from cad_harness.domain.models.raster import PixelPoint, RasterCalibration, RasterTraceReport


def _report(
    tmp_path: Path,
) -> tuple[LocalRasterTracer, RasterTraceService, RasterTraceReport, Path]:
    image = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    tracer = LocalRasterTracer(tmp_path / "raster")
    report = tracer.trace(
        encoded.tobytes(),
        display_name="line.png",
        calibration=RasterCalibration(
            pixel_a=PixelPoint(x=19.0, y=60.0),
            pixel_b=PixelPoint(x=181.0, y=60.0),
            reference_distance_mm=56.0,
        ),
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json(), encoding="utf-8")
    service = RasterTraceService(tracer, signing_secret="test-raster-secret")
    return tracer, service, report, report_path


def test_raster_review_resolves_exact_overlay_and_candidate_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer, service, report, report_path = _report(tmp_path)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "build_context",
        lambda _config: SimpleNamespace(raster_tracer=tracer, raster_trace_service=service),
    )
    monkeypatch.setattr(cli, "_emit", emitted.append)

    assert cli._cmd_raster_review(argparse.Namespace(config=None, report=report_path)) == 0

    payload = emitted[0]
    overlay_path = tracer.resolve_overlay_path(report)
    assert payload["status"] == "review_required"
    assert payload["overlay_path"] == str(overlay_path)
    assert payload["overlay_sha256"] == (
        f"sha256:{hashlib.sha256(overlay_path.read_bytes()).hexdigest()}"
    )
    assert payload["candidates"] == [
        {
            "candidate_id": report.candidates[0].candidate_id,
            "status": "proposed",
            "geometry_kind": "line",
            "confidence": report.candidates[0].confidence,
            "fit_error_px": report.candidates[0].fit_error_px,
        }
    ]


def test_raster_accept_requires_confirmation_and_exact_overlay_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer, service, report, report_path = _report(tmp_path)
    monkeypatch.setattr(
        cli,
        "build_context",
        lambda _config: SimpleNamespace(raster_tracer=tracer, raster_trace_service=service),
    )
    monkeypatch.setattr(cli, "_emit", lambda _payload: None)
    base = {
        "config": None,
        "report": report_path,
        "candidate": [report.candidates[0].candidate_id],
        "accepted_by": "engineer:test",
        "layer": "TRACE_REVIEWED",
    }
    with pytest.raises(ApprovalRequiredError):
        cli._cmd_raster_accept(
            argparse.Namespace(
                **base,
                confirm_reviewed_overlay=False,
                reviewed_overlay_sha256="sha256:" + "0" * 64,
            )
        )
    with pytest.raises(ApprovalScopeMismatchError):
        cli._cmd_raster_accept(
            argparse.Namespace(
                **base,
                confirm_reviewed_overlay=True,
                reviewed_overlay_sha256="sha256:" + "0" * 64,
            )
        )

    overlay = tracer.resolve_overlay_path(report)
    digest = f"sha256:{hashlib.sha256(overlay.read_bytes()).hexdigest()}"
    assert (
        cli._cmd_raster_accept(
            argparse.Namespace(
                **base,
                confirm_reviewed_overlay=True,
                reviewed_overlay_sha256=digest,
            )
        )
        == 0
    )
