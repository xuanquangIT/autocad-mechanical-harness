from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import scripts.live_owned_mcp_acceptance as subject


class _OwnedProcess:
    pid = 24680
    pipe_template = "cadharness.acceptance.nonce.{user_sid}"
    waited = False
    closed = False

    def wait_until_ready(self, _timeout_seconds: float) -> None:
        self.waited = True

    def __enter__(self) -> _OwnedProcess:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True


def _tree(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    work = tmp_path / "work"
    work.mkdir()
    drawing = work / "seed.dxf"
    drawing.write_bytes(b"0\r\nSECTION\r\n")
    config = tmp_path / "config.yaml"
    config.write_text("config", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    return work, drawing, config, spec, bundle


def _seed_scoped_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    previous = {
        name: f"prior-{index}" for index, name in enumerate(subject._SCOPED_ENVIRONMENT_KEYS)
    }
    for name, value in previous.items():
        monkeypatch.setenv(name, value)
    return previous


def test_owned_mcp_acceptance_runs_real_workflow_and_redacts_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, drawing, config, spec, bundle = _tree(tmp_path)
    evidence = tmp_path / "evidence.json"
    export_root = tmp_path / "exports"
    process = _OwnedProcess()
    launch_args: dict[str, Any] = {}
    snapshots = iter(
        [
            {7348: (r"D:\CAD\AutoCAD 2027\acad.exe", 1)},
            {7348: (r"D:\CAD\AutoCAD 2027\acad.exe", 1)},
        ]
    )
    previous_environment = _seed_scoped_environment(monkeypatch)
    monkeypatch.setattr(subject, "_load_spec", lambda _path: {"features": []})
    monkeypatch.setattr(subject, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        subject,
        "load_settings",
        lambda _path: SimpleNamespace(
            security=SimpleNamespace(export_path_allowlist=(export_root,))
        ),
    )

    def launch(**kwargs: Any) -> _OwnedProcess:
        launch_args.update(kwargs)
        return process

    monkeypatch.setattr(subject, "launch_owned_bridge_process", launch)

    async def workflow(**kwargs: Any) -> dict[str, Any]:
        target = kwargs["export_target"]
        assert isinstance(target, Path)
        target.write_bytes(b"AC1032-result")
        return {"commit": {"status": "committed"}, "approval_token": None}

    monkeypatch.setattr(subject, "_run_mcp_workflow", workflow)

    result = subject.run_acceptance(
        config_path=config,
        spec_path=spec,
        case_name="owned-live",
        drawing_path=drawing,
        work_root=work,
        bundle_root=bundle,
        evidence_path=evidence,
        timeout_seconds=10.0,
    )

    assert process.waited and process.closed
    assert result["real_autocad_evidence"] is True
    assert result["preexisting_processes_preserved"] is True
    assert result["source_drawing_unchanged"] is True
    child = launch_args["child_environment_overrides"]
    assert child["CAD_HARNESS_LIVE_WRITE_VERIFIED"] == "1"
    assert child["CAD_HARNESS_APPROVAL_SECRET"]
    assert {
        name: os.environ.get(name) for name in subject._SCOPED_ENVIRONMENT_KEYS
    } == previous_environment
    serialized = evidence.read_text(encoding="utf-8")
    assert child["CAD_HARNESS_APPROVAL_SECRET"] not in serialized
    assert json.loads(serialized)["export"]["format"] == "dwg"


def test_owned_mcp_acceptance_rejects_unrelated_autocad_process_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, drawing, config, spec, bundle = _tree(tmp_path)
    export_root = tmp_path / "exports"
    process = _OwnedProcess()
    process.waited = False
    process.closed = False
    snapshots = iter(
        [
            {7348: ("acad.exe", 1)},
            {7348: ("acad.exe", 1), 9999: ("acad.exe", 2)},
        ]
    )
    previous_environment = _seed_scoped_environment(monkeypatch)
    monkeypatch.setattr(subject, "_load_spec", lambda _path: {"features": []})
    monkeypatch.setattr(subject, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        subject,
        "load_settings",
        lambda _path: SimpleNamespace(
            security=SimpleNamespace(export_path_allowlist=(export_root,))
        ),
    )
    monkeypatch.setattr(subject, "launch_owned_bridge_process", lambda **_kwargs: process)

    async def workflow(**kwargs: Any) -> dict[str, Any]:
        kwargs["export_target"].write_bytes(b"AC1032-result")
        return {}

    monkeypatch.setattr(subject, "_run_mcp_workflow", workflow)

    with pytest.raises(RuntimeError, match="process set or identity"):
        subject.run_acceptance(
            config_path=config,
            spec_path=spec,
            case_name="owned-live-drift",
            drawing_path=drawing,
            work_root=work,
            bundle_root=bundle,
            evidence_path=tmp_path / "evidence.json",
            timeout_seconds=10.0,
        )

    assert process.closed
    assert not (tmp_path / "evidence.json").exists()
    assert {
        name: os.environ.get(name) for name in subject._SCOPED_ENVIRONMENT_KEYS
    } == previous_environment
