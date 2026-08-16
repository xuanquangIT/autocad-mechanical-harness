"""Validation rules. Each test constructs a plan that should trip exactly one rule."""

from __future__ import annotations

from typing import Any

import pytest

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.operation_plan import (
    Operation,
    OperationPlan,
    OperationType,
    ValidationExpectation,
)
from cad_harness.domain.models.result import CommitResult, CommitStatus, EntityResult
from cad_harness.domain.models.validation import Severity, ValidationStage
from cad_harness.domain.value_objects.units import Unit
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.validation.engine import RuleContext, default_engine


def build_plan(
    operations: tuple[Operation, ...],
    *,
    expectations: tuple[ValidationExpectation, ...] = (),
    profile_ref: str = "demo-profile@1.0",
    units: Unit = Unit.MM,
) -> OperationPlan:
    return OperationPlan(
        plan_id="plan_1",
        job_id="job_1",
        document_id="doc_1",
        expected_revision="sha256:rev",
        canonical_units=units,
        profile_ref=profile_ref,
        operations=operations,
        validation_expectations=expectations,
    ).with_hash()


def outline(vertices: list[list[float]], area: float = 16000.0, layer: str = "OBJECT") -> Operation:
    return Operation(
        operation_id="op-outline",
        feature_id="plate-1",
        type=OperationType.CREATE_CLOSED_POLYLINE,
        layer=layer,
        geometry={"vertices_mm": vertices},
        expected={"closed": True, "vertex_count": 4, "area_mm2": area},
    )


def holes(centers: list[list[float]], diameter: float = 14.0, layer: str = "OBJECT") -> Operation:
    return Operation(
        operation_id="op-holes",
        feature_id="plate-1-holes",
        type=OperationType.CREATE_CIRCLES,
        layer=layer,
        geometry={"centers_mm": centers, "diameter_mm": diameter},
        expected={"count": len(centers), "diameter_mm": diameter},
    )


PLATE = [[0.0, 0.0], [160.0, 0.0], [160.0, 100.0], [0.0, 100.0]]
PARENT_LINK = (
    ValidationExpectation(
        rule_id="GEO-HOLE-PATTERN-GRID",
        feature_id="plate-1-holes",
        operation_id="op-holes",
        expected={"count": 4, "parent_feature_id": "plate-1"},
    ),
)


@pytest.fixture
def context_factory(profile: CompanyProfile, tolerance: ToleranceProfile):
    def make(plan: OperationPlan, commit_result: Any = None) -> RuleContext:
        return RuleContext(
            plan=plan, profile=profile, tolerance=tolerance, commit_result=commit_result
        )

    return make


def run(context: RuleContext, stage: ValidationStage = ValidationStage.PRE_COMMIT):
    return default_engine().run(stage, context, job_id="job_1")


def rule_ids(report, severity: Severity | None = None) -> set[str]:
    return {f.rule_id for f in report.findings if severity is None or f.severity is severity}


class TestGeometryRules:
    def test_valid_plate_produces_no_geometry_findings(self, context_factory) -> None:
        plan = build_plan(
            (outline(PLATE), holes([[20, 20], [140, 20], [20, 80], [140, 80]])),
            expectations=PARENT_LINK,
        )
        report = run(context_factory(plan))
        assert report.blocking_count == 0
        assert report.error_count == 0

    def test_wrong_expected_area_is_reported(self, context_factory) -> None:
        plan = build_plan((outline(PLATE, area=99999.0),))
        report = run(context_factory(plan))
        assert "GEO-OUTLINE-CLOSED" in rule_ids(report, Severity.ERROR)

    def test_self_intersecting_outline_is_blocking(self, context_factory) -> None:
        bowtie = [[0.0, 0.0], [160.0, 100.0], [160.0, 0.0], [0.0, 100.0]]
        plan = build_plan((outline(bowtie),))
        assert "GEO-OUTLINE-CLOSED" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_hole_outside_boundary_is_blocking(self, context_factory) -> None:
        plan = build_plan((outline(PLATE), holes([[200.0, 50.0]])), expectations=PARENT_LINK)
        assert "GEO-HOLE-PLACEMENT" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_hole_crossing_the_edge_is_blocking(self, context_factory) -> None:
        """Centre is inside, but the hole's radius crosses the boundary."""
        plan = build_plan((outline(PLATE), holes([[3.0, 50.0]])), expectations=PARENT_LINK)
        assert "GEO-HOLE-PLACEMENT" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_hole_too_close_to_the_edge_is_an_error(self, context_factory) -> None:
        # Clearance 8.0 mm from edge, radius 7.0 -> 1.0 mm left, below the 1.5 mm minimum.
        plan = build_plan((outline(PLATE), holes([[8.0, 50.0]])), expectations=PARENT_LINK)
        assert "GEO-HOLE-PLACEMENT" in rule_ids(run(context_factory(plan)), Severity.ERROR)

    def test_overlapping_holes_are_blocking(self, context_factory) -> None:
        plan = build_plan(
            (outline(PLATE), holes([[50.0, 50.0], [55.0, 50.0]])), expectations=PARENT_LINK
        )
        assert "GEO-HOLE-LIGAMENT" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_insufficient_ligament_is_an_error(self, context_factory) -> None:
        # 15 mm centres, 14 mm diameter -> 1 mm ligament, below the 2 mm minimum.
        plan = build_plan(
            (outline(PLATE), holes([[50.0, 50.0], [65.0, 50.0]])), expectations=PARENT_LINK
        )
        assert "GEO-HOLE-LIGAMENT" in rule_ids(run(context_factory(plan)), Severity.ERROR)

    def test_pattern_count_mismatch_is_blocking(self, context_factory) -> None:
        plan = build_plan(
            (outline(PLATE), holes([[20.0, 20.0], [140.0, 20.0]])),
            expectations=(
                ValidationExpectation(
                    rule_id="GEO-HOLE-PATTERN-GRID",
                    feature_id="plate-1-holes",
                    operation_id="op-holes",
                    expected={"count": 4, "parent_feature_id": "plate-1"},
                ),
            ),
        )
        assert "GEO-PATTERN-INTEGRITY" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_hole_off_the_pitch_circle_is_an_error(self, context_factory) -> None:
        plan = build_plan(
            (holes([[60.0, 0.0], [0.0, 55.0]]),),
            expectations=(
                ValidationExpectation(
                    rule_id="GEO-HOLE-PATTERN-BOLT-CIRCLE",
                    feature_id="plate-1-holes",
                    operation_id="op-holes",
                    expected={"count": 2, "pcd_mm": 120.0, "center_mm": [0.0, 0.0]},
                ),
            ),
        )
        assert "GEO-PATTERN-INTEGRITY" in rule_ids(run(context_factory(plan)), Severity.ERROR)

    def test_pattern_without_a_parent_reports_info_not_a_false_pass(self, context_factory) -> None:
        plan = build_plan((holes([[20.0, 20.0]]),))
        report = run(context_factory(plan))
        assert "GEO-HOLE-PLACEMENT" in rule_ids(report, Severity.INFO)


class TestStandardRules:
    def test_undeclared_layer_is_an_error(self, context_factory) -> None:
        plan = build_plan((outline(PLATE, layer="RANDOM-LAYER"),))
        assert "STD-LAYER-DECLARED" in rule_ids(run(context_factory(plan)), Severity.ERROR)

    def test_layer_zero_fallback_is_an_error(self, context_factory) -> None:
        """A generic semantic operation on layer 0 is still an unmapped fallback."""
        plan = build_plan((outline(PLATE, layer="0"),))
        assert "STD-LAYER-DECLARED" in rule_ids(run(context_factory(plan)), Severity.ERROR)

    def test_explicit_hash_bound_declared_layer_zero_is_allowed(self, context_factory) -> None:
        operation = outline(PLATE, layer="0").model_copy(
            update={"expected": {**outline(PLATE).expected, "layer": "0"}}
        )
        plan = build_plan((operation,))
        assert "STD-LAYER-DECLARED" not in rule_ids(run(context_factory(plan)))

    def test_non_canonical_units_are_blocking(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),), units=Unit.INCH)
        assert "STD-UNITS-CANONICAL" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_profile_version_drift_is_blocking(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),), profile_ref="demo-profile@0.9")
        assert "STD-PROFILE-PROVENANCE" in rule_ids(run(context_factory(plan)), Severity.BLOCKING)

    def test_demo_profile_warns_about_provenance(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),))
        assert "STD-PROFILE-PROVENANCE" in rule_ids(run(context_factory(plan)), Severity.WARNING)


class TestPostCommitRules:
    def _commit_result(self, measurements: dict[str, Any]) -> CommitResult:
        return CommitResult(
            job_id="job_1",
            plan_hash="sha256:plan",
            status=CommitStatus.COMMITTED,
            entity_results=(
                EntityResult(
                    operation_id="op-outline",
                    feature_id="plate-1",
                    entity_ref="fake:handle:1",
                    entity_type="AcDbPolyline",
                    measurements=measurements,
                ),
            ),
            previous_revision="sha256:a",
            new_revision="sha256:b",
        )

    def test_matching_measurements_pass(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),))
        result = self._commit_result({"closed": True, "vertex_count": 4, "area_mm2": 16000.0})
        report = run(context_factory(plan, result), ValidationStage.POST_COMMIT)
        assert report.blocking_count == 0

    def test_area_mismatch_is_blocking(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),))
        result = self._commit_result({"closed": True, "vertex_count": 4, "area_mm2": 15000.0})
        report = run(context_factory(plan, result), ValidationStage.POST_COMMIT)
        assert "POST-MEASUREMENT-MATCH" in rule_ids(report, Severity.BLOCKING)

    def test_missing_measurement_is_a_warning_not_a_pass(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),))
        result = self._commit_result({"closed": True})
        report = run(context_factory(plan, result), ValidationStage.POST_COMMIT)
        assert "POST-MEASUREMENT-MATCH" in rule_ids(report, Severity.WARNING)

    def test_missing_raster_measurement_is_blocking(self, context_factory) -> None:
        raster = outline(PLATE).model_copy(update={"feature_id": "raster-candidate-reviewed-line"})
        plan = build_plan((raster,))
        result = CommitResult(
            job_id="job_1",
            plan_hash="sha256:plan",
            status=CommitStatus.COMMITTED,
            entity_results=(
                EntityResult(
                    operation_id="op-outline",
                    feature_id="raster-candidate-reviewed-line",
                    entity_ref="fake:handle:1",
                    entity_type="AcDbPolyline",
                    measurements={"closed": True},
                ),
            ),
            previous_revision="sha256:a",
            new_revision="sha256:b",
        )

        report = run(context_factory(plan, result), ValidationStage.POST_COMMIT)

        assert "POST-MEASUREMENT-MATCH" in rule_ids(report, Severity.BLOCKING)

    def test_explicit_layer_mismatch_or_missing_readback_is_blocking(self, context_factory) -> None:
        operation = outline(PLATE).model_copy(
            update={"expected": {**outline(PLATE).expected, "layer": "0"}}
        )
        plan = build_plan((operation,))

        wrong = run(
            context_factory(
                plan,
                self._commit_result(
                    {
                        "closed": True,
                        "vertex_count": 4,
                        "area_mm2": 16000.0,
                        "layer": "OBJECT",
                    }
                ),
            ),
            ValidationStage.POST_COMMIT,
        )
        missing = run(
            context_factory(
                plan,
                self._commit_result({"closed": True, "vertex_count": 4, "area_mm2": 16000.0}),
            ),
            ValidationStage.POST_COMMIT,
        )

        assert "POST-MEASUREMENT-MATCH" in rule_ids(wrong, Severity.BLOCKING)
        assert "POST-MEASUREMENT-MATCH" in rule_ids(missing, Severity.BLOCKING)

    def test_point_measurements_use_length_tolerance(self, context_factory) -> None:
        operation = Operation(
            operation_id="op-outline",
            feature_id="reference-circle",
            type=OperationType.CREATE_CIRCLE,
            layer="OBJECT",
            geometry={"center_mm": [10.0, 20.0], "diameter_mm": 40.0},
            expected={"center_mm": [10.0, 20.0]},
        )
        plan = build_plan((operation,))
        inside = self._commit_result({"center_mm": [10.0005, 20.0]})
        outside = self._commit_result({"center_mm": [10.01, 20.0]})

        inside_report = run(context_factory(plan, inside), ValidationStage.POST_COMMIT)
        outside_report = run(context_factory(plan, outside), ValidationStage.POST_COMMIT)

        assert "POST-MEASUREMENT-MATCH" not in rule_ids(inside_report, Severity.BLOCKING)
        assert "POST-MEASUREMENT-MATCH" in rule_ids(outside_report, Severity.BLOCKING)

    def test_operation_without_an_entity_is_blocking(self, context_factory) -> None:
        plan = build_plan((outline(PLATE), holes([[20.0, 20.0]])), expectations=PARENT_LINK)
        result = self._commit_result({"closed": True, "vertex_count": 4, "area_mm2": 16000.0})
        report = run(context_factory(plan, result), ValidationStage.POST_COMMIT)
        assert "POST-OPERATION-COVERAGE" in rule_ids(report, Severity.BLOCKING)


class TestEngineWiring:
    def test_all_rules_are_registered_once(self) -> None:
        engine = default_engine()
        assert len(engine.rule_ids()) == len(set(engine.rule_ids()))
        assert len(engine.rule_ids()) == 40
        assert {
            "LAYER_SET_MATCHES_PROFILE",
            "ENTITY_ON_EXPECTED_LAYER",
            "DIMSTYLE_IN_PROFILE",
            "TEXTSTYLE_IN_PROFILE",
            "DOCUMENT_UNITS_MATCH_PROFILE",
        } <= set(engine.rule_ids())

    def test_post_commit_rules_do_not_run_pre_commit(self, context_factory) -> None:
        plan = build_plan((outline(PLATE),))
        report = run(context_factory(plan))
        assert "POST-MEASUREMENT-MATCH" not in rule_ids(report)
