"""Opt-in destructive acceptance for the verified AutoCAD 2027 bridge tuple."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from scripts import live_r26_rollback_acceptance


@pytest.mark.integration
@pytest.mark.com
@pytest.mark.slow
def test_live_r26_commit_undo_replay_and_activity_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.environ.get("CAD_HARNESS_LIVE_WRITE_ACCEPTANCE") != "1":
        pytest.skip(
            "destructive live bridge acceptance disabled; use a disposable AutoCAD 2027 drawing"
        )
    pipe_name = os.environ.get("CAD_HARNESS_LIVE_PIPE_NAME")
    scratch_file = os.environ.get("CAD_HARNESS_LIVE_SCRATCH_FILE")
    if not pipe_name or not scratch_file or not os.environ.get("CAD_HARNESS_APPROVAL_SECRET"):
        pytest.skip("live pipe, scratch drawing and approval secret are required")

    evidence = tmp_path / "live-r26-rollback-evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_r26_rollback_acceptance.py",
            "--pipe-name",
            pipe_name,
            "--scratch-file",
            scratch_file,
            "--evidence",
            str(evidence),
            "--wait-seconds",
            "30",
        ],
    )

    assert live_r26_rollback_acceptance.main() == 0
    assert evidence.is_file()
