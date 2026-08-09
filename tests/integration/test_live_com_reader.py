"""Opt-in live COM reader acceptance in a PID-fenced AutoCAD process."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.live_com_reader_acceptance import run_acceptance

pytestmark = [pytest.mark.integration, pytest.mark.com, pytest.mark.slow]


def test_live_com_reader_is_non_mutating(tmp_path: Path) -> None:
    if os.getenv("CAD_HARNESS_LIVE_COM_READER_ACCEPTANCE") != "1":
        pytest.skip("isolated live COM reader acceptance disabled; set the explicit scratch opt-in")
    source = Path("tests/golden_drawings/extended_01_base_plate_160x100/input_drawing.dxf")
    evidence = run_acceptance(
        source,
        tmp_path / "com-reader-scratch.dxf",
        tmp_path / "evidence.json",
    )
    assert evidence["revision_unchanged"] is True
    assert evidence["selection_unchanged"] is True
    assert evidence["system_variables_unchanged"] is True
    assert evidence["scratch_file_unchanged"] is True
