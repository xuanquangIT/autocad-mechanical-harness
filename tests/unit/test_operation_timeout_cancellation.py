"""Fast examples for cooperative application-operation timeouts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from time import monotonic

import pytest

from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.application.services.instrumented_measurement_service import (
    InstrumentedMeasurementService,
)
from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.application.services.takeoff_service import TakeoffService
from cad_harness.application.timeout import OperationDeadline, run_cancellable
from cad_harness.config import Settings
from cad_harness.domain.errors import IpcTimeoutError
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    DrawingSummary,
    ReadScope,
)
from cad_harness.domain.models.measurement import MeasurementKind, MeasurementRequest
from cad_harness.domain.models.takeoff import MaterialTable, TakeoffReport, TakeoffRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.recorder import OperationMetricsRecorder

TIMEOUT_SECONDS = 0.05
RETURN_UPPER_BOUND_SECONDS = 1.0
WORKER_RELEASE_WAIT_SECONDS = 2.0


@dataclass(slots=True)
class BlockingGate:
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    returned: Event = field(default_factory=Event)

    def block(self) -> None:
        self.entered.set()
        try:
            if not self.release.wait(WORKER_RELEASE_WAIT_SECONDS):
                raise AssertionError("test did not release the blocked worker")
        finally:
            self.returned.set()


class BlockingDrawingSource:
    def __init__(self, gate: BlockingGate) -> None:
        self._gate = gate

    def current_revision(self, document_id: str) -> str:
        raise AssertionError("service must use cooperative drawing source boundary")

    def current_revision_cancellable(self, document_id: str, deadline: OperationDeadline) -> str:
        deadline.add_cancel_callback(self._gate.release.set)
        self._gate.block()
        return "sha256:blocked-read"

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        raise AssertionError("cancelled read must stop before summary generation")

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        raise AssertionError("cancelled read must stop before geometry retrieval")


class BlockingMaterials:
    def __init__(self, gate: BlockingGate) -> None:
        self._gate = gate

    def load(self, profile_ref: str) -> MaterialTable:
        raise AssertionError("service must use cooperative material boundary")

    def load_cancellable(self, profile_ref: str, deadline: OperationDeadline) -> MaterialTable:
        deadline.add_cancel_callback(self._gate.release.set)
        self._gate.block()
        return MaterialTable(profile_id="blocked", version="1.0", entries=())


@dataclass(slots=True)
class RecordingStore:
    records: list[tuple[str, TakeoffReport, float]] = field(default_factory=list)

    def save_takeoff_report(
        self, *, report_id: str, report: TakeoffReport, total_mass_kg: float
    ) -> None:
        self.records.append((report_id, report, total_mass_kg))

    def persist_created(
        self,
        *,
        report_id: str,
        report: TakeoffReport,
        total_mass_kg: float,
        actor_id: str,
        deadline: OperationDeadline,
    ) -> str:
        deadline.checkpoint()
        self.records.append((report_id, report, total_mass_kg))
        return "audit-timeout"


class BlockingMeasurementService(MeasurementService):
    def __init__(self, gate: BlockingGate) -> None:
        self._gate = gate

    def measure(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
    ):
        raise AssertionError("wrapper must use cooperative measurement boundary")

    def measure_cancellable(
        self,
        model: DrawingModel,
        request: MeasurementRequest,
        *,
        tolerance: ToleranceProfile,
        deadline: OperationDeadline,
    ):
        deadline.add_cancel_callback(self._gate.release.set)
        self._gate.block()
        return super().measure(model, request, tolerance=tolerance)


class NullMetricStore:
    def record_operation(self, **values: object) -> None:
        pass


def _minimal_model() -> DrawingModel:
    return DrawingModel(
        document_id="doc-timeout",
        revision="sha256:timeout",
        display_name="timeout.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        arc_chord_tolerance_mm=0.01,
    )


def _run_blocked(
    operation: Callable[[], object], gate: BlockingGate
) -> tuple[IpcTimeoutError, float]:
    started_at = monotonic()
    try:
        with pytest.raises(IpcTimeoutError) as error:
            operation()
        elapsed = monotonic() - started_at
        assert gate.entered.is_set()
        assert gate.returned.is_set(), "operation returned while cooperative work was still running"
        return error.value, elapsed
    finally:
        gate.release.set()
        assert gate.returned.wait(WORKER_RELEASE_WAIT_SECONDS)


def _assert_configured_timeout(error: IpcTimeoutError, operation: str) -> None:
    assert error.details["operation"] == operation
    assert error.details["timeout_seconds"] == TIMEOUT_SECONDS
    assert error.details["cancelled"] is True


def test_run_cancellable_returns_near_deadline_and_blocks_late_side_effect() -> None:
    gate = BlockingGate()
    side_effect = Event()
    deadline = OperationDeadline(TIMEOUT_SECONDS, "sentinel")

    def late_worker(token: OperationDeadline) -> None:
        token.add_cancel_callback(gate.release.set)
        gate.block()
        token.checkpoint()
        side_effect.set()

    error, elapsed = _run_blocked(lambda: run_cancellable(deadline, late_worker), gate)

    _assert_configured_timeout(error, "sentinel")
    assert elapsed >= TIMEOUT_SECONDS * 0.8
    assert elapsed < RETURN_UPPER_BOUND_SECONDS
    assert not side_effect.is_set()


def test_drawing_read_service_uses_settings_timeout() -> None:
    gate = BlockingGate()
    settings = Settings.model_validate({"read": {"read_timeout_seconds": TIMEOUT_SECONDS}})
    service = DrawingReadService(settings, BlockingDrawingSource(gate))
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref="blocked.dxf"),
        scope=None,
        max_entities=10,
        max_block_nesting_depth=1,
    )

    error, elapsed = _run_blocked(lambda: service.read(request), gate)

    _assert_configured_timeout(error, "read")
    assert elapsed < RETURN_UPPER_BOUND_SECONDS


def test_takeoff_timeout_never_persists_or_audits() -> None:
    gate = BlockingGate()
    settings = Settings.model_validate({"takeoff": {"timeout_seconds": TIMEOUT_SECONDS}})
    store = RecordingStore()
    service = TakeoffService(
        settings,
        BlockingMaterials(gate),
        persistence=store,
    )
    request = TakeoffRequest(
        document_id="doc-timeout",
        parts=(),
        material_profile_ref="blocked@1.0",
    )

    error, elapsed = _run_blocked(
        lambda: service.create(
            _minimal_model(),
            request,
            tolerance=ToleranceProfile(id="timeout", version="1.0"),
        ),
        gate,
    )

    _assert_configured_timeout(error, "takeoff")
    assert elapsed < RETURN_UPPER_BOUND_SECONDS
    assert store.records == []


def test_instrumented_measurement_service_uses_settings_timeout() -> None:
    gate = BlockingGate()
    settings = Settings.model_validate({"measure": {"timeout_seconds": TIMEOUT_SECONDS}})
    service = InstrumentedMeasurementService(
        BlockingMeasurementService(gate),
        OperationMetricsRecorder(NullMetricStore()),
        settings,
    )
    request = MeasurementRequest(
        kind=MeasurementKind.POINT_TO_POINT,
        first_point_mm=(0.0, 0.0),
        second_point_mm=(3.0, 4.0),
    )

    error, elapsed = _run_blocked(
        lambda: service.measure(
            _minimal_model(),
            request,
            tolerance=ToleranceProfile(id="timeout", version="1.0"),
        ),
        gate,
    )

    _assert_configured_timeout(error, "measure")
    assert elapsed < RETURN_UPPER_BOUND_SECONDS
