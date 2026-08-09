"""Versioned contracts for deterministic feature recognition."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from cad_harness.domain.errors import InvalidFeatureParametersError
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.drawing_model import MeasuredValue
from cad_harness.domain.models.drawing_spec import FeatureSpec


class RecognizedFeatureType(StrEnum):
    PART_OUTLINE = "part_outline"
    CIRCULAR_HOLE = "circular_hole"
    RECTANGULAR_HOLE_PATTERN = "rectangular_hole_pattern"
    BOLT_CIRCLE_PATTERN = "bolt_circle_pattern"
    SLOT = "slot"
    FILLET_CORNER = "fillet_corner"
    CHAMFER_CORNER = "chamfer_corner"


_SOURCE_BOUND_TYPES = frozenset(
    {
        RecognizedFeatureType.PART_OUTLINE,
        RecognizedFeatureType.CIRCULAR_HOLE,
        RecognizedFeatureType.FILLET_CORNER,
        RecognizedFeatureType.CHAMFER_CORNER,
    }
)


class RecognizedFeature(ContractModel):
    feature_type: RecognizedFeatureType
    source_revision: str
    entity_refs: tuple[str, ...] = Field(min_length=1)
    parameters: dict[str, MeasuredValue]
    evidence: dict[str, float] = Field(default_factory=dict)

    def _value(self, name: str) -> float:
        try:
            return self.parameters[name].value
        except KeyError:
            raise InvalidFeatureParametersError(
                f"Recognized feature lacks measured parameter '{name}'",
                required_action="Re-run recognition on complete supported geometry",
                details={"feature_type": self.feature_type.value, "parameter": name},
            ) from None

    def to_feature_spec(
        self,
        feature_id: str,
        *,
        user_supplied: dict[str, Any] | None = None,
    ) -> FeatureSpec:
        """Create a semantic spec or a source-bound internal reconstruction spec.

        Internal types carry only revision and entity references. Their compilers also
        require the trusted source DrawingModel in ``CompileContext``, so an ordinary
        AI-authored spec cannot turn coordinates into a write path.
        """

        supplied = dict(user_supplied or {})
        if self.feature_type in _SOURCE_BOUND_TYPES and supplied:
            raise InvalidFeatureParametersError(
                "Source-bound recognition specs cannot accept caller-supplied parameters",
                required_action=(
                    "Compile the unchanged recognition result against its source drawing"
                ),
                details={"rejected_parameters": sorted(supplied)},
            )
        overlap = set(self.parameters).intersection(supplied)
        if overlap:
            raise InvalidFeatureParametersError(
                "User-supplied values cannot override measured recognition parameters",
                details={"overrides": sorted(overlap)},
            )

        if self.feature_type in _SOURCE_BOUND_TYPES:
            parameters: dict[str, Any] = {
                "source_revision": self.source_revision,
                "source_entity_refs": list(self.entity_refs),
            }
            if "source_edge_index" in self.evidence:
                parameters["source_edge_index"] = round(self.evidence["source_edge_index"])
            mapped_type = f"_recognized_{self.feature_type.value}"
        elif self.feature_type is RecognizedFeatureType.RECTANGULAR_HOLE_PATTERN:
            parameters = {
                "hole_diameter_mm": self._value("hole_diameter_mm"),
                "count_x": round(self._value("count_x")),
                "count_y": round(self._value("count_y")),
                "pitch_x_mm": self._value("pitch_x_mm"),
                "pitch_y_mm": self._value("pitch_y_mm"),
                "edge_offset_x_mm": self._value("origin_x_mm"),
                "edge_offset_y_mm": self._value("origin_y_mm"),
            }
            mapped_type = self.feature_type.value
        elif self.feature_type is RecognizedFeatureType.BOLT_CIRCLE_PATTERN:
            parameters = {
                "hole_diameter_mm": self._value("hole_diameter_mm"),
                "pcd_mm": self._value("pcd_mm"),
                "count": round(self._value("count")),
                "center_mm": [self._value("center_x_mm"), self._value("center_y_mm")],
                "start_angle_deg": self._value("start_angle_deg"),
            }
            mapped_type = self.feature_type.value
        elif self.feature_type is RecognizedFeatureType.SLOT:
            parameters = {
                "length_mm": self._value("length_mm"),
                "width_mm": self._value("width_mm"),
                "center_mm": [self._value("center_x_mm"), self._value("center_y_mm")],
                "angle_deg": self._value("angle_deg"),
            }
            mapped_type = self.feature_type.value
        else:  # pragma: no cover - exhaustive enum guard
            raise InvalidFeatureParametersError("Unsupported recognized feature type")
        parameters.update(supplied)
        return FeatureSpec(feature_id=feature_id, type=mapped_type, parameters=parameters)


class CandidateExplanation(ContractModel):
    candidate_id: str
    feature: RecognizedFeature
    rationale: str


class OpenContourFinding(ContractModel):
    code: Literal["OPEN_CONTOUR"] = "OPEN_CONTOUR"
    gap_mm: float
    endpoint_entity_refs: tuple[str, str]


class RecognitionReport(ContractModel):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    revision: str
    features: tuple[RecognizedFeature, ...] = ()
    ambiguous_groups: tuple[tuple[CandidateExplanation, ...], ...] = ()
    open_contours: tuple[OpenContourFinding, ...] = ()
