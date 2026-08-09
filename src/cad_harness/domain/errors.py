"""Error codes and exception hierarchy (architecture section 20).

Every error surfaced to an MCP client must be actionable: a stable code, whether a
retry is safe, and the action required to move forward.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, client-facing error codes. Never renumber or reuse."""

    MISSING_REQUIRED_INPUTS = "MISSING_REQUIRED_INPUTS"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    INVALID_FEATURE_PARAMETERS = "INVALID_FEATURE_PARAMETERS"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    STANDARD_PROFILE_NOT_FOUND = "STANDARD_PROFILE_NOT_FOUND"
    STANDARD_VIOLATION = "STANDARD_VIOLATION"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    DOCUMENT_NOT_ACTIVE = "DOCUMENT_NOT_ACTIVE"
    STALE_DOCUMENT_REVISION = "STALE_DOCUMENT_REVISION"
    PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_SCOPE_MISMATCH = "APPROVAL_SCOPE_MISMATCH"
    WRITER_LEASE_CONFLICT = "WRITER_LEASE_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    AUTOCAD_NOT_RUNNING = "AUTOCAD_NOT_RUNNING"
    AUTOCAD_BUSY = "AUTOCAD_BUSY"
    ADAPTER_CAPABILITY_MISSING = "ADAPTER_CAPABILITY_MISSING"
    IPC_TIMEOUT = "IPC_TIMEOUT"
    COM_CALL_FAILED = "COM_CALL_FAILED"
    TRANSACTION_ABORTED = "TRANSACTION_ABORTED"
    POST_COMMIT_VALIDATION_FAILED = "POST_COMMIT_VALIDATION_FAILED"
    UNKNOWN_COMMIT_STATE = "UNKNOWN_COMMIT_STATE"
    EXPORT_PATH_NOT_ALLOWED = "EXPORT_PATH_NOT_ALLOWED"
    ROLLBACK_NOT_AVAILABLE = "ROLLBACK_NOT_AVAILABLE"
    INVALID_JOB_TRANSITION = "INVALID_JOB_TRANSITION"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    UNSUPPORTED_INPUT_FORMAT = "UNSUPPORTED_INPUT_FORMAT"
    READ_SCOPE_TOO_LARGE = "READ_SCOPE_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class HarnessError(Exception):
    """Base class for every error the harness reports to a caller.

    ``details`` must stay free of stack traces and absolute sensitive paths; the
    MCP boundary forwards it verbatim to the client.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False
    #: Action a subclass always wants reported when the caller supplies none. Set it
    #: where the way forward does not depend on the call site.
    default_required_action: str | None = None

    def __init__(
        self,
        message: str,
        *,
        required_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.required_action = required_action or self.default_required_action
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        """Render the wire format described in architecture section 20.2."""
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "required_action": self.required_action,
            "details": self.details,
        }


class MissingRequiredInputsError(HarnessError):
    code = ErrorCode.MISSING_REQUIRED_INPUTS


class UnsupportedSchemaVersionError(HarnessError):
    code = ErrorCode.UNSUPPORTED_SCHEMA_VERSION


class UnsupportedFeatureError(HarnessError):
    code = ErrorCode.UNSUPPORTED_FEATURE


class InvalidFeatureParametersError(HarnessError):
    code = ErrorCode.INVALID_FEATURE_PARAMETERS


class InvalidGeometryError(HarnessError):
    code = ErrorCode.INVALID_GEOMETRY


class StandardProfileNotFoundError(HarnessError):
    code = ErrorCode.STANDARD_PROFILE_NOT_FOUND


class DocumentNotFoundError(HarnessError):
    code = ErrorCode.DOCUMENT_NOT_FOUND


class StaleDocumentRevisionError(HarnessError):
    code = ErrorCode.STALE_DOCUMENT_REVISION


class PlanHashMismatchError(HarnessError):
    code = ErrorCode.PLAN_HASH_MISMATCH


class ApprovalRequiredError(HarnessError):
    code = ErrorCode.APPROVAL_REQUIRED


class ApprovalExpiredError(HarnessError):
    code = ErrorCode.APPROVAL_EXPIRED


class ApprovalScopeMismatchError(HarnessError):
    code = ErrorCode.APPROVAL_SCOPE_MISMATCH


class WriterLeaseConflictError(HarnessError):
    code = ErrorCode.WRITER_LEASE_CONFLICT


class IdempotencyKeyReusedError(HarnessError):
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED


class AutoCADNotRunningError(HarnessError):
    code = ErrorCode.AUTOCAD_NOT_RUNNING


class AutoCADBusyError(HarnessError):
    code = ErrorCode.AUTOCAD_BUSY
    retryable = True


class AdapterCapabilityMissingError(HarnessError):
    code = ErrorCode.ADAPTER_CAPABILITY_MISSING


class IpcTimeoutError(HarnessError):
    """A cooperative or transport deadline expired and the result was discarded."""

    code = ErrorCode.IPC_TIMEOUT
    retryable = True
    default_required_action = (
        "Reduce the operation scope or increase its configured timeout, then retry safely"
    )


class ComCallFailedError(HarnessError):
    code = ErrorCode.COM_CALL_FAILED


class PostCommitValidationFailedError(HarnessError):
    code = ErrorCode.POST_COMMIT_VALIDATION_FAILED


class UnknownCommitStateError(HarnessError):
    """Raised when the commit outcome cannot be determined. Never auto-retry."""

    code = ErrorCode.UNKNOWN_COMMIT_STATE


class ExportPathNotAllowedError(HarnessError):
    code = ErrorCode.EXPORT_PATH_NOT_ALLOWED


class RollbackNotAvailableError(HarnessError):
    code = ErrorCode.ROLLBACK_NOT_AVAILABLE


class InvalidJobTransitionError(HarnessError):
    code = ErrorCode.INVALID_JOB_TRANSITION


class ToolNotAllowedError(HarnessError):
    """A client called a tool outside its permission profile's allowlist."""

    code = ErrorCode.TOOL_NOT_ALLOWED
    default_required_action = (
        "Call a tool from the allowed list, or raise the client's permission mode"
    )


class UnsupportedInputFormatError(HarnessError):
    """Raster, PDF or any format the reader cannot turn into exact geometry."""

    code = ErrorCode.UNSUPPORTED_INPUT_FORMAT
    default_required_action = "Supply the drawing in one of the supported formats"


class ReadScopeTooLargeError(HarnessError):
    """The requested read exceeds the configured entity budget.

    Never retryable as-is: retrying the same scope hits the same limit. The caller
    must narrow the scope instead.
    """

    code = ErrorCode.READ_SCOPE_TOO_LARGE
    default_required_action = "Narrow the read scope by layer, space or window and try again"
