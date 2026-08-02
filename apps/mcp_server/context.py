"""Server-wide wiring and the uniform response envelope."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cad_harness.adapters import build_adapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.config import Settings, load_settings
from cad_harness.domain.errors import (
    ErrorCode,
    HarnessError,
    IdempotencyKeyReusedError,
    StaleDocumentRevisionError,
    WriterLeaseConflictError,
)
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.observability.logging import configure_logging, get_logger

#: Errors that mean "the world moved", not "you asked wrongly".
_CONFLICT_ERRORS = (
    StaleDocumentRevisionError,
    WriterLeaseConflictError,
    IdempotencyKeyReusedError,
)

#: Adapter or environment failures, as opposed to caller mistakes.
_FAILURE_CODES = frozenset(
    {
        ErrorCode.AUTOCAD_NOT_RUNNING,
        ErrorCode.AUTOCAD_BUSY,
        ErrorCode.COM_CALL_FAILED,
        ErrorCode.IPC_TIMEOUT,
        ErrorCode.TRANSACTION_ABORTED,
        ErrorCode.POST_COMMIT_VALIDATION_FAILED,
        ErrorCode.UNKNOWN_COMMIT_STATE,
        ErrorCode.INTERNAL_ERROR,
    }
)


@dataclass(slots=True)
class ServerContext:
    settings: Settings
    service: HarnessService


def build_context(config_path: Path | None = None) -> ServerContext:
    """Load settings, configure logging, wire the adapter and the service."""
    settings = load_settings(config_path)
    configure_logging(
        level=settings.observability.log_level,
        json_output=settings.observability.log_json,
    )
    adapter = build_adapter(
        settings.adapter.type,
        preview_directory=Path(settings.storage.preview_directory),
        autocad_prog_id=settings.adapter.autocad_prog_id,
    )

    # COM needs an explicit attach. Failing here is better than failing on first write.
    if settings.adapter.type == "com":
        from cad_harness.adapters.autocad_com import ComAutoCADAdapter

        assert isinstance(adapter, ComAutoCADAdapter)
        adapter.connect(launch_if_missing=settings.adapter.launch_autocad_if_missing)

    get_logger(__name__).info(
        "server_configured",
        adapter_type=settings.adapter.type,
        environment=settings.app.environment,
        profile=settings.standards.company_profile,
    )
    return ServerContext(settings=settings, service=HarnessService(settings, adapter))


def ok(
    data: dict[str, Any],
    *,
    job_id: str | None = None,
    request_id: str | None = None,
    warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    return ToolResponse.ok(
        data, job_id=job_id, request_id=request_id, warnings=warnings
    ).model_dump(mode="json", exclude_none=True)


def failure(error: Exception, *, job_id: str | None = None) -> dict[str, Any]:
    """Map an exception onto the response envelope.

    Unexpected exceptions become a generic ``INTERNAL_ERROR``: no stack traces or
    absolute paths cross the MCP boundary.
    """
    if isinstance(error, HarnessError):
        if error.code is ErrorCode.MISSING_REQUIRED_INPUTS:
            status = ToolStatus.NEEDS_INPUT
        elif isinstance(error, _CONFLICT_ERRORS):
            status = ToolStatus.CONFLICT
        elif error.code in _FAILURE_CODES:
            status = ToolStatus.FAILED
        else:
            status = ToolStatus.REJECTED
        get_logger(__name__).warning(
            "tool_error", error_code=error.code.value, job_id=job_id, outcome=status.value
        )
        return ToolResponse.from_error(error, status=status, job_id=job_id).model_dump(
            mode="json", exclude_none=True
        )

    get_logger(__name__).error(
        "tool_unhandled_error", error_type=type(error).__name__, job_id=job_id
    )
    return ToolResponse(
        status=ToolStatus.FAILED,
        job_id=job_id,
        error={  # type: ignore[arg-type]
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": "An internal error occurred",
            "retryable": False,
            "required_action": "Check the server log for the correlated request id",
            "details": {"error_type": type(error).__name__},
        },
    ).model_dump(mode="json", exclude_none=True)
