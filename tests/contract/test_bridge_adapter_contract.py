"""Contract tests for the typed Python side of the C# bridge boundary."""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

import pytest

from cad_harness.adapters.dotnet_bridge import (
    MAX_FRAME_BYTES,
    MAX_JSON_DEPTH,
    DotNetBridgeAdapter,
    decode_frame,
    encode_frame,
)
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ComCallFailedError,
    IpcTimeoutError,
    RollbackRecoveryRequiredError,
    StaleDocumentRevisionError,
    UnknownCommitStateError,
    UnsupportedSchemaVersionError,
)
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    CommitRequest,
    ExportRequest,
    InspectRequest,
    RollbackRequest,
    SelectionRequest,
)


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "request_id": "ignored", "status": "ok", "data": data}


def _handshake(
    *,
    capabilities: list[str] | None = None,
    operations: list[str] | None = None,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    return _ok(
        {
            "schema_version": schema_version,
            "capabilities": capabilities
            if capabilities is not None
            else [capability.value for capability in DotNetBridgeAdapter.PRODUCTION_CAPABILITIES],
            "supported_operations": operations
            if operations is not None
            else [operation.value for operation in OperationType],
            "cad_application": "AutoCAD",
            "cad_version": "25.1s (LMS Tech)",
        }
    )


class FakeTransport:
    def __init__(
        self,
        responses: list[
            dict[str, Any] | BaseException | Callable[[dict[str, Any]], dict[str, Any]]
        ],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[dict[str, Any], float]] = []

    def request(self, envelope: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        self.calls.append((envelope, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(envelope)
        result = dict(response)
        if result.get("request_id") == "ignored":
            result["request_id"] = envelope["request_id"]
        return result


def _plan() -> OperationPlan:
    return OperationPlan(
        plan_id="plan_1",
        job_id="job_1",
        document_id="doc_1",
        expected_revision="sha256:old",
        profile_ref="demo-profile@1.0",
        operations=(
            Operation(
                operation_id="op_1",
                feature_id="feature_1",
                type=OperationType.CREATE_LINE,
                layer="OBJECT",
                geometry={"start_mm": [0.0, 0.0], "end_mm": [10.0, 0.0]},
            ),
        ),
    ).with_hash()


def _commit_request() -> CommitRequest:
    return CommitRequest(
        plan=_plan(),
        idempotency_key="idem_1",
        expected_revision="sha256:old",
        approval_token="secret-not-logged",
    )


def test_construction_is_lazy_and_handshake_is_cached_with_honest_enums() -> None:
    transport = FakeTransport([_handshake()])
    adapter = DotNetBridgeAdapter(transport=transport, timeout_seconds=7.5)

    assert transport.calls == []
    assert adapter.capabilities == frozenset()
    assert adapter.supported_operations == frozenset()

    first = adapter.handshake()
    second = adapter.handshake()

    assert first == second
    assert len(transport.calls) == 1
    envelope, timeout = transport.calls[0]
    assert envelope == {
        "schema_version": SCHEMA_VERSION,
        "method": "handshake",
        "request_id": envelope["request_id"],
        "job_id": None,
        "idempotency_key": None,
        "params": {"schema_version": SCHEMA_VERSION},
    }
    assert envelope["request_id"].startswith("req_")
    assert timeout == 7.5
    assert adapter.capabilities == DotNetBridgeAdapter.PRODUCTION_CAPABILITIES
    assert adapter.supported_operations == frozenset(OperationType)


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_handshake(schema_version="1.7"), UnsupportedSchemaVersionError),
        (_handshake(capabilities=["not_a_capability"]), AdapterCapabilityMissingError),
        (_handshake(operations=["not_an_operation"]), AdapterCapabilityMissingError),
    ],
)
def test_handshake_rejects_schema_and_enum_dishonesty(
    response: dict[str, Any], error_type: type[Exception]
) -> None:
    adapter = DotNetBridgeAdapter(transport=FakeTransport([response]))
    with pytest.raises(error_type):
        adapter.handshake()
    assert adapter.capabilities == frozenset()
    assert adapter.supported_operations == frozenset()


@pytest.mark.parametrize(
    "field_update",
    [
        {"capabilities": [True]},
        {"supported_operations": [False]},
        {"cad_application": True},
        {"cad_version": 25},
        {"unexpected": "field"},
    ],
)
def test_handshake_rejects_untyped_or_open_data(field_update: dict[str, Any]) -> None:
    response = _handshake()
    response["data"] = {**response["data"], **field_update}

    with pytest.raises((AdapterCapabilityMissingError, ComCallFailedError)):
        DotNetBridgeAdapter(transport=FakeTransport([response])).handshake()


def test_status_maps_live_bridge_fields_and_unavailable_status_never_throws() -> None:
    available_transport = FakeTransport(
        [
            _handshake(),
            _ok(
                {
                    "available": True,
                    "cad_application": "AutoCAD Mechanical",
                    "cad_version": "25.1",
                    "active_document_id": "doc_live",
                    "message": "Ready",
                }
            ),
            ConnectionError("bridge stopped after handshake"),
        ]
    )
    available_adapter = DotNetBridgeAdapter(transport=available_transport)
    status = available_adapter.status()
    assert status.available is True
    assert status.cad_application == "AutoCAD Mechanical"
    assert status.cad_version == "25.1"
    assert status.active_document_id == "doc_live"
    assert status.process_id is None
    assert status.capabilities
    assert [call[0]["method"] for call in available_transport.calls] == ["handshake", "status"]

    stopped_status = available_adapter.status()
    assert stopped_status.available is False
    assert available_adapter.capabilities == frozenset()
    assert available_adapter.supported_operations == frozenset()

    unavailable = DotNetBridgeAdapter(
        transport=FakeTransport([FileNotFoundError("pipe does not exist")])
    ).status()
    assert unavailable.available is False
    assert unavailable.capabilities == ()
    assert "Start AutoCAD" in str(unavailable.message)
    assert "AdapterCapabilityMissingError" in str(unavailable.message)


def test_status_reports_the_authenticated_named_pipe_server_process() -> None:
    transport = FakeTransport(
        [
            _handshake(),
            _ok(
                {
                    "available": True,
                    "cad_application": "AutoCAD Mechanical",
                    "cad_version": "26.0",
                    "active_document_id": "doc_live",
                    "message": "Ready",
                }
            ),
        ]
    )
    transport.last_server_process_id = 9260

    status = DotNetBridgeAdapter(transport=transport).status()

    assert status.process_id == 9260


@pytest.mark.parametrize("available", ["false", 0, 1, None])
def test_status_rejects_non_boolean_availability(available: object) -> None:
    status = DotNetBridgeAdapter(
        transport=FakeTransport(
            [
                _handshake(),
                _ok(
                    {
                        "available": available,
                        "cad_application": "AutoCAD",
                        "cad_version": "25.1s (LMS Tech)",
                        "active_document_id": None,
                        "message": None,
                    }
                ),
            ]
        )
    ).status()

    assert status.available is False
    assert status.capabilities == ()
    assert "ComCallFailedError" in str(status.message)


def test_public_frame_limit_matches_server_default() -> None:
    assert MAX_FRAME_BYTES == 1_048_576


@pytest.mark.parametrize("failure", [ValueError("invalid frame"), ConnectionError("pipe closed")])
def test_request_boundary_maps_protocol_and_transport_failures(failure: Exception) -> None:
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), failure]))

    with pytest.raises(ComCallFailedError) as caught:
        adapter.validate_revision("doc_1", "sha256:old")

    assert caught.value.details == {"method": "validate_revision"}


def test_commit_capability_requires_every_atomic_write_guarantee() -> None:
    incomplete = [
        AdapterCapability.COMMIT.value,
        AdapterCapability.ATOMIC_TRANSACTION.value,
        AdapterCapability.DOCUMENT_LOCK.value,
        AdapterCapability.UNDO_GROUP.value,
    ]
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(capabilities=incomplete)]))

    with pytest.raises(AdapterCapabilityMissingError, match="atomic write guarantees") as caught:
        adapter.handshake()

    assert caught.value.details == {"missing_capabilities": ["stable_metadata"]}
    assert adapter.capabilities == frozenset()


def test_rollback_with_session_receipt_requires_undo_capability() -> None:
    capabilities = [
        capability.value
        for capability in DotNetBridgeAdapter.PRODUCTION_CAPABILITIES
        if capability is not AdapterCapability.ROLLBACK_UNDO_GROUP
    ]
    transport = FakeTransport([_handshake(capabilities=capabilities)])
    adapter = DotNetBridgeAdapter(transport=transport)

    with pytest.raises(AdapterCapabilityMissingError) as caught:
        adapter.rollback(
            RollbackRequest(
                job_id="job_1",
                document_id="doc_1",
                checkpoint_id="checkpoint_1",
                current_revision="sha256:current",
                rollback_approval_token="rb1.opaque.signature",
                undo_group="undo-exact-session",
            )
        )

    assert "does not support 'rollback_undo_group'" in str(caught.value)
    assert [call[0]["method"] for call in transport.calls] == ["handshake"]


def test_rollback_without_session_receipt_requires_checkpoint_restore() -> None:
    capabilities = [
        capability.value
        for capability in DotNetBridgeAdapter.PRODUCTION_CAPABILITIES
        if capability is not AdapterCapability.CHECKPOINT_RESTORE
    ]
    transport = FakeTransport([_handshake(capabilities=capabilities)])
    adapter = DotNetBridgeAdapter(transport=transport)

    with pytest.raises(AdapterCapabilityMissingError):
        adapter.rollback(
            RollbackRequest(
                job_id="job_1",
                document_id="doc_1",
                checkpoint_id="checkpoint_1",
                current_revision="sha256:current",
                rollback_approval_token="rb1.opaque.signature",
            )
        )

    assert [call[0]["method"] for call in transport.calls] == ["handshake"]


def test_checkpoint_restore_routes_only_when_capability_is_advertised() -> None:
    capabilities = [capability.value for capability in DotNetBridgeAdapter.PRODUCTION_CAPABILITIES]
    transport = FakeTransport(
        [
            _handshake(capabilities=capabilities),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": "job_1",
                    "restored_revision": "sha256:restored",
                    "checkpoint_id": "checkpoint_1",
                    "method": "checkpoint_restore",
                }
            ),
        ]
    )
    adapter = DotNetBridgeAdapter(transport=transport)

    result = adapter.rollback(
        RollbackRequest(
            job_id="job_1",
            document_id="doc_1",
            checkpoint_id="checkpoint_1",
            current_revision="sha256:current",
            rollback_approval_token="rb1.opaque.signature",
        )
    )

    assert result.method == "checkpoint_restore"
    assert [call[0]["method"] for call in transport.calls] == [
        "handshake",
        "rollback",
    ]


def test_rollback_rejects_unrecognised_recovery_method() -> None:
    adapter = DotNetBridgeAdapter(
        transport=FakeTransport(
            [
                _handshake(),
                _ok(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "job_id": "job_1",
                        "restored_revision": "sha256:restored",
                        "checkpoint_id": "checkpoint_1",
                        "method": "replace_dwg_without_proof",
                    }
                ),
            ]
        )
    )

    with pytest.raises(ComCallFailedError, match="does not match schema"):
        adapter.rollback(
            RollbackRequest(
                job_id="job_1",
                document_id="doc_1",
                checkpoint_id="checkpoint_1",
                current_revision="sha256:current",
                rollback_approval_token="rb1.opaque.signature",
                undo_group="undo-exact-session",
            )
        )


def test_every_adapter_method_maps_exact_request_and_typed_response() -> None:
    plan = _plan()
    commit_request = _commit_request()
    transport = FakeTransport(
        [
            _handshake(),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "document_id": "doc_1",
                    "revision": "sha256:old",
                    "path_hash": "sha256:redacted",
                    "display_name": "part.dwg",
                    "units": "mm",
                    "layers": [{"name": "OBJECT"}],
                    "dimension_styles": ["ISO"],
                    "text_styles": ["Standard"],
                    "entity_count": 1,
                }
            ),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "document_id": "doc_1",
                    "revision": "sha256:old",
                    "entities": [
                        {
                            "entity_ref": "AB",
                            "entity_type": "AcDbLine",
                            "layer": "OBJECT",
                            "feature_id": "feature_1",
                            "measurements": {"length_mm": 10.0},
                        }
                    ],
                    "truncated": False,
                }
            ),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "preview_id": "preview_1",
                    "job_id": "job_1",
                    "plan_hash": plan.plan_hash,
                    "artifacts": [{"kind": "semantic_diff", "artifact_ref": "bridge://preview/1"}],
                    "company_approved": False,
                }
            ),
            _ok({"valid": True}),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": "job_1",
                    "plan_hash": plan.plan_hash,
                    "status": "committed",
                    "entity_results": [
                        {
                            "operation_id": "op_1",
                            "feature_id": "feature_1",
                            "entity_ref": "AB",
                            "entity_type": "AcDbLine",
                            "measurements": {"length_mm": 10.0},
                        }
                    ],
                    "previous_revision": "sha256:old",
                    "new_revision": "sha256:new",
                    "checkpoint_id": "checkpoint_1",
                    "undo_group": "CADHARNESS_job_1",
                }
            ),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": "job_1",
                    "restored_revision": "sha256:restored",
                    "checkpoint_id": "checkpoint_1",
                    "method": "undo_group",
                }
            ),
            _ok(
                {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": None,
                    "document_id": "doc_1",
                    "format": "dxf",
                    "artifact_ref": "exports/part.dxf",
                    "byte_size": 123,
                }
            ),
        ]
    )
    adapter = DotNetBridgeAdapter(transport=transport, timeout_seconds=9.0)

    assert adapter.inspect_document(InspectRequest(document_id="doc_1")).display_name == "part.dwg"
    assert (
        adapter.inspect_selection(SelectionRequest(document_id="doc_1")).entities[0].entity_ref
        == "AB"
    )
    assert adapter.preview(plan).preview_id == "preview_1"
    assert adapter.validate_revision("doc_1", "sha256:old") is True
    assert adapter.commit(commit_request).new_revision == "sha256:new"
    assert (
        adapter.rollback(
            RollbackRequest(
                job_id="job_1",
                document_id="doc_1",
                checkpoint_id="checkpoint_1",
                current_revision="sha256:current",
                rollback_approval_token="rb1.opaque.signature",
                undo_group="CADHARNESS_job_1",
            )
        ).restored_revision
        == "sha256:restored"
    )
    assert (
        adapter.export(
            ExportRequest(document_id="doc_1", format="dxf", target_path="exports/part.dxf")
        ).byte_size
        == 123
    )

    envelopes = [call[0] for call in transport.calls]
    assert [envelope["method"] for envelope in envelopes] == [
        "handshake",
        "inspect_document",
        "inspect_selection",
        "preview",
        "validate_revision",
        "commit",
        "rollback",
        "export",
    ]
    assert len({envelope["request_id"] for envelope in envelopes}) == len(envelopes)
    assert all(envelope["request_id"].startswith("req_") for envelope in envelopes)
    assert all(timeout == 9.0 for _, timeout in transport.calls)

    commit_envelope = envelopes[5]
    assert commit_envelope["job_id"] == "job_1"
    assert commit_envelope["idempotency_key"] == "idem_1"
    assert commit_envelope["params"] == commit_request.model_dump(mode="json")
    assert sum(envelope["method"] == "commit" for envelope in envelopes) == 1


def test_remote_known_error_maps_type_and_strips_sensitive_details() -> None:
    response = {
        "schema_version": SCHEMA_VERSION,
        "request_id": "ignored",
        "status": "conflict",
        "error": {
            "code": "STALE_DOCUMENT_REVISION",
            "message": "Document changed",
            "retryable": True,
            "required_action": "Inspect again",
            "details": {
                "expected_revision": "sha256:old",
                "actual_revision": "sha256:new",
                "source_path": r"C:\secret\part.dwg",
                "approval_token": "do-not-forward",
                "stack": "do-not-forward",
            },
        },
    }
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), response]))

    with pytest.raises(StaleDocumentRevisionError) as caught:
        adapter.inspect_document(InspectRequest(document_id="doc_1"))

    assert caught.value.details == {
        "expected_revision": "sha256:old",
        "actual_revision": "sha256:new",
    }
    assert caught.value.retryable is False


def test_typed_durable_recovery_error_is_retryable_and_never_generic() -> None:
    response = {
        "schema_version": SCHEMA_VERSION,
        "request_id": "ignored",
        "status": "failed",
        "error": {
            "code": "ROLLBACK_RECOVERY_REQUIRED",
            "message": "Exact journal retry required",
            "retryable": True,
            "required_action": "Retry the exact rollback",
        },
    }
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), response]))

    with pytest.raises(RollbackRecoveryRequiredError) as caught:
        adapter.rollback(
            RollbackRequest(
                job_id="job-recovery",
                document_id="doc-recovery",
                checkpoint_id="checkpoint-recovery",
                current_revision="sha256:post",
                rollback_approval_token="rb1.opaque.signature",
            )
        )

    assert caught.value.retryable is True


@pytest.mark.parametrize(
    "response",
    [
        {
            **_ok({"valid": True}),
            "unexpected": "smuggled",
        },
        {
            **_ok({"valid": True}),
            "error": {"code": "INTERNAL_ERROR", "message": "ambiguous"},
        },
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": "ignored",
            "status": "conflict",
            "data": {"valid": False},
            "error": {"code": "STALE_DOCUMENT_REVISION", "message": "changed"},
        },
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": "ignored",
            "status": "failed",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": "ignored",
            "status": "failed",
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "failed",
                "exception_type": "System.Exception",
            },
        },
        {
            **_ok({"valid": True}),
            "capabilities": [AdapterCapability.INSPECT_DOCUMENT.value, 7],
        },
    ],
)
def test_response_envelope_rejects_unknown_fields_and_invalid_status_shapes(
    response: dict[str, Any],
) -> None:
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), response]))

    with pytest.raises(ComCallFailedError):
        adapter.validate_revision("doc_1", "sha256:old")


@pytest.mark.parametrize(
    "response",
    [
        {
            **_ok({"valid": True}),
            "schema_version": "1.7",
        },
        {
            **_ok({"valid": True}),
            "request_id": "req_wrong",
        },
    ],
)
def test_response_envelope_rejects_schema_or_request_correlation_mismatch(
    response: dict[str, Any],
) -> None:
    adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), response]))

    with pytest.raises(UnsupportedSchemaVersionError):
        adapter.validate_revision("doc_1", "sha256:old")


def test_response_envelope_rejects_excessive_depth_and_non_finite_numbers() -> None:
    nested: dict[str, Any] = {"valid": True}
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = {"nested": nested}

    deep_adapter = DotNetBridgeAdapter(transport=FakeTransport([_handshake(), _ok(nested)]))
    with pytest.raises(ComCallFailedError, match="invalid JSON"):
        deep_adapter.validate_revision("doc_1", "sha256:old")

    non_finite_adapter = DotNetBridgeAdapter(
        transport=FakeTransport([_handshake(), _ok({"valid": float("nan")})])
    )
    with pytest.raises(ComCallFailedError, match="invalid JSON"):
        non_finite_adapter.validate_revision("doc_1", "sha256:old")


@pytest.mark.parametrize(
    "body",
    [
        b'{"schema_version":"1.13","request_id":"req_1","status":"ok","data":{"value":NaN}}',
        b'{"schema_version":"1.13","request_id":"req_1","request_id":"req_2",'
        b'"status":"ok","data":{}}',
    ],
)
def test_frame_decoder_rejects_non_standard_or_duplicate_json(body: bytes) -> None:
    with pytest.raises(ValueError):
        decode_frame(struct.pack(">I", len(body)) + body)


def test_frame_encoder_rejects_non_finite_json() -> None:
    with pytest.raises(ValueError):
        encode_frame({"value": float("inf")})


def test_commit_timeout_is_unknown_but_read_timeout_remains_retryable() -> None:
    current_ack_timeout = IpcTimeoutError(
        "deadline",
        details={"request_id": "req_hidden", "terminal_cancel_confirmed": True},
    )
    commit_adapter = DotNetBridgeAdapter(
        transport=FakeTransport([_handshake(), current_ack_timeout])
    )
    with pytest.raises(UnknownCommitStateError) as caught:
        commit_adapter.commit(_commit_request())
    assert caught.value.retryable is False
    assert caught.value.details["request_id"] == "req_hidden"

    read_adapter = DotNetBridgeAdapter(
        transport=FakeTransport([_handshake(), IpcTimeoutError("deadline")])
    )
    with pytest.raises(IpcTimeoutError) as read_timeout:
        read_adapter.inspect_document(InspectRequest(document_id="doc_1"))
    assert read_timeout.value.retryable is True


def test_unadvertised_preview_fails_before_a_preview_pipe_call() -> None:
    transport = FakeTransport(
        [
            _handshake(
                capabilities=[AdapterCapability.INSPECT_DOCUMENT.value],
                operations=[OperationType.CREATE_LINE.value],
            )
        ]
    )
    adapter = DotNetBridgeAdapter(transport=transport)

    with pytest.raises(AdapterCapabilityMissingError):
        adapter.preview(_plan())

    assert [call[0]["method"] for call in transport.calls] == ["handshake"]
