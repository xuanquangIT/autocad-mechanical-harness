"""Property 27: title-block values are non-blank and fully attributed."""

from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.annotation.title_block import resolve_title_block
from cad_harness.company_rules.loader import TitleBlockField, load_profile
from cad_harness.domain.models.drawing_spec import DrawingSpec

FIELDS = ("drawing_number", "revision", "author")


# Feature: cad-ai-production-roadmap, Property 27: Title block có xuất xứ đầy đủ và không bao giờ điền giá trị trống
@given(missing=st.frozensets(st.sampled_from(FIELDS)))
@settings(max_examples=100, deadline=None)
def test_title_block_never_writes_blank_or_unattributed_values(missing: frozenset[str]) -> None:
    """**Validates: Requirements 10.1, 10.2**"""
    profile = load_profile("demo-profile").model_copy(
        update={
            "title_block_fields": tuple(
                TitleBlockField(name=name, required=True) for name in FIELDS
            )
        }
    )
    supplied = {name: f"value-{name}" for name in FIELDS if name not in missing}
    spec = DrawingSpec.model_validate(
        {
            "spec_id": "s",
            "document_id": "d",
            "standard_profile": {"profile_id": profile.profile_id, "version": profile.version},
            "annotations": {
                "dimensions": "none",
                "title_block": "demo",
                "title_block_values": supplied,
            },
        }
    )
    result = resolve_title_block(spec, profile)
    assert {item.path.rsplit(".", 1)[-1] for item in result.missing_inputs} == set(missing)
    assert set(result.values) == set(FIELDS) - set(missing)
    for record in result.values.values():
        assert str(record.value).strip()
        assert record.source.strip()
        assert record.source_version.strip()
