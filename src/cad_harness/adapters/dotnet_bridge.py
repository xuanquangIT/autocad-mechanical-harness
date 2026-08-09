"""Typed Python client for the local C# AutoCAD bridge.

The adapter is deliberately lazy: importing or constructing it neither imports
``pywin32`` nor opens a pipe.  A successful, exact-schema handshake is required before
the bridge's capabilities or operation vocabulary are trusted.
"""

from __future__ import annotations

import json
import math
import re
import struct
from typing import Any, Protocol

from pydantic import ValidationError

from cad_harness.adapters.base import BaseAdapter
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
    AutoCADBusyError,
    AutoCADNotRunningError,
    ComCallFailedError,
    DocumentNotFoundError,
    ErrorCode,
    ExportPathNotAllowedError,
    HarnessError,
    IdempotencyKeyReusedError,
    InvalidFeatureParametersError,
    InvalidGeometryError,
    InvalidJobTransitionError,
    IpcTimeoutError,
    MissingRequiredInputsError,
    PlanHashMismatchError,
    PostCommitValidationFailedError,
    ReadScopeTooLargeError,
    RollbackNotAvailableError,
    StaleDocumentRevisionError,
    StandardProfileNotFoundError,
    ToolNotAllowedError,
    UnknownCommitStateError,
    UnsupportedFeatureError,
    UnsupportedInputFormatError,
    UnsupportedSchemaVersionError,
    WriterLeaseConflictError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION, ContractModel
from cad_harness.domain.models.document import DocumentSnapshot, SelectionSnapshot
from cad_harness.domain.models.operation_plan import OperationPlan, OperationType
from cad_harness.domain.models.result import (
    CommitResult,
    ExportResult,
    PreviewResult,
    RollbackResult,
)
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    AdapterStatus,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)
from cad_harness.domain.value_objects.identifiers import IdPrefix, new_id

#: Local pipe name. The installer restricts its ACL to the permitted account.
DEFAULT_PIPE_NAME = r"\\.\pipe\cadharness.{user_sid}"

#: Public frame ceiling, aligned with the production C# server default.
MAX_FRAME_BYTES = 1_048_576
MAX_JSON_DEPTH = 32

_LENGTH_PREFIX = struct.Struct(">I")
_RESPONSE_STATUSES = frozenset({"ok", "rejected", "conflict", "failed"})
_RESPONSE_FIELDS = frozenset(
    {"schema_version", "request_id", "status", "capabilities", "data", "error"}
)
_ERROR_FIELDS = frozenset({"code", "message", "details", "required_action", "retryable"})
_UNSAFE_DETAIL_KEY_PARTS = (
    "approval",
    "credential",
    "exception",
    "password",
    "path",
    "prompt",
    "secret",
    "stack",
    "token",
    "trace",
)
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_HANDSHAKE_DATA_FIELDS = frozenset(
    {
        "schema_version",
        "capabilities",
        "supported_operations",
        "cad_application",
        "cad_version",
    }
)
_HANDSHAKE_REQUIRED_FIELDS = frozenset({"schema_version", "capabilities", "supported_operations"})
_STATUS_DATA_FIELDS = frozenset(
    {"available", "cad_application", "cad_version", "active_document_id", "message"}
)


class BridgeTransport(Protocol):
    """Small injectable boundary implemented by :class:`NamedPipeTransport`."""

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]: ...


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize one request frame."""
    _validate_json_tree(payload)
    body = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
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
    decoded = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_object_fields,
        parse_constant=_reject_non_finite_json_constant,
    )
    if not isinstance(decoded, dict):
        raise ValueError("Frame body must be a JSON object")
    _validate_json_tree(decoded)
    return decoded


def _reject_duplicate_object_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON objects cannot contain duplicate fields")
        result[key] = value
    return result


def _reject_non_finite_json_constant(value: str) -> None:
    del value
    raise ValueError("JSON numbers must be finite")


def _validate_json_tree(value: object) -> None:
    """Reject values outside bounded RFC 8259 JSON before trusting an envelope."""
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON nesting exceeds the maximum depth {MAX_JSON_DEPTH}")
        if current is None or isinstance(current, bool | int | str):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("JSON numbers must be finite")
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict) and all(isinstance(key, str) for key in current):
            pending.extend((item, depth + 1) for item in current.values())
            continue
        raise ValueError("Envelope contains a value outside the JSON contract")


def _validate_response_envelope(
    response: object,
    *,
    request_id: str,
    method: str,
) -> dict[str, Any]:
    """Validate the closed response schema and status discriminator before mapping data."""
    try:
        _validate_json_tree(response)
    except ValueError as error:
        raise ComCallFailedError(
            "Bridge returned an invalid JSON response envelope",
            required_action="Restart or reinstall the bridge, then inspect its local logs",
            details={"method": method},
        ) from error
    if not isinstance(response, dict):
        raise ComCallFailedError(
            "Bridge response envelope must be an object",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    if set(response) - _RESPONSE_FIELDS or not {
        "schema_version",
        "request_id",
        "status",
    } <= set(response):
        raise ComCallFailedError(
            "Bridge response contains missing or unknown envelope fields",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    if response.get("schema_version") != SCHEMA_VERSION or response.get("request_id") != request_id:
        raise UnsupportedSchemaVersionError(
            "Bridge response did not match the request and schema contract",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    if not isinstance(response["request_id"], str) or not 1 <= len(response["request_id"]) <= 64:
        raise ComCallFailedError(
            "Bridge returned an invalid response request identifier",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    status = response["status"]
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise ComCallFailedError(
            "Bridge returned an invalid response status",
            required_action="Restart or reinstall the bridge, then inspect its local logs",
            details={"method": method},
        )
    capabilities = response.get("capabilities")
    if capabilities is not None and (
        not isinstance(capabilities, list)
        or not all(isinstance(capability, str) for capability in capabilities)
    ):
        raise ComCallFailedError(
            "Bridge returned invalid response capabilities",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    if status == "ok":
        if "error" in response or not isinstance(response.get("data"), dict):
            raise ComCallFailedError(
                "Bridge returned an invalid successful response shape",
                required_action="Install a bridge version matching schema 1.10",
                details={"method": method},
            )
        return response
    if "data" in response or not isinstance(response.get("error"), dict):
        raise ComCallFailedError(
            "Bridge returned an invalid error response shape",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    error_payload = response["error"]
    if (
        set(error_payload) - _ERROR_FIELDS
        or not {"code", "message"} <= set(error_payload)
        or not isinstance(error_payload["code"], str)
        or not isinstance(error_payload["message"], str)
        or ("details" in error_payload and not isinstance(error_payload["details"], dict))
        or (
            "required_action" in error_payload
            and error_payload["required_action"] is not None
            and not isinstance(error_payload["required_action"], str)
        )
        or ("retryable" in error_payload and not isinstance(error_payload["retryable"], bool))
    ):
        raise ComCallFailedError(
            "Bridge returned an invalid error object",
            required_action="Install a bridge version matching schema 1.10",
            details={"method": method},
        )
    return response


def build_request(
    method: str,
    params: dict[str, Any],
    *,
    request_id: str,
    job_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build the IPC envelope without any polymorphic .NET type field."""
    return {
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "request_id": request_id,
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "params": params,
    }


_ERROR_TYPES: dict[ErrorCode, type[HarnessError]] = {
    ErrorCode.MISSING_REQUIRED_INPUTS: MissingRequiredInputsError,
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: UnsupportedSchemaVersionError,
    ErrorCode.UNSUPPORTED_FEATURE: UnsupportedFeatureError,
    ErrorCode.INVALID_FEATURE_PARAMETERS: InvalidFeatureParametersError,
    ErrorCode.INVALID_GEOMETRY: InvalidGeometryError,
    ErrorCode.STANDARD_PROFILE_NOT_FOUND: StandardProfileNotFoundError,
    ErrorCode.DOCUMENT_NOT_FOUND: DocumentNotFoundError,
    ErrorCode.STALE_DOCUMENT_REVISION: StaleDocumentRevisionError,
    ErrorCode.PLAN_HASH_MISMATCH: PlanHashMismatchError,
    ErrorCode.APPROVAL_REQUIRED: ApprovalRequiredError,
    ErrorCode.APPROVAL_EXPIRED: ApprovalExpiredError,
    ErrorCode.APPROVAL_SCOPE_MISMATCH: ApprovalScopeMismatchError,
    ErrorCode.WRITER_LEASE_CONFLICT: WriterLeaseConflictError,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: IdempotencyKeyReusedError,
    ErrorCode.AUTOCAD_NOT_RUNNING: AutoCADNotRunningError,
    ErrorCode.AUTOCAD_BUSY: AutoCADBusyError,
    ErrorCode.ADAPTER_CAPABILITY_MISSING: AdapterCapabilityMissingError,
    ErrorCode.IPC_TIMEOUT: IpcTimeoutError,
    ErrorCode.COM_CALL_FAILED: ComCallFailedError,
    ErrorCode.POST_COMMIT_VALIDATION_FAILED: PostCommitValidationFailedError,
    ErrorCode.UNKNOWN_COMMIT_STATE: UnknownCommitStateError,
    ErrorCode.EXPORT_PATH_NOT_ALLOWED: ExportPathNotAllowedError,
    ErrorCode.ROLLBACK_NOT_AVAILABLE: RollbackNotAvailableError,
    ErrorCode.INVALID_JOB_TRANSITION: InvalidJobTransitionError,
    ErrorCode.TOOL_NOT_ALLOWED: ToolNotAllowedError,
    ErrorCode.UNSUPPORTED_INPUT_FORMAT: UnsupportedInputFormatError,
    ErrorCode.READ_SCOPE_TOO_LARGE: ReadScopeTooLargeError,
}


def _safe_text(value: object, *, fallback: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return fallback
    if "\n" in value or "\r" in value or _ABSOLUTE_PATH.match(value):
        return fallback
    return value


def _safe_details(value: object) -> dict[str, Any]:
    """Keep bounded diagnostic scalars while dropping paths, tokens and nested blobs."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for raw_key, item in value.items():
        if not isinstance(raw_key, str) or len(raw_key) > 64:
            continue
        key = raw_key.lower()
        if any(part in key for part in _UNSAFE_DETAIL_KEY_PARTS):
            continue
        if isinstance(item, str):
            if len(item) <= 256 and not _ABSOLUTE_PATH.match(item) and "\n" not in item:
                result[raw_key] = item
        elif item is None or isinstance(item, bool | int | float):
            result[raw_key] = item
        elif (
            isinstance(item, list)
            and len(item) <= 32
            and all(entry is None or isinstance(entry, bool | int | float | str) for entry in item)
        ):
            result[raw_key] = [
                entry
                for entry in item
                if not isinstance(entry, str)
                or (len(entry) <= 256 and not _ABSOLUTE_PATH.match(entry))
            ]
    return result


class DotNetBridgeAdapter(BaseAdapter):
    """AutoCAD adapter backed by the per-user local C# Named Pipe bridge."""

    adapter_type = "dotnet_bridge"
    capabilities: frozenset[AdapterCapability] = frozenset()
    supported_operations: frozenset[OperationType] = frozenset()

    PRODUCTION_CAPABILITIES: frozenset[AdapterCapability] = frozenset(
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
            AdapterCapability.ROLLBACK_UNDO_GROUP,
            AdapterCapability.IN_VIEWPORT_PREVIEW,
        }
    )
    # Compatibility alias retained for callers that displayed the old roadmap state.
    PLANNED_CAPABILITIES = PRODUCTION_CAPABILITIES

    def __init__(
        self,
        pipe_name: str = DEFAULT_PIPE_NAME,
        *,
        timeout_seconds: float = 30.0,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        transport: BridgeTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.pipe_name = pipe_name
        self.timeout_seconds = timeout_seconds
        if transport is None:
            # The module is imported lazily to avoid its reciprocal frame-helper import.
            # Constructing NamedPipeTransport validates a name but does not open a pipe.
            from cad_harness.adapters.named_pipe_transport import (
                NamedPipeTransport,
                resolve_current_user_pipe_name,
            )

            resolved_pipe_name = (
                resolve_current_user_pipe_name(pipe_name)
                if "{user_sid}" in pipe_name
                else pipe_name
            )
            self.pipe_name = resolved_pipe_name
            transport = NamedPipeTransport(resolved_pipe_name, max_frame_bytes=max_frame_bytes)
        self._transport = transport
        self.capabilities = frozenset()
        self.supported_operations = frozenset()
        self._handshake_data: dict[str, Any] | None = None

    def _effective_timeout(self, requested: float | None) -> float:
        return min(
            self.timeout_seconds,
            self.timeout_seconds if requested is None else requested,
        )

    def _envelope(
        self,
        method: str,
        params: dict[str, Any],
        *,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return build_request(
            method,
            params,
            request_id=new_id(IdPrefix.REQUEST),
            job_id=job_id,
            idempotency_key=idempotency_key,
        )

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        envelope = self._envelope(
            method,
            params,
            job_id=job_id,
            idempotency_key=idempotency_key,
        )
        try:
            response = self._transport.request(
                envelope,
                timeout_seconds=self._effective_timeout(timeout_seconds),
            )
        except HarnessError:
            raise
        except (TypeError, ValueError) as error:
            raise ComCallFailedError(
                "Bridge returned an invalid IPC response",
                required_action="Restart or reinstall the bridge, then inspect its local logs",
                details={"method": method},
            ) from error
        except Exception as error:
            raise ComCallFailedError(
                "Bridge transport failed before a valid response was received",
                required_action="Verify the local bridge pipe and retry only when safe",
                details={"method": method},
            ) from error
        response = _validate_response_envelope(
            response,
            request_id=envelope["request_id"],
            method=method,
        )
        status = response["status"]
        if status != "ok":
            self._raise_remote_error(response["error"], method=method)
        data = response["data"]
        if not isinstance(data, dict):  # pragma: no cover - validated above
            raise AssertionError("response validator returned non-object data")
        return data

    @staticmethod
    def _raise_remote_error(error: object, *, method: str) -> None:
        if not isinstance(error, dict):
            raise ComCallFailedError(
                "Bridge rejected the request without a valid error envelope",
                required_action="Restart or reinstall the bridge, then inspect its local logs",
                details={"method": method},
            )
        raw_code = error.get("code")
        try:
            code = ErrorCode(raw_code) if isinstance(raw_code, str) else ErrorCode.INTERNAL_ERROR
        except (TypeError, ValueError):
            code = ErrorCode.INTERNAL_ERROR
        error_type = _ERROR_TYPES.get(code, HarnessError)
        exception = error_type(
            _safe_text(error.get("message"), fallback="The bridge rejected the request"),
            required_action=_safe_text(
                error.get("required_action"),
                fallback="Inspect the bridge status and retry only when the operation is safe",
            ),
            details=_safe_details(error.get("details")),
        )
        if error_type is HarnessError:
            exception.code = code
        raise exception

    def handshake(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Negotiate exact schema, capabilities and operation enums once per adapter."""
        if self._handshake_data is not None:
            return dict(self._handshake_data)
        envelope = self._envelope("handshake", {"schema_version": SCHEMA_VERSION})
        try:
            response = self._transport.request(
                envelope,
                timeout_seconds=self._effective_timeout(timeout_seconds),
            )
        except HarnessError:
            raise
        except (TypeError, ValueError) as error:
            raise ComCallFailedError(
                "Bridge returned an invalid handshake response",
                required_action="Restart or reinstall the bridge, then inspect its local logs",
                details={"method": "handshake"},
            ) from error
        except Exception as error:
            raise AdapterCapabilityMissingError(
                "The local C# bridge pipe is not available",
                required_action=(
                    "Start AutoCAD, load the signed bridge bundle, and verify the per-user pipe ACL"
                ),
            ) from error
        response = _validate_response_envelope(
            response,
            request_id=envelope["request_id"],
            method="handshake",
        )
        status = response["status"]
        if status != "ok":
            self._raise_remote_error(response["error"], method="handshake")
        data = response["data"]
        data_fields = set(data)
        if data_fields - _HANDSHAKE_DATA_FIELDS or not data_fields >= _HANDSHAKE_REQUIRED_FIELDS:
            raise ComCallFailedError(
                "Bridge handshake data contains missing or unknown fields",
                required_action="Install a bridge version matching schema 1.10",
                details={"method": "handshake"},
            )
        if data.get("schema_version") != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                "Bridge handshake did not negotiate the exact client schema",
                required_action="Install a bridge version matching schema 1.10",
                details={"required_schema_version": SCHEMA_VERSION},
            )
        raw_capabilities = data.get("capabilities")
        raw_operations = data.get("supported_operations")
        if (
            not isinstance(raw_capabilities, list)
            or not all(isinstance(value, str) for value in raw_capabilities)
            or not isinstance(raw_operations, list)
            or not all(isinstance(value, str) for value in raw_operations)
        ):
            raise AdapterCapabilityMissingError(
                "Bridge handshake omitted its capability or operation declarations",
                required_action="Install a complete bridge build and retry the handshake",
            )
        for field in ("cad_application", "cad_version"):
            value = data.get(field)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 128):
                raise ComCallFailedError(
                    "Bridge handshake returned invalid application metadata",
                    required_action="Install a bridge version matching schema 1.10",
                    details={"method": "handshake"},
                )
        try:
            capabilities = frozenset(AdapterCapability(value) for value in raw_capabilities)
            operations = frozenset(OperationType(value) for value in raw_operations)
        except (TypeError, ValueError) as error:
            raise AdapterCapabilityMissingError(
                "Bridge handshake declared an unknown capability or operation",
                required_action="Install a bridge version matching the Python contract",
            ) from error
        if not capabilities <= self.PRODUCTION_CAPABILITIES:
            raise AdapterCapabilityMissingError(
                "Bridge handshake declared a capability outside the production contract",
                required_action="Install a bridge version matching the Python contract",
            )
        if len(capabilities) != len(raw_capabilities) or len(operations) != len(raw_operations):
            raise AdapterCapabilityMissingError(
                "Bridge handshake repeated a capability or operation declaration",
                required_action="Install a bridge version matching the Python contract",
            )
        write_guarantees = {
            AdapterCapability.ATOMIC_TRANSACTION,
            AdapterCapability.DOCUMENT_LOCK,
            AdapterCapability.UNDO_GROUP,
            AdapterCapability.STABLE_METADATA,
        }
        if AdapterCapability.COMMIT in capabilities and not write_guarantees <= capabilities:
            raise AdapterCapabilityMissingError(
                "Bridge advertised commit without all required atomic write guarantees",
                required_action="Install a complete production bridge build before writing",
                details={
                    "missing_capabilities": sorted(
                        capability.value for capability in write_guarantees - capabilities
                    )
                },
            )
        self.capabilities = capabilities
        self.supported_operations = operations
        self._handshake_data = dict(data)
        return dict(data)

    def _ensure_handshake(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        return self.handshake(timeout_seconds=timeout_seconds)

    def status(self) -> AdapterStatus:
        try:
            self._ensure_handshake()
            data = self._request("status", {})
            if set(data) != _STATUS_DATA_FIELDS:
                raise ComCallFailedError(
                    "Bridge returned an invalid status response shape",
                    required_action="Install a bridge version matching schema 1.10",
                    details={"method": "status"},
                )
            available = data["available"]
            if not isinstance(available, bool):
                raise ComCallFailedError(
                    "Bridge returned an invalid status availability flag",
                    required_action="Install a bridge version matching schema 1.10",
                    details={"method": "status"},
                )
            for field in ("cad_application", "cad_version"):
                value = data[field]
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ComCallFailedError(
                        "Bridge returned invalid typed status data",
                        required_action="Install a bridge version matching schema 1.10",
                        details={"method": "status"},
                    )
            for field, maximum in (("active_document_id", 128), ("message", 512)):
                value = data[field]
                if value is not None and (not isinstance(value, str) or len(value) > maximum):
                    raise ComCallFailedError(
                        "Bridge returned invalid typed status data",
                        required_action="Install a bridge version matching schema 1.10",
                        details={"method": "status"},
                    )
            return AdapterStatus(
                adapter_type=self.adapter_type,
                available=available,
                capabilities=tuple(sorted(self.capabilities, key=lambda item: item.value)),
                cad_application=data["cad_application"],
                cad_version=data["cad_version"],
                active_document_id=data["active_document_id"],
                message=data["message"],
            )
        except Exception as error:
            self.capabilities = frozenset()
            self.supported_operations = frozenset()
            self._handshake_data = None
            return AdapterStatus(
                adapter_type=self.adapter_type,
                available=False,
                capabilities=(),
                message=(
                    "Phase 5 C# bridge is unavailable. Start AutoCAD, load the signed "
                    f"bridge bundle, and verify the per-user pipe ACL ({type(error).__name__})."
                ),
            )

    def inspect_document(self, request: InspectRequest) -> DocumentSnapshot:
        self._ensure_handshake()
        self.require(AdapterCapability.INSPECT_DOCUMENT)
        return self._model(
            DocumentSnapshot, self._request("inspect_document", request.model_dump(mode="json"))
        )

    def inspect_selection(self, request: SelectionRequest) -> SelectionSnapshot:
        self._ensure_handshake()
        self.require(AdapterCapability.INSPECT_SELECTION)
        return self._model(
            SelectionSnapshot,
            self._request("inspect_selection", request.model_dump(mode="json")),
        )

    def preview(self, plan: OperationPlan) -> PreviewResult:
        self._ensure_handshake()
        self.require(AdapterCapability.PREVIEW)
        return self._model(
            PreviewResult,
            self._request("preview", plan.model_dump(mode="json"), job_id=plan.job_id),
        )

    def validate_revision(self, document_id: str, expected_revision: str) -> bool:
        self._ensure_handshake()
        data = self._request(
            "validate_revision",
            {"document_id": document_id, "expected_revision": expected_revision},
        )
        valid = data.get("valid")
        if not isinstance(valid, bool):
            raise ComCallFailedError(
                "Bridge returned an invalid revision validation result",
                required_action="Install a bridge version matching schema 1.10",
            )
        return valid

    def commit(self, request: CommitRequest) -> CommitResult:
        self._ensure_handshake()
        self.require(AdapterCapability.COMMIT)
        try:
            data = self._request(
                "commit",
                request.model_dump(mode="json"),
                job_id=request.plan.job_id,
                idempotency_key=request.idempotency_key,
            )
        except IpcTimeoutError as error:
            if (
                error.details.get("terminal_cancel_confirmed") is True
                and error.details.get("cancellation_stage") == "precommit"
                and error.details.get("transaction_aborted") is True
            ):
                raise
            raise UnknownCommitStateError(
                "Bridge commit timed out without proof that it aborted before commit",
                required_action="Reconcile the job and document revision before any retry",
                details=_safe_details(error.details),
            ) from error
        return self._model(CommitResult, data)

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        self._ensure_handshake()
        self.require(AdapterCapability.ROLLBACK_UNDO_GROUP)
        return self._model(
            RollbackResult,
            self._request(
                "rollback",
                request.model_dump(mode="json"),
                job_id=request.job_id,
            ),
        )

    def export(self, request: ExportRequest) -> ExportResult:
        self._ensure_handshake()
        self.require(AdapterCapability.EXPORT)
        return self._model(
            ExportResult,
            self._request("export", request.model_dump(mode="json")),
        )

    @staticmethod
    def _model[ModelT: ContractModel](model_type: type[ModelT], data: dict[str, Any]) -> ModelT:
        try:
            return model_type.model_validate(data)
        except ValidationError as error:
            raise ComCallFailedError(
                "Bridge returned data that does not match schema 1.10",
                required_action="Install a bridge version matching the Python contract",
                details={"model": model_type.__name__},
            ) from error
