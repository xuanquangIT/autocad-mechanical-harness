"""Requirement 26 performance gates using configured, reproducible workloads."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter

import ezdxf
import pytest

from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.application.services.plan_compiler import PlanCompilerService
from cad_harness.application.services.takeoff_service import TakeoffService
from cad_harness.company_rules.loader import load_profile
from cad_harness.company_rules.material_loader import YamlMaterialTableLoader
from cad_harness.config import Settings
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.drawing_model import (
    DrawingModel,
    EntityRecord,
    LineGeometry,
    PolylineGeometry,
    PolylineVertex,
    ReadScope,
)
from cad_harness.domain.models.drawing_spec import DrawingSpec
from cad_harness.domain.models.job import JobState
from cad_harness.domain.models.measurement import MeasurementKind, MeasurementRequest
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.takeoff import PartInput, TakeoffRequest
from cad_harness.domain.ports.autocad_adapter import CommitRequest, InspectRequest, RollbackRequest
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.collector import PerformanceThresholds, load_pilot_thresholds

_ROOT = Path(__file__).resolve().parents[2]
_ENTITY_LIMIT = 20_000
_FEATURE_LIMIT = 50
_OPERATION_LIMIT = 500

pytestmark = pytest.mark.slow


@dataclass(frozen=True, slots=True)
class _Timing:
    median_seconds: float
    p95_seconds: float
    samples: tuple[float, ...]


def _benchmark(operation: Callable[[], object], *, sample_count: int) -> _Timing:
    """Warm once, then calculate a nearest-rank p95 from independent calls."""
    operation()
    samples: list[float] = []
    for _ in range(sample_count):
        started = perf_counter()
        operation()
        samples.append(perf_counter() - started)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return _Timing(median(samples), ordered[p95_index], tuple(samples))


def _assert_gate(
    name: str,
    timing: _Timing,
    budget_seconds: float,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    prefix = f"requirement_26.{name}"
    record_testsuite_property(f"{prefix}.median_seconds", timing.median_seconds)
    record_testsuite_property(f"{prefix}.p95_seconds", timing.p95_seconds)
    record_testsuite_property(f"{prefix}.configured_budget_seconds", budget_seconds)
    record_testsuite_property(f"{prefix}.samples_seconds", list(timing.samples))
    assert timing.p95_seconds <= budget_seconds, (
        f"p95 {timing.p95_seconds:.6f}s exceeded configured budget {budget_seconds:.6f}s; "
        f"median={timing.median_seconds:.6f}s samples={timing.samples!r}"
    )


@pytest.fixture(scope="module")
def thresholds() -> PerformanceThresholds:
    return load_pilot_thresholds(_ROOT / "config/pilot.yaml").performance


@pytest.fixture(scope="module")
def sample_count() -> int:
    return load_pilot_thresholds(_ROOT / "config/pilot.yaml").minimum_metric_samples


@pytest.fixture(scope="module")
def fifty_feature_spec() -> DrawingSpec:
    return DrawingSpec.model_validate(
        {
            "spec_id": "spec-performance-compile",
            "document_id": "doc-performance",
            "units": "mm",
            "standard_profile": {"profile_id": "demo-profile", "version": "1.0"},
            "drawing": {
                "projection": "orthographic",
                "view": "top",
                "datum": {"type": "point", "point_mm": [0.0, 0.0]},
            },
            "features": [
                {
                    "feature_id": f"plate-{index:02d}",
                    "type": "rectangular_plate",
                    "parameters": {
                        "width_mm": 100.0,
                        "height_mm": 60.0,
                        "thickness_mm": 10.0,
                        "material": "SS400",
                        "origin_mm": [float(index * 120), 0.0],
                    },
                }
                for index in range(_FEATURE_LIMIT)
            ],
            "annotations": {"dimensions": "none"},
        }
    )


@pytest.fixture(scope="module")
def compiled_plan(fifty_feature_spec: DrawingSpec) -> OperationPlan:
    profile = load_profile("demo-profile@1.0")
    result = PlanCompilerService(
        profile,
        profile.tolerance(),
        FakeAutoCADAdapter(),
    ).compile(
        fifty_feature_spec,
        job_id="job-performance",
        expected_revision="sha256:performance",
    )
    assert result.plan is not None, result.missing_inputs
    assert len(result.plan.operations) <= _OPERATION_LIMIT
    return result.plan


@pytest.fixture(scope="module")
def large_model() -> DrawingModel:
    outline = EntityRecord(
        entity_ref="outline",
        entity_type="AcDbPolyline",
        layer="OBJECT",
        visible=True,
        space="model",
        geometry=PolylineGeometry(
            vertices=tuple(
                PolylineVertex(point_mm=point)
                for point in ((0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0))
            ),
            closed=True,
        ),
        bounding_box_mm=(0.0, 0.0, 100.0, 50.0),
    )
    lines = tuple(
        EntityRecord(
            entity_ref=f"line-{index}",
            entity_type="AcDbLine",
            layer="AUX",
            visible=True,
            space="model",
            geometry=LineGeometry(
                start_mm=(float(index), 100.0),
                end_mm=(float(index + 1), 100.0),
            ),
            bounding_box_mm=(float(index), 100.0, float(index + 1), 100.0),
        )
        for index in range(_ENTITY_LIMIT - 1)
    )
    return DrawingModel(
        document_id="doc-performance",
        revision="sha256:performance",
        display_name="performance.dxf",
        source_unit_code="mm",
        to_mm_factor=1.0,
        geometry_normalized=True,
        scope=ReadScope(),
        entities=(outline, *lines),
        arc_chord_tolerance_mm=0.01,
    )


@pytest.fixture(scope="module")
def large_dxf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("performance-dxf") / "20k-lines.dxf"
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    modelspace = document.modelspace()
    for index in range(_ENTITY_LIMIT):
        modelspace.add_line((float(index), 0.0), (float(index + 1), 1.0))
    document.saveas(target)
    return target


def test_compile_50_features_meets_configured_p95(
    fifty_feature_spec: DrawingSpec,
    thresholds: PerformanceThresholds,
    sample_count: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    profile = load_profile("demo-profile@1.0")
    compiler = PlanCompilerService(profile, profile.tolerance(), FakeAutoCADAdapter())
    timing = _benchmark(
        lambda: compiler.compile(
            fifty_feature_spec,
            job_id="job-performance",
            expected_revision="sha256:performance",
        ),
        sample_count=sample_count,
    )
    assert len(fifty_feature_spec.features) == _FEATURE_LIMIT
    _assert_gate(
        "compile",
        timing,
        thresholds.compile_p95_seconds,
        record_testsuite_property,
    )


def test_preview_under_500_operations_meets_configured_p95(
    tmp_path: Path,
    compiled_plan: OperationPlan,
    thresholds: PerformanceThresholds,
    sample_count: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    settings = Settings.model_validate(
        {"storage": {"preview_directory": str(tmp_path / "previews")}}
    )
    service = HarnessService(settings, FakeAutoCADAdapter())
    job = service.create_job()
    plan = compiled_plan.model_copy(
        update={
            "job_id": job.job_id,
            "document_id": job.document_id,
            "expected_revision": job.expected_revision,
            "operations": tuple(
                Operation(
                    operation_id=f"op-preview-{index}",
                    feature_id=f"feature-preview-{index}",
                    type=OperationType.CREATE_LINE,
                    layer="OBJECT",
                    geometry={
                        "start_mm": [float(index), 0.0],
                        "end_mm": [float(index), 1.0],
                    },
                )
                for index in range(_OPERATION_LIMIT)
            ),
            "validation_expectations": (),
        }
    ).with_hash()
    service.store.save_plan(plan)
    accepted = job.transition_to(JobState.SPEC_ACCEPTED)
    service.store.save_job(
        accepted.transition_to(
            JobState.PLANNED,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
        )
    )
    timing = _benchmark(lambda: service.preview(plan.job_id), sample_count=sample_count)
    assert len(plan.operations) == _OPERATION_LIMIT
    _assert_gate(
        "preview",
        timing,
        thresholds.preview_p95_seconds,
        record_testsuite_property,
    )


def test_dxf_read_20k_entities_meets_configured_p95(
    large_dxf: Path,
    thresholds: PerformanceThresholds,
    sample_count: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    settings = Settings.model_validate(
        {
            "read": {
                "max_entities": _ENTITY_LIMIT,
                "read_timeout_seconds": thresholds.read_p95_seconds,
            }
        }
    )
    service = DrawingReadService(settings, DxfDrawingReader())
    request = DrawingReadRequest(
        source=DrawingSourceRef(kind="file", format="dxf", ref=str(large_dxf)),
        scope=ReadScope(),
        max_entities=_ENTITY_LIMIT,
        max_block_nesting_depth=settings.read.max_block_nesting_depth,
    )
    latest: DrawingModel | None = None

    def read() -> object:
        nonlocal latest
        result = service.read(request)
        assert isinstance(result, DrawingModel)
        latest = result
        return result

    timing = _benchmark(read, sample_count=sample_count)
    assert latest is not None
    assert len(latest.entities) == _ENTITY_LIMIT
    _assert_gate("read", timing, thresholds.read_p95_seconds, record_testsuite_property)


def test_takeoff_20k_entities_meets_configured_p95(
    large_model: DrawingModel,
    thresholds: PerformanceThresholds,
    sample_count: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    settings = Settings.model_validate(
        {"takeoff": {"timeout_seconds": thresholds.takeoff_p95_seconds}}
    )
    service = TakeoffService(settings, YamlMaterialTableLoader())
    request = TakeoffRequest(
        document_id=large_model.document_id,
        parts=(
            PartInput(
                part_code="P-PERFORMANCE",
                outline_entity_ref="outline",
                thickness_mm=10.0,
                material_code="SS400",
                quantity=1,
            ),
        ),
        material_profile_ref="demo-materials@1.0",
    )
    tolerance = ToleranceProfile(id="performance", version="1.0")
    timing = _benchmark(
        lambda: service.create(large_model, request, tolerance=tolerance),
        sample_count=sample_count,
    )
    assert len(large_model.entities) == _ENTITY_LIMIT
    _assert_gate(
        "takeoff",
        timing,
        thresholds.takeoff_p95_seconds,
        record_testsuite_property,
    )


def test_measure_20k_entities_meets_configured_p95(
    large_model: DrawingModel,
    thresholds: PerformanceThresholds,
    sample_count: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    request = MeasurementRequest(
        kind=MeasurementKind.BOUNDING_BOX,
        entity_refs=tuple(entity.entity_ref for entity in large_model.entities),
    )
    service = MeasurementService()
    tolerance = ToleranceProfile(id="performance", version="1.0")
    timing = _benchmark(
        lambda: service.measure(large_model, request, tolerance=tolerance),
        sample_count=sample_count,
    )
    _assert_gate(
        "measure",
        timing,
        thresholds.measure_p95_seconds,
        record_testsuite_property,
    )


def _live_bridge() -> DotNetBridgeAdapter:
    if os.environ.get("CAD_HARNESS_RUN_LIVE_PERFORMANCE") != "1":
        pytest.skip(
            "live AutoCAD benchmark disabled; open a scratch drawing, load the bridge bundle, "
            "then set CAD_HARNESS_RUN_LIVE_PERFORMANCE=1"
        )
    adapter = DotNetBridgeAdapter()
    try:
        adapter.handshake()
        adapter.inspect_document(InspectRequest())
    except HarnessError as exc:
        pytest.skip(f"live AutoCAD bridge unavailable: {exc.code.value}")
    return adapter


def _live_plan(document_id: str, revision: str, operation_count: int) -> OperationPlan:
    return OperationPlan(
        plan_id=f"plan-live-performance-{operation_count}",
        job_id=f"job-live-performance-{operation_count}",
        document_id=document_id,
        expected_revision=revision,
        profile_ref="live-performance@1",
        operations=tuple(
            Operation(
                operation_id=f"op-live-{index}",
                feature_id=f"feature-live-{index}",
                type=OperationType.CREATE_LINE,
                layer="0",
                geometry={
                    "start_mm": [float(index), 0.0],
                    "end_mm": [float(index), 1.0],
                },
            )
            for index in range(operation_count)
        ),
    ).with_hash()


def _timed_live_commit(operation_count: int) -> float:
    adapter = _live_bridge()
    snapshot = adapter.inspect_document(InspectRequest())
    plan = _live_plan(snapshot.document_id, snapshot.revision, operation_count)
    started = perf_counter()
    result = adapter.commit(
        CommitRequest(
            plan=plan,
            idempotency_key=f"live-performance-{operation_count}-{snapshot.revision}",
            expected_revision=snapshot.revision,
            approval_token="explicit-live-performance-opt-in",
        )
    )
    elapsed = perf_counter() - started
    adapter.rollback(
        RollbackRequest(
            job_id=plan.job_id,
            document_id=plan.document_id,
            checkpoint_id=result.checkpoint_id,
            current_revision=result.new_revision,
            rollback_approval_token="explicit-live-performance-opt-in",
            undo_group=result.undo_group,
        )
    )
    return elapsed


@pytest.mark.integration
@pytest.mark.slow
def test_live_bridge_commit_500_operations_meets_configured_p95(
    thresholds: PerformanceThresholds,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    elapsed = _timed_live_commit(_OPERATION_LIMIT)
    _assert_gate(
        "commit",
        _Timing(elapsed, elapsed, (elapsed,)),
        thresholds.commit_p95_seconds,
        record_testsuite_property,
    )


@pytest.mark.integration
@pytest.mark.slow
def test_live_autocad_command_context_block_time_is_bounded(
    thresholds: PerformanceThresholds,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    # A one-operation commit is one command-context slice. Its wall time is a
    # conservative upper bound for time spent blocking AutoCAD's command loop.
    elapsed = _timed_live_commit(1)
    _assert_gate(
        "autocad_command_block",
        _Timing(elapsed, elapsed, (elapsed,)),
        thresholds.max_autocad_command_block_seconds,
        record_testsuite_property,
    )
