from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from cad_harness.application.services.raster_trace_service import RasterTraceService
from cad_harness.comprehension.raster_trace import LocalRasterTracer
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
    ErrorCode,
    InvalidFeatureParametersError,
    MissingRequiredInputsError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.raster import PixelPoint, RasterCalibration, RasterTraceReport


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _calibration() -> RasterCalibration:
    return RasterCalibration(
        pixel_a=PixelPoint(x=20.0, y=60.0),
        pixel_b=PixelPoint(x=180.0, y=60.0),
        reference_distance_mm=160.0,
    )


def _payload() -> bytes:
    image = np.full((220, 220), 255, dtype=np.uint8)
    cv2.line(image, (20, 60), (180, 60), 0, 2, cv2.LINE_8)
    success, encoded = cv2.imencode(".png", image)
    assert success
    return encoded.tobytes()


def _service(tmp_path: Path, clock: _Clock | None = None) -> RasterTraceService:
    return RasterTraceService(
        LocalRasterTracer(tmp_path),
        signing_secret="local-raster-test-secret",
        acceptance_ttl=timedelta(minutes=5),
        clock=clock,
    )


def _trace(service: RasterTraceService) -> RasterTraceReport:
    return service.trace(_payload(), "source.png", _calibration())


def test_empty_secret_and_invalid_lifetime_fail_closed(tmp_path: Path) -> None:
    tracer = LocalRasterTracer(tmp_path)
    with pytest.raises(ApprovalRequiredError):
        RasterTraceService(tracer, signing_secret="   ")
    with pytest.raises(ApprovalRequiredError):
        RasterTraceService(
            tracer,
            signing_secret="secret",
            acceptance_ttl=timedelta(0),
        )
    with pytest.raises(ApprovalRequiredError):
        RasterTraceService(
            tracer,
            signing_secret="secret",
            acceptance_ttl=timedelta(minutes=16),
        )


def test_trace_maps_untrusted_input_errors_to_structured_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(InvalidFeatureParametersError):
        service.trace(_payload(), "private/source.png", _calibration())
    with pytest.raises(UnsupportedInputFormatError) as error:
        service.trace(b"not-an-image", "source.png", _calibration())
    assert error.value.code is ErrorCode.UNSUPPORTED_INPUT_FORMAT
    assert b"not-an-image" not in str(error.value).encode()


def test_uncalibrated_trace_is_inspectable_but_acceptance_requires_calibration(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    report = service.trace(_payload(), "source.png", None)
    with pytest.raises(MissingRequiredInputsError) as error:
        service.accept(
            report,
            (report.candidates[0].candidate_id,),
            "engineer@example.com",
            layer="TRACE_REVIEWED",
        )
    assert error.value.code is ErrorCode.MISSING_REQUIRED_INPUTS
    assert error.value.details == {"missing": ["calibration"]}


def test_signed_acceptance_returns_draft_operations_and_contains_no_image_data(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    report = _trace(service)
    candidate = next(item for item in report.candidates if item.status == "proposed")

    acceptance, token = service.accept(
        report,
        (candidate.candidate_id,),
        "engineer@example.com",
        layer="TRACE_REVIEWED",
    )
    operations = service.draft_operations(
        report,
        acceptance,
        token,
        layer="TRACE_REVIEWED",
    )

    assert len(operations) == 1
    assert operations[0].feature_id == candidate.candidate_id
    assert token.startswith("raster-v1.")
    assert "source.png" not in token
    assert _payload().hex()[:40] not in token


def test_acceptance_requires_identity_selection_and_exact_trace_scope(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = _trace(service)
    candidate_id = next(
        item.candidate_id for item in report.candidates if item.status == "proposed"
    )

    with pytest.raises(ApprovalRequiredError):
        service.accept(report, (), "engineer@example.com", layer="TRACE_REVIEWED")
    with pytest.raises(ApprovalRequiredError):
        service.accept(report, (candidate_id,), " ", layer="TRACE_REVIEWED")
    with pytest.raises(ApprovalScopeMismatchError):
        service.accept(
            report,
            ("raster-candidate-outside-trace",),
            "engineer@example.com",
            layer="TRACE_REVIEWED",
        )


def test_forged_token_or_changed_acceptance_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = _trace(service)
    candidate_id = next(
        item.candidate_id for item in report.candidates if item.status == "proposed"
    )
    acceptance, token = service.accept(
        report, (candidate_id,), "engineer@example.com", layer="TRACE_REVIEWED"
    )

    with pytest.raises(ApprovalScopeMismatchError, match="signature"):
        service.draft_operations(
            report,
            acceptance,
            token[:-1] + ("0" if token[-1] != "0" else "1"),
            layer="TRACE_REVIEWED",
        )
    changed_identity = acceptance.model_copy(update={"accepted_by": "another@example.com"})
    with pytest.raises(ApprovalScopeMismatchError, match="does not match"):
        service.draft_operations(report, changed_identity, token, layer="TRACE_REVIEWED")
    with pytest.raises(ApprovalScopeMismatchError, match="does not match"):
        service.draft_operations(report, acceptance, token, layer="UNREVIEWED_LAYER")
    changed_source = report.model_copy(
        update={"source": report.source.model_copy(update={"source_sha256": "sha256:" + "0" * 64})}
    )
    with pytest.raises(ApprovalScopeMismatchError, match="does not cover"):
        service.draft_operations(changed_source, acceptance, token, layer="TRACE_REVIEWED")


@pytest.mark.parametrize("layer", ["", "bad\nlayer", "X" * 257])
def test_draft_layer_is_required_and_bounded(tmp_path: Path, layer: str) -> None:
    service = _service(tmp_path)
    report = _trace(service)
    candidate_id = next(
        item.candidate_id for item in report.candidates if item.status == "proposed"
    )
    with pytest.raises(InvalidFeatureParametersError):
        service.accept(report, (candidate_id,), "engineer@example.com", layer=layer)


def test_acceptance_expiry_uses_timezone_aware_clock(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    report = _trace(service)
    candidate_id = next(
        item.candidate_id for item in report.candidates if item.status == "proposed"
    )
    acceptance, token = service.accept(
        report, (candidate_id,), "engineer@example.com", layer="TRACE_REVIEWED"
    )

    clock.value += timedelta(minutes=5)
    with pytest.raises(ApprovalExpiredError) as error:
        service.draft_operations(report, acceptance, token, layer="TRACE_REVIEWED")
    assert error.value.code is ErrorCode.APPROVAL_EXPIRED


def test_naive_service_clock_fails_closed(tmp_path: Path) -> None:
    service = RasterTraceService(
        LocalRasterTracer(tmp_path),
        signing_secret="secret",
        clock=lambda: datetime(2026, 8, 9, 12, 0),
    )
    report = _trace(service)
    candidate_id = next(
        item.candidate_id for item in report.candidates if item.status == "proposed"
    )
    with pytest.raises(ApprovalRequiredError, match="timezone-aware"):
        service.accept(report, (candidate_id,), "engineer@example.com", layer="TRACE_REVIEWED")
