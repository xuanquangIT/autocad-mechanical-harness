"""Named-pipe client for the C# AutoCAD bridge (architecture section 16.3).

Phase 5. The framing and envelope are defined here now so the Python side and the C#
side can be developed against the same contract, and so the adapter port never has to
change when the bridge arrives.

Wire format: length-prefixed UTF-8 JSON.

    +--------------------+------------------------+
    | uint32 big-endian  | UTF-8 JSON payload     |
    | payload length     |                        |
    +--------------------+------------------------+
"""

from __future__ import annotations

import json
import struct
from typing import Any

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.ports.autocad_adapter import AdapterCapability, AdapterStatus

#: Local pipe name. The installer restricts its ACL to the permitted account.
DEFAULT_PIPE_NAME = r"\\.\pipe\cad-harness-bridge"

#: Hard ceiling so a malformed or hostile frame cannot exhaust memory.
MAX_FRAME_BYTES = 8 * 1024 * 1024

_LENGTH_PREFIX = struct.Struct(">I")


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize one request frame."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"Frame exceeds {MAX_FRAME_BYTES} bytes")
    return _LENGTH_PREFIX.pack(len(body)) + body


def decode_frame(frame: bytes) -> dict[str, Any]:
    """Parse one response frame, rejecting oversized or malformed input."""
    if len(frame) < _LENGTH_PREFIX.size:
        raise ValueError("Frame is shorter than its length prefix")
    (length,) = _LENGTH_PREFIX.unpack(frame[: _LENGTH_PREFIX.size])
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"Declared frame length {length} exceeds the maximum")
    body = frame[_LENGTH_PREFIX.size : _LENGTH_PREFIX.size + length]
    if len(body) != length:
        raise ValueError("Frame body is truncated")
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Frame body must be a JSON object")
    return decoded


def build_request(
    method: str,
    params: dict[str, Any],
    *,
    request_id: str,
    job_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build the IPC envelope. Note there is no polymorphic type field by design.

    The bridge must never deserialize a client-supplied .NET type name.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "request_id": request_id,
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "params": params,
    }


class DotNetBridgeAdapter(BaseAdapter):
    """Production adapter. Not implemented until Phase 5."""

    adapter_type = "dotnet_bridge"
    #: Intentionally empty: nothing is available until the bridge ships. The
    #: capabilities it *will* declare are listed in :data:`PLANNED_CAPABILITIES`.
    capabilities: frozenset[AdapterCapability] = frozenset()

    PLANNED_CAPABILITIES: frozenset[AdapterCapability] = frozenset(
        {
            AdapterCapability.INSPECT_DOCUMENT,
            AdapterCapability.INSPECT_SELECTION,
            AdapterCapability.PREVIEW,
            AdapterCapability.COMMIT,
            AdapterCapability.EXPORT,
            AdapterCapability.ATOMIC_TRANSACTION,
            AdapterCapability.DOCUMENT_LOCK,
            AdapterCapability.UNDO_GROUP,
            AdapterCapability.STABLE_METADATA,
            AdapterCapability.CHECKPOINT_RESTORE,
            AdapterCapability.IN_VIEWPORT_PREVIEW,
        }
    )

    def __init__(
        self, pipe_name: str = DEFAULT_PIPE_NAME, *, timeout_seconds: float = 30.0
    ) -> None:
        self.pipe_name = pipe_name
        self.timeout_seconds = timeout_seconds

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            adapter_type=self.adapter_type,
            available=False,
            capabilities=(),
            message=(
                "C# bridge not implemented (roadmap Phase 5). Use the COM adapter for the MVP."
            ),
        )

    def handshake(self) -> dict[str, Any]:
        """Capability and schema-version negotiation, per section 16.3."""
        raise AdapterCapabilityMissingError(
            "C# bridge is not implemented yet",
            required_action="Use adapter type 'com' or 'fake' until Phase 5 ships",
            details={"pipe_name": self.pipe_name},
        )
