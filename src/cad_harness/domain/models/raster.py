"""Contracts for calibrated, local-only raster-to-vector intake (ADR-016)."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.drawing_model import (
    ArcGeometry,
    CircleGeometry,
    LineGeometry,
    PolylineGeometry,
)


class RasterFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"


class RasterCandidateStatus(StrEnum):
    PROPOSED = "proposed"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"


class PixelPoint(ContractModel):
    x: float
    y: float

    @model_validator(mode="after")
    def _finite(self) -> PixelPoint:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("pixel coordinates must be finite")
        return self


class RasterCalibration(ContractModel):
    """Similarity transform: pixel_a maps to origin_mm and A->B defines +X."""

    pixel_a: PixelPoint
    pixel_b: PixelPoint
    reference_distance_mm: float = Field(gt=0.0)
    origin_mm: tuple[float, float] = (0.0, 0.0)

    @model_validator(mode="after")
    def _valid_reference(self) -> RasterCalibration:
        values = (*self.origin_mm, self.reference_distance_mm)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("calibration values must be finite")
        if math.hypot(self.pixel_b.x - self.pixel_a.x, self.pixel_b.y - self.pixel_a.y) <= 0.0:
            raise ValueError("calibration pixel points must be distinct")
        return self

    @property
    def millimetres_per_pixel(self) -> float:
        pixel_distance = math.hypot(
            self.pixel_b.x - self.pixel_a.x,
            self.pixel_b.y - self.pixel_a.y,
        )
        return self.reference_distance_mm / pixel_distance


class RasterSource(ContractModel):
    source_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    format: RasterFormat
    byte_size: int = Field(gt=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    display_name: str

    @model_validator(mode="after")
    def _safe_display_name(self) -> RasterSource:
        if not self.display_name.strip() or "/" in self.display_name or "\\" in self.display_name:
            raise ValueError("display_name must be a basename, not a path")
        return self


RasterGeometry = Annotated[
    LineGeometry | CircleGeometry | ArcGeometry | PolylineGeometry,
    Field(discriminator="kind"),
]


class RasterVectorCandidate(ContractModel):
    candidate_id: str
    geometry: RasterGeometry
    confidence: float = Field(ge=0.0, le=1.0)
    fit_error_px: float = Field(ge=0.0)
    support_pixels: int = Field(ge=1)
    evidence_bbox_px: tuple[float, float, float, float]
    status: RasterCandidateStatus = RasterCandidateStatus.PROPOSED
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _finite_evidence(self) -> RasterVectorCandidate:
        if not math.isfinite(self.fit_error_px) or any(
            not math.isfinite(value) for value in self.evidence_bbox_px
        ):
            raise ValueError("candidate evidence must be finite")
        left, top, right, bottom = self.evidence_bbox_px
        if right < left or bottom < top:
            raise ValueError("candidate evidence bbox is inverted")
        return self


class RasterTraceReport(ContractModel):
    schema_version: str = SCHEMA_VERSION
    trace_id: str
    source: RasterSource
    calibration: RasterCalibration | None
    candidates: tuple[RasterVectorCandidate, ...] = ()
    overlay_artifact_ref: str
    trace_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    requires_engineer_review: Literal[True] = True
    production_ready: Literal[False] = False
    warnings: tuple[str, ...] = ()


class RasterTraceAcceptance(ContractModel):
    schema_version: str = SCHEMA_VERSION
    trace_id: str
    trace_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    accepted_candidate_ids: tuple[str, ...] = Field(min_length=1)
    accepted_by: str

    @model_validator(mode="after")
    def _unique_ids(self) -> RasterTraceAcceptance:
        if len(set(self.accepted_candidate_ids)) != len(self.accepted_candidate_ids):
            raise ValueError("accepted_candidate_ids must be unique")
        if not self.accepted_by.strip():
            raise ValueError("accepted_by must not be blank")
        return self


__all__ = [
    "PixelPoint",
    "RasterCalibration",
    "RasterCandidateStatus",
    "RasterFormat",
    "RasterGeometry",
    "RasterSource",
    "RasterTraceAcceptance",
    "RasterTraceReport",
    "RasterVectorCandidate",
]
