"""Property 17: catalog support is exactly registry membership."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.feature_catalog import registry

feature_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=40,
)


# Feature: cad-ai-production-roadmap, Property 17: Feature chưa register được báo là chưa hỗ trợ
@given(candidate=feature_names, registered=st.sampled_from(tuple(registry.supported_types())))
@settings(max_examples=100, deadline=None)
def test_unregistered_features_are_reported_unsupported(candidate: str, registered: str) -> None:
    """**Validates: Requirements 7.10**"""
    supported = set(registry.supported_types())
    assert registry.search(registered)
    assert registry.get_compiler(registered).feature_type == registered

    if candidate in supported:
        assert any(item["type"] == candidate for item in registry.search(candidate))
        assert registry.get_compiler(candidate).feature_type == candidate
    else:
        assert all(item["type"] != candidate for item in registry.search(candidate))
        with pytest.raises(UnsupportedFeatureError) as caught:
            registry.get_compiler(candidate)
        assert set(caught.value.details["supported"]) == supported
