"""Title-block value resolution with complete provenance and no blank writes."""

from __future__ import annotations

from dataclasses import dataclass, field

from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.models.drawing_spec import DefaultRecord, DrawingSpec, MissingInput


@dataclass(slots=True)
class TitleBlockResult:
    values: dict[str, DefaultRecord] = field(default_factory=dict)
    missing_inputs: list[MissingInput] = field(default_factory=list)


def resolve_title_block(spec: DrawingSpec, profile: CompanyProfile) -> TitleBlockResult:
    """Resolve configured fields from explicit spec values, then versioned profile values."""
    result = TitleBlockResult()
    supplied = spec.annotations.title_block_values
    for field_rule in profile.title_block_fields:
        explicit = supplied.get(field_rule.name)
        value = explicit.strip() if explicit is not None else None
        if value:
            result.values[field_rule.name] = DefaultRecord(
                path=f"annotations.title_block_values.{field_rule.name}",
                value=value,
                source="drawing-spec",
                source_version=spec.schema_version,
                reason="Explicit title-block value supplied by the caller",
                impact="Released drawing identification",
                override_allowed=True,
            )
            continue
        profile_value = field_rule.value.strip() if field_rule.value else None
        if profile_value:
            result.values[field_rule.name] = DefaultRecord(
                path=f"annotations.title_block_values.{field_rule.name}",
                value=profile_value,
                source=profile.profile_id,
                source_version=profile.version,
                reason="Title-block value declared by the selected company profile",
                impact="Released drawing identification",
                override_allowed=True,
            )
            continue
        if field_rule.required:
            result.missing_inputs.append(
                MissingInput(
                    path=f"annotations.title_block_values.{field_rule.name}",
                    reason=f"Required title-block field '{field_rule.name}' has no non-blank value",
                    accepted_formats=("non-empty string",),
                )
            )
    return result
