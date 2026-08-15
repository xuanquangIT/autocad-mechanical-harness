"""Execution receipts bind a real adapter result to a separate signing authority."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from cad_harness.security.execution_receipt import (
    ExecutionReceipt,
    ExecutionReceiptClaims,
    ExecutionReceiptError,
    execution_public_key,
    execution_public_key_sha256,
    issue_execution_receipt,
    verify_execution_receipt,
)

NOW = datetime(2026, 8, 15, tzinfo=UTC)
PRIVATE_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")


def _claims() -> ExecutionReceiptClaims:
    return ExecutionReceiptClaims(
        adapter_type="com",
        process_id=9260,
        document_id="doc_01M02XP38798JT10RGDRTKKR00",
        pre_revision=f"sha256:{'1' * 64}",
        post_revision=f"sha256:{'2' * 64}",
        plan_hash=f"sha256:{'3' * 64}",
        job_id="job_01M02XP38798JT10RGDRTKKR00",
        validation_report_sha256=f"sha256:{'4' * 64}",
        result_sha256=f"sha256:{'5' * 64}",
    )


def test_receipt_round_trip_is_pinned_and_exact() -> None:
    claims = _claims()
    public = execution_public_key(PRIVATE_KEY)
    receipt = issue_execution_receipt(
        claims,
        signer_id="owned-live-runner",
        private_key=PRIVATE_KEY,
        issued_at=NOW,
    )

    verify_execution_receipt(
        receipt,
        claims,
        public_key=public,
        expected_public_key_sha256=execution_public_key_sha256(public),
        now=NOW,
    )
    assert set(receipt.to_external_dict()) == {
        "schema_version",
        "signer_id",
        "issued_at",
        "claims",
        "signature",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"process_id": 9999},
        {"post_revision": f"sha256:{'9' * 64}"},
        {"result_sha256": f"sha256:{'8' * 64}"},
    ],
)
def test_changed_execution_claims_are_rejected(mutation: dict[str, object]) -> None:
    claims = _claims()
    public = execution_public_key(PRIVATE_KEY)
    receipt = issue_execution_receipt(
        claims,
        signer_id="owned-live-runner",
        private_key=PRIVATE_KEY,
        issued_at=NOW,
    )
    changed = claims.model_copy(update=mutation)

    with pytest.raises(ExecutionReceiptError, match="EXECUTION_CLAIMS_MISMATCH"):
        verify_execution_receipt(
            receipt,
            changed,
            public_key=public,
            expected_public_key_sha256=execution_public_key_sha256(public),
            now=NOW,
        )


def test_signature_key_pin_and_future_timestamp_fail_closed() -> None:
    claims = _claims()
    public = execution_public_key(PRIVATE_KEY)
    receipt = issue_execution_receipt(
        claims,
        signer_id="owned-live-runner",
        private_key=PRIVATE_KEY,
        issued_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ExecutionReceiptError, match="EXECUTION_PUBLIC_KEY_MISMATCH"):
        verify_execution_receipt(
            receipt,
            claims,
            public_key=public,
            expected_public_key_sha256=f"sha256:{'0' * 64}",
            now=NOW,
        )
    with pytest.raises(ExecutionReceiptError, match="EXECUTION_TIMESTAMP_INVALID"):
        verify_execution_receipt(
            receipt,
            claims,
            public_key=public,
            expected_public_key_sha256=execution_public_key_sha256(public),
            now=NOW,
        )


def test_tampered_signature_is_rejected_without_private_key_output() -> None:
    claims = _claims()
    public = execution_public_key(PRIVATE_KEY)
    receipt = issue_execution_receipt(
        claims,
        signer_id="owned-live-runner",
        private_key=PRIVATE_KEY,
        issued_at=NOW,
    )
    tampered = ExecutionReceipt.model_validate(
        {**receipt.to_external_dict(), "signature": f"ed25519:{'A' * 86}"}
    )

    with pytest.raises(ExecutionReceiptError) as captured:
        verify_execution_receipt(
            tampered,
            claims,
            public_key=public,
            expected_public_key_sha256=execution_public_key_sha256(public),
            now=NOW,
        )

    rendered = str(captured.value)
    assert PRIVATE_KEY not in rendered
    assert public not in rendered
