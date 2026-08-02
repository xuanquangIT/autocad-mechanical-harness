"""Feature registry. The MCP catalog tool reads from here."""

from __future__ import annotations

from cad_harness.domain.errors import UnsupportedFeatureError
from cad_harness.feature_catalog.base import FeatureCompiler

_REGISTRY: dict[str, FeatureCompiler] = {}


def register(compiler: FeatureCompiler) -> FeatureCompiler:
    """Register a compiler. Duplicate feature types are a programming error."""
    if compiler.feature_type in _REGISTRY:
        raise ValueError(f"Feature type already registered: {compiler.feature_type}")
    _REGISTRY[compiler.feature_type] = compiler
    return compiler


def get_compiler(feature_type: str) -> FeatureCompiler:
    try:
        return _REGISTRY[feature_type]
    except KeyError:
        raise UnsupportedFeatureError(
            f"Feature type '{feature_type}' is not in the catalog",
            required_action="Choose a supported feature type or request it be implemented",
            details={"supported": supported_types()},
        ) from None


def supported_types() -> list[str]:
    return sorted(_REGISTRY)


def describe_all() -> list[dict[str, object]]:
    """Catalog listing for ``cad_feature_catalog_search``."""
    return [
        {
            "type": compiler.feature_type,
            "schema_version": compiler.schema_version,
            "description": compiler.description,
            "required_parameters": list(compiler.required_parameters),
            "optional_parameters": list(compiler.optional_parameters),
        }
        for _, compiler in sorted(_REGISTRY.items())
    ]


def search(query: str) -> list[dict[str, object]]:
    needle = query.strip().lower()
    if not needle:
        return describe_all()
    return [
        entry
        for entry in describe_all()
        if needle in str(entry["type"]).lower() or needle in str(entry["description"]).lower()
    ]
