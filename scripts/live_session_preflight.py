"""Bind one live-write MCP launch to the exact already-open AutoCAD document."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cad_harness.adapters import build_adapter
from cad_harness.adapters.base import BaseAdapter
from cad_harness.application.live_session_proof import issue_live_session_proof
from cad_harness.application.manual_gate import LIVE_SETUP_STEPS
from cad_harness.config import load_settings
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.ports.autocad_adapter import InspectRequest


def issue_existing_live_session_proof(
    *,
    config_path: Path,
    adapter_type: str,
    secret: str,
    adapter_factory: Callable[..., BaseAdapter] = build_adapter,
) -> str:
    """Attach read-only, bind a proof, then release the proxy without closing CAD."""
    settings = load_settings(config_path)
    if settings.adapter.type != adapter_type or adapter_type not in {"com", "dotnet_bridge"}:
        raise AdapterCapabilityMissingError(
            "Live proof adapter does not match the effective runtime configuration",
            required_action="Use the exact live adapter selected by the acceptance config",
        )
    adapter = adapter_factory(
        adapter_type,
        preview_directory=Path(settings.storage.preview_directory),
        autocad_prog_id=settings.adapter.autocad_prog_id,
        pipe_name=settings.bridge.pipe_name_template,
        timeout_seconds=settings.bridge.ipc_timeout_seconds,
        max_request_bytes=settings.bridge.max_request_bytes,
        write_enabled=False,
    )
    disconnect: Callable[[], Any] | None = None
    try:
        if adapter_type == "com":
            connect = getattr(adapter, "connect", None)
            disconnect_candidate = getattr(adapter, "disconnect", None)
            if not callable(connect) or not callable(disconnect_candidate):
                raise AdapterCapabilityMissingError(
                    "COM live proof adapter has no bounded attach lifecycle"
                )
            connect(launch_if_missing=False)
            disconnect = disconnect_candidate
        status = adapter.status()
        if (
            not status.available
            or status.adapter_type != adapter_type
            or status.process_id is None
            or status.active_document_id is None
        ):
            raise AdapterCapabilityMissingError(
                "Live proof could not bind the AutoCAD process and active document",
                required_action="Open the intended drawing and load the verified adapter",
            )
        snapshot = adapter.inspect_document(InspectRequest(document_id=status.active_document_id))
        if snapshot.document_id != status.active_document_id:
            raise AdapterCapabilityMissingError(
                "Live proof status and document inspection disagree",
                required_action="Stop and inspect the active AutoCAD document",
            )
        return issue_live_session_proof(
            adapter_type=adapter_type,
            process_id=status.process_id,
            document_id=snapshot.document_id,
            revision=snapshot.revision,
            setup_steps=LIVE_SETUP_STEPS,
            secret=secret,
        )
    finally:
        if disconnect is not None:
            disconnect()


__all__ = ["issue_existing_live_session_proof"]
