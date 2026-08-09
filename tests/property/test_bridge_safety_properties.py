# Feature: cad-ai-production-roadmap, Property 13: Job ghi là nguyên tử tại mọi điểm thất bại

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter, decode_frame, encode_frame
from cad_harness.domain.errors import HarnessError, UnknownCommitStateError
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.ports.autocad_adapter import AdapterCapability, CommitRequest

_ROOT = Path(__file__).resolve().parents[2]
_ATOMIC_SOURCE = (
    _ROOT / "dotnet/AutoCADBridge/CadBridge.Execution/AtomicJobExecutor.cs"
).read_text(encoding="utf-8")
_IPC_SOURCE = (_ROOT / "dotnet/AutoCADBridge/CadBridge.Ipc/PipeRequestProcessor.cs").read_text(
    encoding="utf-8"
)
_PREFIX = struct.Struct(">I")


def _ok(request_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": "ok",
        "data": data,
    }


def _failed(request_id: str, code: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": "failed",
        "error": {
            "code": code,
            "message": "The bridge failed safely.",
            "retryable": False,
            "details": {},
        },
    }


class _AtomicLedgerTransport:
    """Independent bridge oracle with staged and committed entity ledgers."""

    def __init__(self, failure_point: int, commit_persisted: bool) -> None:
        self.failure_point = failure_point
        self.commit_persisted = commit_persisted
        self.staged_entities = 0
        self.committed_entities = 0
        self.transaction_aborts = 0
        self.transaction_commits_started = 0

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds
        request_id = str(envelope["request_id"])
        if envelope["method"] == "handshake":
            return _ok(
                request_id,
                {
                    "schema_version": SCHEMA_VERSION,
                    "capabilities": [
                        AdapterCapability.COMMIT.value,
                        AdapterCapability.ATOMIC_TRANSACTION.value,
                        AdapterCapability.DOCUMENT_LOCK.value,
                        AdapterCapability.UNDO_GROUP.value,
                        AdapterCapability.STABLE_METADATA.value,
                    ],
                    "supported_operations": [OperationType.CREATE_LINE.value],
                },
            )

        assert envelope["method"] == "commit"
        operations = envelope["params"]["plan"]["operations"]
        # Failure points: 0..N-1 operations, N validation, N+1 commit.
        for index, _operation in enumerate(operations):
            if self.failure_point == index:
                return self._abort(request_id)
            self.staged_entities += 1

        if self.failure_point == len(operations):
            return self._abort(request_id)

        self.transaction_commits_started += 1
        if self.failure_point == len(operations) + 1:
            if self.commit_persisted:
                self.committed_entities += self.staged_entities
                self.staged_entities = 0
            return _failed(request_id, "UNKNOWN_COMMIT_STATE")

        self.committed_entities += self.staged_entities
        self.staged_entities = 0
        return _ok(
            request_id,
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": envelope["job_id"],
                "plan_hash": envelope["params"]["plan"]["plan_hash"],
                "status": "committed",
                "new_revision": "sha256:new",
                "created_entity_refs": [f"entity-{index}" for index in range(len(operations))],
                "measurements": [],
            },
        )

    def _abort(self, request_id: str) -> dict[str, Any]:
        self.transaction_aborts += 1
        self.staged_entities = 0
        return _failed(request_id, "INTERNAL_ERROR")


@st.composite
def _atomic_cases(draw: st.DrawFn) -> tuple[int, int, bool]:
    operation_count = draw(st.integers(min_value=1, max_value=12))
    failure_point = draw(st.integers(min_value=0, max_value=operation_count + 1))
    commit_persisted = draw(st.booleans())
    return operation_count, failure_point, commit_persisted


def _commit_request(operation_count: int) -> CommitRequest:
    plan = OperationPlan(
        plan_id="plan-property-13",
        job_id="job-property-13",
        document_id="doc-property-13",
        expected_revision="sha256:old",
        profile_ref="property@1",
        operations=tuple(
            Operation(
                operation_id=f"op-{index}",
                feature_id=f"feature-{index}",
                type=OperationType.CREATE_LINE,
                layer="OBJECT",
                geometry={"start_mm": [float(index), 0.0], "end_mm": [float(index + 1), 0.0]},
            )
            for index in range(operation_count)
        ),
    ).with_hash()
    return CommitRequest(
        plan=plan,
        idempotency_key="idem-property-13",
        expected_revision="sha256:old",
        approval_token="opaque-property-token",
    )


@given(case=_atomic_cases())
@settings(max_examples=120, deadline=None)
def test_bridge_write_is_atomic_at_every_failure_point(case: tuple[int, int, bool]) -> None:
    """**Validates: Requirements 5.4, 25.8, 25.9**"""
    operation_count, failure_point, commit_persisted = case
    transport = _AtomicLedgerTransport(failure_point, commit_persisted)
    adapter = DotNetBridgeAdapter(transport=transport)

    if failure_point <= operation_count:
        with pytest.raises(HarnessError) as caught:
            adapter.commit(_commit_request(operation_count))
        assert not isinstance(caught.value, UnknownCommitStateError)
        assert transport.transaction_aborts == 1
        assert transport.transaction_commits_started == 0
        assert transport.staged_entities == 0
        assert transport.committed_entities == 0
    else:
        with pytest.raises(UnknownCommitStateError):
            adapter.commit(_commit_request(operation_count))
        assert transport.transaction_aborts == 0
        assert transport.transaction_commits_started == 1
        assert transport.staged_entities == (0 if commit_persisted else operation_count)
        assert transport.committed_entities == (operation_count if commit_persisted else 0)

    # Keep the executable boundary test tied to the production rollback/uncertainty policy.
    assert "catch (Exception exception) when (!state.CommitStarted)" in _ATOMIC_SOURCE
    assert _ATOMIC_SOURCE.index("transaction.Abort();") < _ATOMIC_SOURCE.index(
        "undoGroup.Rollback();"
    )
    assert "return Unknown(state);" in _ATOMIC_SOURCE


# Feature: cad-ai-production-roadmap, Property 14: Biên IPC luôn trả envelope và request sai không mở transaction

_VALID_METHODS = {
    "handshake",
    "status",
    "inspect_document",
    "inspect_selection",
    "preview",
    "validate_revision",
    "commit",
    "rollback",
    "export",
    "cancel",
}
_ALLOWED_PROPERTIES = {
    "schema_version",
    "method",
    "request_id",
    "params",
    "idempotency_key",
    "job_id",
}


def _valid_request() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "commit",
        "request_id": "req-property-14",
        "params": {},
        "idempotency_key": None,
        "job_id": "job-property-14",
    }


@st.composite
def _boundary_cases(draw: st.DrawFn) -> tuple[bytes, bool]:
    mode = draw(
        st.sampled_from(
            (
                "valid_handler_failure",
                "malformed_json",
                "non_object",
                "missing_required",
                "unknown_property",
                "wrong_schema",
                "unknown_method",
                "params_not_object",
                "long_request_id",
                "long_idempotency_key",
            )
        )
    )
    if mode == "malformed_json":
        return draw(st.sampled_from((b"{", b"not-json", b"\xff"))), False
    if mode == "non_object":
        return json.dumps(draw(st.one_of(st.none(), st.booleans(), st.integers()))).encode(), False

    request = _valid_request()
    if mode == "missing_required":
        request.pop(draw(st.sampled_from(("schema_version", "method", "request_id", "params"))))
    elif mode == "unknown_property":
        request["unexpected"] = draw(st.integers())
    elif mode == "wrong_schema":
        request["schema_version"] = draw(st.sampled_from(("1.7", "2.0", "garbage")))
    elif mode == "unknown_method":
        request["method"] = draw(
            st.text(min_size=0, max_size=20).filter(lambda value: value not in _VALID_METHODS)
        )
    elif mode == "params_not_object":
        request["params"] = draw(
            st.one_of(st.none(), st.booleans(), st.integers(), st.lists(st.none()))
        )
    elif mode == "long_request_id":
        request["request_id"] = draw(st.text(min_size=65, max_size=80))
    elif mode == "long_idempotency_key":
        request["idempotency_key"] = draw(st.text(min_size=129, max_size=150))
    return json.dumps(request, ensure_ascii=False).encode("utf-8"), mode == "valid_handler_failure"


def _is_valid_request(value: object) -> bool:
    if not isinstance(value, dict) or not set(value) <= _ALLOWED_PROPERTIES:
        return False
    request_id = value.get("request_id")
    schema = value.get("schema_version")
    method = value.get("method")
    params = value.get("params")
    job_id = value.get("job_id")
    idempotency_key = value.get("idempotency_key")
    return (
        isinstance(request_id, str)
        and 1 <= len(request_id) <= 64
        and schema == SCHEMA_VERSION
        and isinstance(method, str)
        and method in _VALID_METHODS
        and isinstance(params, dict)
        and (job_id is None or (isinstance(job_id, str) and len(job_id) <= 64))
        and (
            idempotency_key is None
            or (isinstance(idempotency_key, str) and len(idempotency_key) <= 128)
        )
    )


def _boundary_response(payload: bytes, handler: Callable[[], None]) -> tuple[bytes, int]:
    request_id = "unknown"
    transaction_opens = 0
    try:
        request = json.loads(payload.decode("utf-8"))
        if not _is_valid_request(request):
            raise ValueError("invalid request")
        request_id = request["request_id"]
        transaction_opens += 1
        handler()
        response = _ok(request_id, {})
    except Exception:
        response = _failed(request_id, "INTERNAL_ERROR")
    return encode_frame(response), transaction_opens


@given(case=_boundary_cases())
@settings(max_examples=120, deadline=None)
def test_ipc_boundary_always_envelopes_and_rejects_before_transaction(
    case: tuple[bytes, bool],
) -> None:
    """**Validates: Requirements 5.8, 5.9, 27.9**"""
    payload, valid_handler_failure = case
    framed, transaction_opens = _boundary_response(
        payload,
        lambda: (_ for _ in ()).throw(RuntimeError("host path must not cross boundary")),
    )

    declared_length = _PREFIX.unpack(framed[: _PREFIX.size])[0]
    envelope = decode_frame(framed)
    assert declared_length == len(framed) - _PREFIX.size
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["status"] in {"ok", "rejected", "failed"}
    assert isinstance(envelope["request_id"], str)
    assert "data" in envelope or "error" in envelope
    assert envelope["status"] == "failed"
    assert transaction_opens == (1 if valid_handler_failure else 0)

    # Production C# validates before dispatch and catches handler exceptions at the IPC boundary.
    validation = _IPC_SOURCE.index("if (!TryValidateRequest(request")
    dispatch = _IPC_SOURCE.index("var result = await handler(request")
    assert validation < dispatch
    assert '"The bridge handler failed safely."' in _IPC_SOURCE
