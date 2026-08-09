"""MCP request/response envelope (architecture section 10.1).

The structured payload is the source of truth. Any prose an AI client shows the user
is an explanation of this envelope, never a substitute for it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.drawing_spec import MissingInput


class ToolStatus(StrEnum):
    OK = "ok"
    NEEDS_INPUT = "needs_input"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"
    #: Read/export batches only. An atomic commit is never partial.
    PARTIAL = "partial"


class ClientInfo(ContractModel):
    name: str
    version: str = "unknown"


class ToolRequestMeta(ContractModel):
    request_id: str | None = None
    schema_version: str = SCHEMA_VERSION
    client: ClientInfo | None = None


class ErrorPayload(ContractModel):
    code: str
    message: str
    retryable: bool = False
    required_action: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(ContractModel):
    """Uniform envelope returned by every public MCP tool."""

    status: ToolStatus
    schema_version: str = SCHEMA_VERSION
    request_id: str | None = None
    job_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    missing_inputs: tuple[MissingInput, ...] = ()
    error: ErrorPayload | None = None
    audit_event_id: str | None = None

    @classmethod
    def ok(
        cls,
        data: dict[str, Any] | None = None,
        *,
        job_id: str | None = None,
        request_id: str | None = None,
        warnings: tuple[str, ...] = (),
        audit_event_id: str | None = None,
    ) -> ToolResponse:
        return cls(
            status=ToolStatus.OK,
            data=data or {},
            job_id=job_id,
            request_id=request_id,
            warnings=warnings,
            audit_event_id=audit_event_id,
        )

    @classmethod
    def needs_input(
        cls,
        missing: tuple[MissingInput, ...],
        *,
        job_id: str | None = None,
        request_id: str | None = None,
    ) -> ToolResponse:
        return cls(
            status=ToolStatus.NEEDS_INPUT,
            missing_inputs=missing,
            job_id=job_id,
            request_id=request_id,
            error=ErrorPayload(
                code="MISSING_REQUIRED_INPUTS",
                message="Required engineering inputs are missing",
                required_action="Supply every listed field, then resubmit the spec",
            ),
        )

    @classmethod
    def from_error(
        cls,
        error: HarnessError,
        *,
        status: ToolStatus = ToolStatus.REJECTED,
        job_id: str | None = None,
        request_id: str | None = None,
    ) -> ToolResponse:
        return cls(
            status=status,
            job_id=job_id,
            request_id=request_id,
            error=ErrorPayload(**error.to_payload()),
        )
