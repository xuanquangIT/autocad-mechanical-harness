"""Adapters implementing :class:`~cad_harness.domain.ports.autocad_adapter.AutoCADAdapter`.

The factory is the only place that knows which concrete adapter a configuration means,
so swapping COM for the C# bridge is a config change, not a code change.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.adapters.base import BaseAdapter
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.adapters.dxf_preview import DxfPreviewAdapter
from cad_harness.adapters.fake import FakeAutoCADAdapter

__all__ = [
    "BaseAdapter",
    "DotNetBridgeAdapter",
    "DxfPreviewAdapter",
    "FakeAutoCADAdapter",
    "build_adapter",
]


def build_adapter(
    adapter_type: str,
    *,
    preview_directory: Path | None = None,
    autocad_prog_id: str = "autocad",
    pipe_name: str | None = None,
) -> BaseAdapter:
    """Instantiate an adapter by name.

    ``com`` is imported lazily so that importing this package never pulls in pywin32
    on a non-Windows machine or in CI.
    """
    normalized = adapter_type.strip().lower()

    if normalized == "fake":
        return FakeAutoCADAdapter()
    if normalized == "dxf_preview":
        return DxfPreviewAdapter(preview_directory or Path("./data/previews"))
    if normalized == "com":
        from cad_harness.adapters.autocad_com import ComAutoCADAdapter

        return ComAutoCADAdapter(autocad_prog_id)
    if normalized == "dotnet_bridge":
        from cad_harness.adapters.dotnet_bridge import DEFAULT_PIPE_NAME

        return DotNetBridgeAdapter(pipe_name or DEFAULT_PIPE_NAME)

    raise ValueError(
        f"Unknown adapter type '{adapter_type}'. "
        "Expected one of: fake, dxf_preview, com, dotnet_bridge"
    )
