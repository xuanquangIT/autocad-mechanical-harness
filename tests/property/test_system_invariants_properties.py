"""Properties 72-76 and 78: production system invariants."""

from __future__ import annotations

import builtins
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters import build_adapter
from cad_harness.adapters.dotnet_bridge import DEFAULT_PIPE_NAME, decode_frame, encode_frame
from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.adapters.named_pipe_transport import (
    NamedPipeDeadlineExceededError,
    NamedPipeTransport,
)
from cad_harness.application.manual_gate import (
    MANUAL_STEP_INSTRUCTIONS,
    ManualGate,
    ManualStep,
    ManualStepId,
)
from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.instrumented_measurement_service import (
    InstrumentedMeasurementService,
)
from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.application.services.takeoff_service import TakeoffService
from cad_harness.application.timeout import OperationDeadline
from cad_harness.compatibility import load_compatibility_matrix
from cad_harness.config import Settings
from cad_harness.domain.errors import (
    ApprovalRequiredError,
    HarnessError,
    IdempotencyKeyReusedError,
    IpcTimeoutError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_model import DrawingModel, DrawingSummary, ReadScope
from cad_harness.domain.models.measurement import (
    MeasurementKind,
    MeasurementRequest,
    MeasurementResult,
)
from cad_harness.domain.models.takeoff import MaterialTable, TakeoffReport, TakeoffRequest
from cad_harness.domain.models.validation import (
    Finding,
    Severity,
    ValidationReport,
    ValidationStage,
)
from cad_harness.domain.ports.autocad_adapter import AdapterStatus, InspectRequest, SelectionRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.recorder import OperationMetricsRecorder
from cad_harness.security.local_only import (
    OutboundNetworkBlockedError,
    install_local_only_network_guard,
    uninstall_local_only_network_guard,
)

_SPEC = {
    "units": "mm",
    "drawing": {
        "projection": "orthographic",
        "view": "top",
        "datum": {"type": "point", "point_mm": [0.0, 0.0]},
    },
    "features": [
        {
            "feature_id": "property-plate",
            "type": "rectangular_plate",
            "parameters": {
                "width_mm": 120.0,
                "height_mm": 80.0,
                "thickness_mm": 10.0,
                "material": "SS400",
                "origin_mm": [0.0, 0.0],
            },
        }
    ],
    "annotations": {"general_tolerance": "ISO 2768-m", "dimensions": "auto_required"},
}


def _approved_service() -> tuple[HarnessService, FakeAutoCADAdapter, str, str, str, str]:
    os.environ["CAD_HARNESS_APPROVAL_SECRET"] = "property-system-invariants"
    adapter = FakeAutoCADAdapter()
    service = HarnessService(Settings(), adapter)
    job = service.create_job()
    submitted = service.submit_spec(job.job_id, _SPEC)
    plan_hash = str(submitted["plan_hash"])
    service.preview(job.job_id)
    report = service.validate(job.job_id, ValidationStage.PRE_COMMIT)
    warnings = tuple(
        finding.rule_id for finding in report.findings if finding.severity is Severity.WARNING
    )
    _, token = service.approve(job.job_id, "property-engineer", warnings)
    return service, adapter, job.job_id, job.expected_revision, plan_hash, token


@dataclass(slots=True)
class _VirtualClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(slots=True)
class _BoundaryState:
    terminal: bool = False
    side_effects: list[str] = field(default_factory=list)
    persistence: list[tuple[str, str]] = field(default_factory=list)


class _BoundaryDrawingSource:
    def __init__(self, clock: _VirtualClock, duration: float, state: _BoundaryState) -> None:
        self._clock = clock
        self._duration = duration
        self._state = state
        self._advanced = False

    def current_revision(self, document_id: str) -> str:
        raise AssertionError("production service must use the cancellable read boundary")

    def current_revision_cancellable(self, document_id: str, deadline: OperationDeadline) -> str:
        if not self._advanced:
            self._advanced = True
            try:
                self._clock.advance(self._duration)
                deadline.checkpoint()
                self._state.side_effects.append("read-result-published")
            finally:
                self._state.terminal = True
        return "sha256:property-timeout"

    def summarize(self, request: DrawingReadRequest) -> DrawingSummary:
        raise AssertionError("production service must use the cancellable read boundary")

    def summarize_cancellable(
        self, request: DrawingReadRequest, deadline: OperationDeadline
    ) -> DrawingSummary:
        deadline.checkpoint()
        return DrawingSummary(
            document_id="property-timeout.dxf",
            revision="sha256:property-timeout",
            counts_by_entity_type={},
            counts_by_layer={},
            counts_by_space={},
        )

    def read(self, request: DrawingReadRequest) -> DrawingModel:
        raise AssertionError("summary-only request must not load geometry")

    def read_cancellable(
        self, request: DrawingReadRequest, deadline: OperationDeadline
    ) -> DrawingModel:
        raise AssertionError("summary-only request must not load geometry")


class _BoundaryMaterials:
    def load_cancellable(self, profile_ref: str, deadline: OperationDeadline) -> MaterialTable:
        deadline.checkpoint()
        return MaterialTable(profile_id="property", version="1.0", entries=())


class _AtomicTakeoffPersistence:
    def __init__(self, state: _BoundaryState) -> None:
        self._state = state

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
        # One append represents the combined report + mandatory audit transaction.
        self._state.persistence.append((report_id, actor_id))
        return "audit-property-timeout"


class _NullMetricStore:
    def record_operation(self, **values: object) -> None:
        pass


class _BoundaryPipeDriver:
    def __init__(self, clock: _VirtualClock, duration: float, state: _BoundaryState) -> None:
        self._clock = clock
        self._duration = duration
        self._state = state
        self._connect_count = 0
        self._advanced = False
        self._buffers: dict[str, bytearray] = {}

    def connect(
        self,
        pipe_name: str,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> object:
        assert pipe_name == DEFAULT_PIPE_NAME
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        self._connect_count += 1
        return f"pipe-{self._connect_count}"

    def write(
        self,
        handle: object,
        data: bytes,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        payload = decode_frame(data)
        if str(handle) == "pipe-1":
            response = {
                "schema_version": SCHEMA_VERSION,
                "request_id": payload["request_id"],
                "status": "ok",
                "data": {"revision": "sha256:property-timeout"},
            }
        else:
            response = {
                "schema_version": SCHEMA_VERSION,
                "request_id": payload["request_id"],
                "status": "ok",
                "data": {
                    "terminal": True,
                    "cancelled_request_id": payload["params"]["target_request_id"],
                },
            }
        self._buffers[str(handle)] = bytearray(encode_frame(response))

    def read_exact(
        self,
        handle: object,
        size: int,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> bytes:
        name = str(handle)
        if name == "pipe-1" and not self._advanced:
            self._advanced = True
            self._clock.advance(self._duration)
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        buffer = self._buffers[name]
        result = bytes(buffer[:size])
        del buffer[:size]
        if len(result) != size:
            raise EOFError("property driver response ended early")
        if name == "pipe-1" and not buffer:
            self._state.terminal = True
            self._state.side_effects.append("ipc-response-published")
        return result

    def cancel_pending(self, handle: object) -> None:
        pass

    def wait_pending_terminal(
        self,
        handle: object,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        if clock() > deadline:
            raise NamedPipeDeadlineExceededError
        self._state.terminal = True

    def close(self, handle: object) -> None:
        pass


def _minimal_timeout_model() -> DrawingModel:
    return DrawingModel(
        document_id="property-timeout.dxf",
        revision="sha256:property-timeout",
        display_name="property-timeout.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        arc_chord_tolerance_mm=0.01,
    )


def _deadline_factory(clock: _VirtualClock) -> Callable[[float, str], OperationDeadline]:
    return lambda timeout, operation: OperationDeadline(timeout, operation, clock=clock)


def _exercise_production_timeout_boundary(
    operation: str, timeout_seconds: float, duration_seconds: float
) -> tuple[object | None, IpcTimeoutError | None, _BoundaryState]:
    clock = _VirtualClock()
    state = _BoundaryState()
    deadline_factory = _deadline_factory(clock)
    result: object | None = None
    error: IpcTimeoutError | None = None
    try:
        if operation == "read":
            settings_value = Settings.model_validate(
                {"read": {"read_timeout_seconds": timeout_seconds}}
            )
            source = _BoundaryDrawingSource(clock, duration_seconds, state)
            service = DrawingReadService(settings_value, source)
            request = DrawingReadRequest(
                source=DrawingSourceRef(kind="file", format="dxf", ref="property-timeout.dxf"),
                scope=None,
                max_entities=1,
                max_block_nesting_depth=1,
            )
            with patch(
                "cad_harness.application.services.drawing_read_service.OperationDeadline",
                side_effect=deadline_factory,
            ):
                result = service.read(request)
        elif operation == "takeoff":
            settings_value = Settings.model_validate(
                {"takeoff": {"timeout_seconds": timeout_seconds}}
            )
            report = TakeoffReport(
                document_id="property-timeout.dxf",
                revision="sha256:property-timeout",
                profile_id="property-timeout",
                material_profile_id="property",
                material_profile_version="1.0",
                company_approved=False,
                units={"length": "mm", "mass": "kg"},
            )

            def process_runner(
                deadline: OperationDeadline, command: object, payload: object
            ) -> dict[str, Any]:
                try:
                    clock.advance(duration_seconds)
                    deadline.checkpoint()
                    state.side_effects.append("takeoff-worker-result-published")
                    return {"report": report.model_dump(mode="json")}
                finally:
                    state.terminal = True

            service = TakeoffService(
                settings_value,
                _BoundaryMaterials(),
                persistence=_AtomicTakeoffPersistence(state),
            )
            request = TakeoffRequest(
                document_id="property-timeout.dxf",
                parts=(),
                material_profile_ref="property@1.0",
            )
            with (
                patch(
                    "cad_harness.application.services.takeoff_service.OperationDeadline",
                    side_effect=deadline_factory,
                ),
                patch(
                    "cad_harness.application.services.takeoff_service.run_process_worker",
                    side_effect=process_runner,
                ),
            ):
                result = service.create(
                    _minimal_timeout_model(),
                    request,
                    tolerance=ToleranceProfile(id="property", version="1.0"),
                )
        elif operation == "measure":
            settings_value = Settings.model_validate(
                {"measure": {"timeout_seconds": timeout_seconds}}
            )
            measurement = MeasurementResult(
                kind=MeasurementKind.POINT_TO_POINT,
                value=5.0,
                unit="mm",
                tolerance_used=0.001,
                document_id="property-timeout.dxf",
                revision="sha256:property-timeout",
                measurement_basis=("explicit_point", "explicit_point"),
            )

            def process_runner(
                deadline: OperationDeadline, command: object, payload: object
            ) -> dict[str, Any]:
                try:
                    clock.advance(duration_seconds)
                    deadline.checkpoint()
                    state.side_effects.append("measurement-worker-result-published")
                    return {"measurement": measurement.model_dump(mode="json")}
                finally:
                    state.terminal = True

            service = InstrumentedMeasurementService(
                MeasurementService(),
                OperationMetricsRecorder(_NullMetricStore()),
                settings_value,
            )
            request = MeasurementRequest(
                kind=MeasurementKind.POINT_TO_POINT,
                first_point_mm=(0.0, 0.0),
                second_point_mm=(3.0, 4.0),
            )
            with (
                patch(
                    "cad_harness.application.services.instrumented_measurement_service.OperationDeadline",
                    side_effect=deadline_factory,
                ),
                patch(
                    "cad_harness.application.services.measurement_service.run_process_worker",
                    side_effect=process_runner,
                ),
            ):
                result = service.measure(
                    _minimal_timeout_model(),
                    request,
                    tolerance=ToleranceProfile(id="property", version="1.0"),
                )
        else:
            driver = _BoundaryPipeDriver(clock, duration_seconds, state)
            transport = NamedPipeTransport(
                DEFAULT_PIPE_NAME,
                driver=driver,
                clock=clock,
                request_id_factory=lambda: "property-cancel-request",
                sleeper=clock.advance,
            )
            result = transport.request(
                {
                    "schema_version": SCHEMA_VERSION,
                    "method": "inspect_document",
                    "request_id": "property-target-request",
                    "job_id": None,
                    "idempotency_key": None,
                    "params": {"document_id": "property-timeout.dxf"},
                },
                timeout_seconds=timeout_seconds,
            )
    except IpcTimeoutError as caught:
        error = caught
    return result, error, state


# Feature: cad-ai-production-roadmap, Property 72: manual gate never auto-advances
@given(
    step_id=st.sampled_from(tuple(ManualStepId)),
    confirmation=st.sampled_from(("none", "wrong", "correct")),
)
@settings(max_examples=100)
def test_manual_gate_runs_next_action_only_after_exact_confirmation(
    step_id: ManualStepId, confirmation: str
) -> None:
    """**Validates: Requirements 25.5**"""
    step = ManualStep(step_id, MANUAL_STEP_INSTRUCTIONS[step_id])
    gate = ManualGate((step,))
    calls: list[ManualStepId] = []

    assert step.instruction in gate.notification()
    if confirmation == "correct":
        gate.confirm(step_id)
        assert gate.run_next(lambda: calls.append(step_id)) is None
        assert calls == [step_id]
        assert gate.complete
        return
    if confirmation == "wrong":
        wrong = tuple(ManualStepId)[(tuple(ManualStepId).index(step_id) + 1) % len(ManualStepId)]
        with pytest.raises(ApprovalRequiredError):
            gate.confirm(wrong)
    with pytest.raises(ApprovalRequiredError) as error:
        gate.run_next(lambda: calls.append(step_id))
    assert calls == []
    assert step.instruction in str(error.value.required_action)


# Feature: cad-ai-production-roadmap, Property 73: stale/blocking always blocks commit
@given(stale=st.booleans(), blocking=st.booleans())
@settings(max_examples=100, deadline=None)
def test_stale_revision_or_latest_blocking_finding_never_reaches_adapter(
    stale: bool, blocking: bool
) -> None:
    """**Validates: Requirements 25.11**"""
    service, adapter, job_id, revision, plan_hash, token = _approved_service()
    report = service.store.get_validation(job_id)
    assert report is not None
    findings = report.findings
    if blocking:
        findings += (
            Finding(
                rule_id="PROPERTY-BLOCK",
                severity=Severity.BLOCKING,
                message="Injected after approval",
            ),
        )
    latest = ValidationReport.model_validate(
        {
            **report.model_dump(
                mode="python",
                exclude={
                    "findings",
                    "blocking_count",
                    "error_count",
                    "warning_count",
                    "info_count",
                },
            ),
            "validation_id": "validation-property-latest",
            "findings": findings,
        }
    )
    service.store.save_validation(latest)
    if stale:
        adapter.document.write_counter += 1
    entities_before = len(adapter.document.entities)
    writes_before = adapter.document.write_counter

    if stale or blocking:
        with pytest.raises(HarnessError):
            service.commit(
                job_id,
                idempotency_key="property-gate",
                expected_revision=revision,
                plan_hash=plan_hash,
                approval_token=token,
            )
        assert len(adapter.document.entities) == entities_before
        assert adapter.document.write_counter == writes_before
    else:
        result = service.commit(
            job_id,
            idempotency_key="property-gate",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        assert result.entity_results


# Feature: cad-ai-production-roadmap, Property 74: idempotency never duplicates entities
@given(retry_count=st.integers(min_value=1, max_value=10), reuse_with_change=st.booleans())
@settings(max_examples=100, deadline=None)
def test_same_idempotency_key_writes_once_and_changed_digest_is_rejected(
    retry_count: int, reuse_with_change: bool
) -> None:
    """**Validates: Requirements 25.12**"""
    service, adapter, job_id, revision, plan_hash, token = _approved_service()
    first = service.commit(
        job_id,
        idempotency_key="property-idempotency",
        expected_revision=revision,
        plan_hash=plan_hash,
        approval_token=token,
    )
    entity_refs = tuple(sorted(adapter.document.entities))
    write_counter = adapter.document.write_counter
    for _ in range(retry_count):
        replay = service.commit(
            job_id,
            idempotency_key="property-idempotency",
            expected_revision=revision,
            plan_hash=plan_hash,
            approval_token=token,
        )
        assert replay == first
    assert tuple(sorted(adapter.document.entities)) == entity_refs
    assert adapter.document.write_counter == write_counter

    if reuse_with_change:
        with pytest.raises(IdempotencyKeyReusedError):
            service.commit(
                job_id,
                idempotency_key="property-idempotency",
                expected_revision=revision,
                plan_hash="sha256:changed-request",
                approval_token=token,
            )
        assert tuple(sorted(adapter.document.entities)) == entity_refs
        assert adapter.document.write_counter == write_counter


# Feature: cad-ai-production-roadmap, Property 75: timeout cancels with exact code
@given(
    operation=st.sampled_from(("read", "takeoff", "measure", "ipc")),
    timeout_ticks=st.integers(min_value=1, max_value=60),
    offset=st.sampled_from((-1, 0, 1)),
)
@settings(max_examples=100, deadline=None)
def test_timeout_cancels_if_and_only_if_duration_exceeds_threshold(
    operation: str, timeout_ticks: int, offset: int
) -> None:
    """**Validates: Requirements 26.9**"""
    timeout_seconds = float(timeout_ticks)
    duration_seconds = float(max(0, timeout_ticks + offset))
    result, error, state = _exercise_production_timeout_boundary(
        operation, timeout_seconds, duration_seconds
    )

    assert state.terminal, "public operation returned before its worker/transport was terminal"
    if duration_seconds > timeout_seconds:
        assert result is None
        assert error is not None
        assert error.code.value == "IPC_TIMEOUT"
        assert error.details["operation"] == (
            "inspect_document" if operation == "ipc" else operation
        )
        assert error.details["timeout_seconds"] == timeout_seconds
        assert state.side_effects == []
        assert state.persistence == []
        if operation == "ipc":
            assert error.details["terminal_cancel_confirmed"] is True
        else:
            assert error.details["cancelled"] is True
        return

    assert error is None
    assert result is not None
    assert len(state.side_effects) == 1
    assert len(state.persistence) == (1 if operation == "takeoff" else 0)


# Feature: cad-ai-production-roadmap, Property 76: local/fake creates no external connection
@given(operation=st.sampled_from(("status", "inspect", "selection")))
@settings(max_examples=100)
def test_local_only_blocks_network_and_fake_never_loads_live_connectors(operation: str) -> None:
    """**Validates: Requirements 27.1, 28.5**"""
    try:
        install_local_only_network_guard()
        imported: list[str] = []
        real_import = builtins.__import__

        def tracking_import(name: str, *args: object, **kwargs: object):
            imported.append(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=tracking_import):
            adapter = build_adapter("fake")
        assert isinstance(adapter, FakeAutoCADAdapter)
        assert not any(name.endswith("autocad_com") or name == "win32com" for name in imported)

        if operation == "status":
            adapter.status()
        elif operation == "inspect":
            adapter.inspect_document(InspectRequest())
        else:
            adapter.inspect_selection(
                SelectionRequest(document_id=adapter.document.document_id, max_entities=10)
            )

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OutboundNetworkBlockedError):
            probe.connect(("203.0.113.1", 9))
        probe.close()
        with pytest.raises(OutboundNetworkBlockedError):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(OutboundNetworkBlockedError):
            socket.gethostbyaddr("8.8.8.8")
        with pytest.raises(OutboundNetworkBlockedError):
            socket.getnameinfo(("8.8.8.8", 53), 0)
        datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(OutboundNetworkBlockedError):
                datagram.sendto(b"blocked", ("203.0.113.1", 9))
            if hasattr(datagram, "sendmsg"):
                with pytest.raises(OutboundNetworkBlockedError):
                    datagram.sendmsg([b"blocked"], [], 0, ("203.0.113.1", 9))
        finally:
            datagram.close()
    finally:
        uninstall_local_only_network_guard()


# Feature: cad-ai-production-roadmap, Property 78: unsupported AutoCAD version blocks writer
@given(
    detected=st.one_of(
        st.sampled_from(
            (
                "24.3",
                "24.3s (LMS Tech)",
                "AutoCAD 25.0",
                "25.1.0.0",
                "26.0",
                "26.0s (LMS Tech)",
                "AutoCAD 26.0",
                "26.0.0.0",
                "x26.0x",
                "garbage 26.0 payload",
                "x24.3x",
                "garbage 24.3 payload",
                "24.3.999",
            )
        ),
        st.text(max_size=40),
        st.none(),
    )
)
@settings(max_examples=100)
def test_writer_compatibility_matches_only_published_version_prefixes(
    detected: str | None,
) -> None:
    """**Validates: Requirements 28.2**"""
    matrix = load_compatibility_matrix()
    # Independent reference grammar.  Do not call the production normalizer to
    # decide whether production is correct.
    # Independent policy oracle: spell out the reviewed release policy instead of
    # deriving expected writer support from the matrix under test.  Only TARGET
    # entries may write; provisional versions remain observable but fail closed.
    expected_policy = {
        "24.3": "provisional",
        "25.0": "target",
        "25.1": "provisional",
        "26.0": "target",
    }
    supported = {prefix for prefix, policy in expected_policy.items() if policy == "target"}
    expected_prefix: str | None = None
    if isinstance(detected, str):
        candidate = detected.strip()
        if candidate.lower().startswith("autocad "):
            candidate = candidate[8:]
        if " " in candidate:
            head, tail = candidate.split(" ", 1)
            if not (tail.startswith("(") and tail.endswith(")") and "(" not in tail[1:-1]):
                head = ""
        else:
            head = candidate
        if len(head) == 5 and head[-1:].isalpha():
            head = head[:-1]
        if (
            len(head) == 4
            and head[:2].isdigit()
            and head[2] == "."
            and head[3].isdigit()
            and head in supported
        ):
            expected_prefix = head
    expected = expected_prefix is not None
    status = AdapterStatus(
        adapter_type="com",
        available=True,
        cad_version=detected,
    )
    evaluated = matrix.evaluate_status(status)
    assert evaluated.version_supported == (expected if detected is not None else None)

    if expected:
        assert matrix.require_writer_compatible(status).com_version_prefix == expected_prefix
    else:
        with pytest.raises(HarnessError) as error:
            matrix.require_writer_compatible(status)
        assert error.value.details["detected_version"] == detected
        assert error.value.details["supported_versions"] == list(matrix.supported_versions)
