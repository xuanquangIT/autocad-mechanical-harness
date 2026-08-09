"""Hand-written IPC envelope schema for the C# bridge.

Kept as a Python literal so there is a single source of truth, and emitted to
``contracts/ipc-envelope.schema.json`` by ``scripts/generate_schemas.py``.

Two properties matter most here:

* The envelope is monomorphic. There is no type discriminator a client could use to
  make the bridge deserialize an arbitrary .NET type.
* A commit is never reported as ``partial``. If the outcome cannot be determined the
  status is ``failed`` with ``UNKNOWN_COMMIT_STATE``, and the caller reconciles instead
  of retrying.
"""

from __future__ import annotations

from typing import Any

FILENAME = "ipc-envelope.schema.json"

METHODS: tuple[str, ...] = (
    "handshake",
    "status",
    "inspect_document",
    "inspect_selection",
    "preview",
    "validate_revision",
    "cancel",
    "commit",
    "rollback",
    "export",
)

IPC_ENVELOPE_SCHEMA: dict[str, Any] = {
    "title": "Bridge IPC envelope",
    "description": (
        "Length-prefixed UTF-8 JSON messages exchanged with the C# AutoCAD bridge over a "
        "local Windows named pipe."
    ),
    "oneOf": [{"$ref": "#/$defs/request"}, {"$ref": "#/$defs/response"}],
    "$defs": {
        "request": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "method", "request_id", "params"],
            "properties": {
                "schema_version": {
                    "type": "string",
                    "pattern": r"^\d+\.\d+$",
                    "description": "A major version mismatch is rejected, never coerced.",
                },
                "method": {"type": "string", "enum": list(METHODS)},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "job_id": {"type": ["string", "null"], "maxLength": 64},
                "idempotency_key": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                    "description": (
                        "Required for commit. The bridge records it so a retry cannot "
                        "duplicate entities."
                    ),
                },
                "params": {"type": "object"},
            },
        },
        "response": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "request_id", "status"],
            "properties": {
                "schema_version": {"type": "string", "pattern": r"^\d+\.\d+$"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "status": {
                    "type": "string",
                    "enum": ["ok", "rejected", "conflict", "failed"],
                    "description": (
                        "A commit is never 'partial'. An indeterminate outcome is 'failed' "
                        "with UNKNOWN_COMMIT_STATE and requires reconciliation."
                    ),
                },
                "data": {"type": "object"},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Returned by handshake. Python gates behaviour on these rather than "
                        "assuming what the bridge supports."
                    ),
                },
                "error": {"$ref": "#/$defs/error"},
            },
        },
        "error": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "message"],
            "properties": {
                "code": {
                    "type": "string",
                    "description": "One of the ErrorCode values in cad_harness.domain.errors.",
                },
                "message": {"type": "string"},
                "retryable": {"type": "boolean", "default": False},
                "required_action": {"type": ["string", "null"]},
                "details": {
                    "type": "object",
                    "description": (
                        "No stack traces and no absolute paths: this crosses a process "
                        "boundary and may be logged."
                    ),
                },
            },
        },
    },
}
