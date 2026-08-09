"""Deterministic text placement with explicit overlap findings."""

from __future__ import annotations

from dataclasses import dataclass

from cad_harness.domain.models.validation import Finding, Severity

MAXIMUM_OVERLAP_RATIO = 0.10
DEFAULT_OFFSETS_MM = (
    (0.0, 0.0),
    (0.0, 5.0),
    (5.0, 0.0),
    (-5.0, 0.0),
    (0.0, -5.0),
    (10.0, 5.0),
    (-10.0, 5.0),
)


@dataclass(frozen=True, slots=True)
class TextBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def area(self) -> float:
        return max(0.0, self.max_x - self.min_x) * max(0.0, self.max_y - self.min_y)

    def translated(self, dx: float, dy: float) -> TextBox:
        return TextBox(self.min_x + dx, self.min_y + dy, self.max_x + dx, self.max_y + dy)

    def as_list(self) -> list[float]:
        return [self.min_x, self.min_y, self.max_x, self.max_y]


def text_box(text: str, anchor: tuple[float, float], text_height_mm: float) -> TextBox:
    """Conservative single-line box based only on style height and character count."""
    width = max(1, len(text)) * text_height_mm * 0.6
    return TextBox(anchor[0], anchor[1], anchor[0] + width, anchor[1] + text_height_mm)


def overlap_ratio(first: TextBox, second: TextBox) -> float:
    width = max(0.0, min(first.max_x, second.max_x) - max(first.min_x, second.min_x))
    height = max(0.0, min(first.max_y, second.max_y) - max(first.min_y, second.min_y))
    smaller = min(first.area, second.area)
    return 0.0 if smaller <= 0.0 else width * height / smaller


def place_text(
    *,
    text: str,
    anchor: tuple[float, float],
    text_height_mm: float,
    occupied: list[TextBox],
    offsets: tuple[tuple[float, float], ...] = DEFAULT_OFFSETS_MM,
    maximum_overlap_ratio: float = MAXIMUM_OVERLAP_RATIO,
    feature_id: str | None = None,
    operation_id: str | None = None,
) -> tuple[tuple[float, float], TextBox, Finding | None]:
    """Choose the first acceptable candidate or return a warning with the least-overlap box."""
    base = text_box(text, anchor, text_height_mm)
    candidates = tuple((offset, base.translated(*offset)) for offset in offsets)
    for offset, candidate in candidates:
        if all(overlap_ratio(candidate, prior) <= maximum_overlap_ratio for prior in occupied):
            position = (anchor[0] + offset[0], anchor[1] + offset[1])
            occupied.append(candidate)
            return position, candidate, None
    offset, candidate = min(
        candidates,
        key=lambda item: max((overlap_ratio(item[1], prior) for prior in occupied), default=0.0),
    )
    worst = max((overlap_ratio(candidate, prior) for prior in occupied), default=0.0)
    position = (anchor[0] + offset[0], anchor[1] + offset[1])
    return (
        position,
        candidate,
        Finding(
            rule_id="ANNOTATION_OVERLAP",
            severity=Severity.WARNING,
            message="No deterministic annotation position satisfies the overlap limit",
            feature_id=feature_id,
            operation_id=operation_id,
            expected={"maximum_overlap_ratio": maximum_overlap_ratio},
            actual={"overlap_ratio": worst},
            tolerance=maximum_overlap_ratio,
            suggested_fix="Increase view spacing or edit annotation placement before release",
        ),
    )
