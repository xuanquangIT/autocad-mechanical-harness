"""Destructive opt-in acceptance for the AutoCAD 2027 session undo rollback fence.

The caller owns the scratch AutoCAD process and must pass a unique per-user pipe.  This
script never launches or closes AutoCAD and never saves the active drawing.  It proves
one immediate rollback, idempotent replay, and rejection after an intervening command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.operation_plan import Operation, OperationPlan, OperationType
from cad_harness.domain.models.result import CommitResult
from cad_harness.domain.ports.autocad_adapter import CommitRequest, InspectRequest, RollbackRequest
from cad_harness.security.approval import issue_approval
from cad_harness.security.rollback_approval import issue_rollback_approval


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipe-name", required=True)
    parser.add_argument("--scratch-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_bridge(adapter: DotNetBridgeAdapter, timeout_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_message = "bridge did not answer"
    while time.monotonic() < deadline:
        status = adapter.status()
        if status.available and status.active_document_id:
            return status.model_dump(mode="json")
        last_message = status.message or last_message
        time.sleep(0.5)
    raise TimeoutError(last_message)


def _plan(document_id: str, revision: str, job_id: str, x_offset: float) -> OperationPlan:
    return OperationPlan(
        plan_id=f"plan_{job_id}",
        job_id=job_id,
        document_id=document_id,
        expected_revision=revision,
        profile_ref="live-r26-rollback@1.0",
        operations=(
            Operation(
                operation_id=f"op_{job_id}_line",
                feature_id=f"feature_{job_id}_line",
                type=OperationType.CREATE_LINE,
                layer="0",
                geometry={
                    "start_mm": [x_offset, 140.0],
                    "end_mm": [x_offset + 60.0, 140.0],
                },
                expected={"length_mm": 60.0},
            ),
            Operation(
                operation_id=f"op_{job_id}_circle",
                feature_id=f"feature_{job_id}_circle",
                type=OperationType.CREATE_CIRCLE,
                layer="0",
                geometry={"center_mm": [x_offset + 30.0, 180.0], "radius_mm": 12.5},
                expected={"radius_mm": 12.5, "diameter_mm": 25.0},
            ),
        ),
    ).with_hash()


def _commit(
    adapter: DotNetBridgeAdapter,
    plan: OperationPlan,
    secret: str,
    idempotency_key: str,
) -> CommitResult:
    _, token = issue_approval(
        job_id=plan.job_id,
        document_id=plan.document_id,
        plan_hash=plan.plan_hash or plan.compute_hash(),
        expected_revision=plan.expected_revision,
        approved_by="live-r26-acceptance-engineer",
        secret=secret,
        ttl=timedelta(minutes=10),
    )
    return adapter.commit(
        CommitRequest(
            plan=plan,
            idempotency_key=idempotency_key,
            expected_revision=plan.expected_revision,
            approval_token=token,
            create_checkpoint=True,
        )
    )


def _rollback_request(commit: CommitResult, document_id: str, secret: str) -> RollbackRequest:
    if commit.checkpoint_id is None or commit.undo_group is None:
        raise AssertionError("verified R26 commit must return checkpoint_id and undo_group")
    _, token = issue_rollback_approval(
        job_id=commit.job_id,
        document_id=document_id,
        checkpoint_id=commit.checkpoint_id,
        current_revision=commit.new_revision,
        approved_by="live-r26-rollback-engineer",
        secret=secret,
        ttl=timedelta(minutes=10),
    )
    return RollbackRequest(
        job_id=commit.job_id,
        document_id=document_id,
        checkpoint_id=commit.checkpoint_id,
        current_revision=commit.new_revision,
        rollback_approval_token=token,
        undo_group=commit.undo_group,
    )


def main() -> int:
    args = _args()
    secret = os.environ.get("CAD_HARNESS_APPROVAL_SECRET", "")
    if not secret:
        raise RuntimeError("CAD_HARNESS_APPROVAL_SECRET is required")
    before_file_hash = _sha256(args.scratch_file)
    adapter = DotNetBridgeAdapter(pipe_name=args.pipe_name, timeout_seconds=30.0)
    status = _wait_for_bridge(adapter, args.wait_seconds)
    before = adapter.inspect_document(InspectRequest())
    stamp = str(time.time_ns())

    first_plan = _plan(before.document_id, before.revision, f"job_live_rb_{stamp}_a", 10.0)
    first_commit = _commit(adapter, first_plan, secret, f"idem-live-rb-{stamp}-a")
    first_request = _rollback_request(first_commit, before.document_id, secret)
    first_rollback = adapter.rollback(first_request)
    after_first = adapter.inspect_document(InspectRequest(document_id=before.document_id))
    if (
        first_rollback.restored_revision != before.revision
        or after_first.revision != before.revision
    ):
        raise AssertionError("immediate rollback did not restore the exact pre-commit revision")

    replay = adapter.rollback(first_request)
    after_replay = adapter.inspect_document(InspectRequest(document_id=before.document_id))
    if replay != first_rollback or after_replay.revision != before.revision:
        raise AssertionError("rollback replay changed the drawing or result")

    second_plan = _plan(before.document_id, before.revision, f"job_live_rb_{stamp}_b", 90.0)
    second_commit = _commit(adapter, second_plan, secret, f"idem-live-rb-{stamp}-b")
    # This deliberate read is a separate AutoCAD command.  The session fence must burn
    # the undo receipt before the following rollback reaches Editor.Command.
    after_intervening_read = adapter.inspect_document(
        InspectRequest(document_id=before.document_id)
    )
    second_request = _rollback_request(second_commit, before.document_id, secret)
    rejected_code = None
    try:
        adapter.rollback(second_request)
    except HarnessError as error:
        rejected_code = error.code.value
    if rejected_code != "INVALID_FEATURE_PARAMETERS":
        raise AssertionError(f"intervening command rollback was not rejected: {rejected_code}")
    after_rejection = adapter.inspect_document(InspectRequest(document_id=before.document_id))
    if after_rejection.revision != second_commit.new_revision:
        raise AssertionError("rejected rollback mutated the drawing")

    after_file_hash = _sha256(args.scratch_file)
    if after_file_hash != before_file_hash:
        raise AssertionError("live acceptance modified the scratch file on disk")

    evidence = {
        "status": status,
        "document_id": before.document_id,
        "before_revision": before.revision,
        "first_commit": first_commit.model_dump(mode="json"),
        "first_rollback": first_rollback.model_dump(mode="json"),
        "replay_equal": replay == first_rollback,
        "after_replay_revision": after_replay.revision,
        "second_commit_revision": second_commit.new_revision,
        "intervening_read_revision": after_intervening_read.revision,
        "intervening_command_rejection": rejected_code,
        "after_rejection_revision": after_rejection.revision,
        "scratch_file_sha256_before": before_file_hash,
        "scratch_file_sha256_after": after_file_hash,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
