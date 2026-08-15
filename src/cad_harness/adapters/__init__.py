"""Adapters implementing :class:`~cad_harness.domain.ports.autocad_adapter.AutoCADAdapter`.

The factory is the only place that knows which concrete adapter a configuration means,
so swapping COM for the C# bridge is a config change, not a code change.
"""

from __future__ import annotations

from pathlib import Path

from cad_harness.adapters.base import BaseAdapter
from cad_harness.adapters.bridge_drawing_reader import BridgeDrawingReader
from cad_harness.adapters.com_drawing_reader import ComDrawingReader
from cad_harness.adapters.dotnet_bridge import MAX_FRAME_BYTES, DotNetBridgeAdapter
from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.adapters.dxf_preview import DxfPreviewAdapter
from cad_harness.adapters.fake import FakeAutoCADAdapter

__all__ = [
    "BaseAdapter",
    "BridgeDrawingReader",
    "ComDrawingReader",
    "DotNetBridgeAdapter",
    "DxfDrawingReader",
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
    timeout_seconds: float = 30.0,
    max_request_bytes: int = MAX_FRAME_BYTES,
    write_enabled: bool = False,
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

        return ComAutoCADAdapter(autocad_prog_id, write_enabled=write_enabled)
    if normalized == "dotnet_bridge":
        from cad_harness.adapters.dotnet_bridge import DEFAULT_PIPE_NAME
        from cad_harness.adapters.named_pipe_transport import resolve_current_user_pipe_name

        resolved_pipe_name = (
            resolve_current_user_pipe_name(pipe_name)
            if pipe_name is not None
            else DEFAULT_PIPE_NAME
        )

        return DotNetBridgeAdapter(
            resolved_pipe_name,
            timeout_seconds=timeout_seconds,
            max_frame_bytes=max_request_bytes,
            write_authorized=write_enabled,
        )

    raise ValueError(
        f"Unknown adapter type '{adapter_type}'. "
        "Expected one of: fake, dxf_preview, com, dotnet_bridge"
    )
