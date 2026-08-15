from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.live_owned_bridge_process as subject

from cad_harness.adapters.autocad_com import ComAutoCADAdapter


class _Lease:
    def __init__(self, bridge_path: Path) -> None:
        self.bridge_path = bridge_path
        self.closed = False
        self.revalidated = 0

    def close(self) -> None:
        self.closed = True

    def revalidate_after_load_proof(self) -> None:
        self.revalidated += 1


class _Job:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.assigned: int | None = None
        self.terminated = 0
        self.closed = 0

    def assign_pid(self, pid: int) -> None:
        self.events.append("assign")
        self.assigned = pid

    def contains_pid(self, pid: int) -> bool:
        return self.assigned == pid

    def terminate_and_wait(self, _timeout_seconds: float) -> bool:
        self.events.append("terminate")
        self.terminated += 1
        return True

    def close(self) -> None:
        self.events.append("job_close")
        self.closed += 1


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "work"
    root.mkdir()
    drawing = root / "scratch.dwg"
    drawing.write_bytes(b"AC1032")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bridge = bundle / "AutoCADHarness.dll"
    bridge.write_bytes(b"bridge")
    executable = tmp_path / "acad.exe"
    executable.write_bytes(b"exe")
    return root, drawing, bridge, executable


def test_command_line_is_fixed_and_contains_no_shell(tmp_path: Path) -> None:
    executable = tmp_path / "acad.exe"
    drawing = tmp_path / "drawing.dwg"
    script = tmp_path / "load-bridge.scr"

    command = subject._owned_bridge_command_line(executable, drawing, script)

    assert command == (
        f'"{executable}" "{drawing}" /product ACADM /language "en-US" /nologo /b "{script}"'
    )
    assert "automation" not in command.casefold()
    assert "embedding" not in command.casefold()
    assert "cmd" not in command.casefold()
    assert "powershell" not in command.casefold()


@pytest.mark.parametrize("unsafe", ['bad"name.dwg', "bad\rname.dwg", "bad\nname.dwg"])
def test_command_line_rejects_injection(tmp_path: Path, unsafe: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        subject._owned_bridge_command_line(
            tmp_path / "acad.exe",
            tmp_path / unsafe,
            tmp_path / "load.scr",
        )


def test_fixed_script_contains_only_dialog_gate_and_netload(tmp_path: Path) -> None:
    root, _, bridge, _ = _fixture_tree(tmp_path)

    launch_directory, script = subject._create_fixed_startup_script(root, bridge)
    try:
        assert script.read_bytes() == f'_.NETLOAD\r\n"{bridge.resolve()}"\r\n'.encode("ascii")
        assert script.parent == launch_directory
    finally:
        subject._remove_launch_directory(launch_directory)


def test_drawing_must_be_inside_explicit_work_root(tmp_path: Path) -> None:
    root, _, _, _ = _fixture_tree(tmp_path)
    outside = tmp_path / "outside.dwg"
    outside.write_bytes(b"AC1032")

    with pytest.raises(ValueError, match="contained"):
        subject._validated_drawing(outside, root.resolve())


def test_child_environment_is_case_insensitive_sorted_and_double_nul(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {"Path": "base", "SYSTEMROOT": r"C:\Windows", "ANTHROPIC_API_KEY": "secret"},
    )

    block = subject._windows_environment_block(
        {"PATH": "owned", "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE": "nonce.{user_sid}"}
    )

    assert block.endswith("\0\0")
    entries = block[:-2].split("\0")
    assert entries == sorted(entries, key=str.casefold)
    assert "Path=base" not in entries
    assert "PATH=owned" in entries
    assert r"SYSTEMROOT=C:\Windows" in entries
    assert all("ANTHROPIC" not in entry for entry in entries)
    assert "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE=nonce.{user_sid}" in entries


def test_launch_assigns_suspended_process_before_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, drawing, bridge, executable = _fixture_tree(tmp_path)
    events: list[str] = []
    lease = _Lease(bridge)
    job = _Job(events)

    class _JobFactory:
        @staticmethod
        def create() -> _Job:
            events.append("job_create")
            return job

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subject, "_named_pipe_server_pid", lambda _pipe: None)
    monkeypatch.setattr(subject, "_resolve_local_server_executable", lambda _prog: executable)
    monkeypatch.setattr(subject, "_acquire_acceptance_bundle", lambda: lease)

    def create(*_args: object, **_kwargs: object) -> subject._CreatedProcess:
        events.append("create_suspended")
        return subject._CreatedProcess(pid=42, process_handle=100, thread_handle=101)

    monkeypatch.setattr(subject, "_create_suspended_process", create)
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        lambda _pid: (str(executable), 777),
    )

    def resume(_handle: int) -> int:
        events.append("resume")
        return 1

    monkeypatch.setattr(subject, "_resume_thread", resume)
    monkeypatch.setattr(subject, "_close_handle", lambda _handle: events.append("handle_close"))

    process = subject.launch_owned_bridge_process(
        drawing_path=drawing,
        work_root=root,
        job_factory=_JobFactory,  # type: ignore[arg-type]
    )

    assert process.pid == 42
    assert events[:5] == ["job_create", "create_suspended", "assign", "resume", "handle_close"]
    process.close()


def test_launch_failure_terminates_job_and_releases_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, drawing, bridge, executable = _fixture_tree(tmp_path)
    events: list[str] = []
    lease = _Lease(bridge)
    job = _Job(events)

    class _JobFactory:
        @staticmethod
        def create() -> _Job:
            return job

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subject, "_named_pipe_server_pid", lambda _pipe: None)
    monkeypatch.setattr(subject, "_resolve_local_server_executable", lambda _prog: executable)
    monkeypatch.setattr(subject, "_acquire_acceptance_bundle", lambda: lease)
    monkeypatch.setattr(
        subject,
        "_create_suspended_process",
        lambda *_args, **_kwargs: subject._CreatedProcess(42, 100, 101),
    )
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        lambda _pid: (str(executable), 777),
    )
    monkeypatch.setattr(subject, "_resume_thread", lambda _handle: 2)
    monkeypatch.setattr(subject, "_close_handle", lambda _handle: None)

    with pytest.raises(RuntimeError, match="initially suspended"):
        subject.launch_owned_bridge_process(
            drawing_path=drawing,
            work_root=root,
            job_factory=_JobFactory,  # type: ignore[arg-type]
        )

    assert job.terminated == 1
    assert job.closed == 1
    assert lease.closed
    assert not tuple(root.glob("owned-bridge-*"))


def test_wait_ready_rejects_pipe_from_another_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, bridge, executable = _fixture_tree(tmp_path)
    launch = root / "owned-bridge-test"
    launch.mkdir()
    (launch / "load-bridge.scr").write_text("test", encoding="utf-8")
    lease = _Lease(bridge)
    job = _Job([])
    job.assigned = 42
    process = subject.OwnedBridgeProcess(
        pid=42,
        process_handle=100,
        job=job,
        bundle_lease=lease,  # type: ignore[arg-type]
        script_lease=lease,  # type: ignore[arg-type]
        launch_directory=launch,
        pipe_name=r"\\.\pipe\nonce",
        pipe_template="nonce.{user_sid}",
        expected_executable=executable,
        expected_creation_time_100ns=777,
    )
    monkeypatch.setattr(
        ComAutoCADAdapter,
        "_process_identity",
        lambda _pid: (str(executable), 777),
    )
    monkeypatch.setattr(subject, "_process_is_active", lambda _handle: True)
    monkeypatch.setattr(subject, "_named_pipe_server_pid", lambda _pipe: 99)

    with pytest.raises(RuntimeError, match="another AutoCAD"):
        process.wait_until_ready(0.1)

    monkeypatch.setattr(subject, "_close_handle", lambda _handle: None)
    process.close()


def test_close_is_terminal_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, bridge, executable = _fixture_tree(tmp_path)
    launch = root / "owned-bridge-test"
    launch.mkdir()
    (launch / "load-bridge.scr").write_text("test", encoding="utf-8")
    lease = _Lease(bridge)
    job = _Job([])
    process = subject.OwnedBridgeProcess(
        pid=42,
        process_handle=100,
        job=job,
        bundle_lease=lease,  # type: ignore[arg-type]
        script_lease=_Lease(bridge),  # type: ignore[arg-type]
        launch_directory=launch,
        pipe_name=r"\\.\pipe\nonce",
        pipe_template="nonce.{user_sid}",
        expected_executable=executable,
        expected_creation_time_100ns=777,
    )
    closed_handles: list[int] = []
    monkeypatch.setattr(subject, "_close_handle", closed_handles.append)

    process.close()
    process.close()

    assert job.terminated == 1
    assert job.closed == 1
    assert lease.closed
    assert closed_handles == [100]
    assert not launch.exists()


def test_preexisting_pipe_rejects_before_bundle_or_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, drawing, _, _ = _fixture_tree(tmp_path)
    calls = SimpleNamespace(bundle=0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subject, "_named_pipe_server_pid", lambda _pipe: 7348)
    monkeypatch.setattr(
        subject,
        "_acquire_acceptance_bundle",
        lambda: setattr(calls, "bundle", calls.bundle + 1),
    )

    with pytest.raises(RuntimeError, match="before launch"):
        subject.launch_owned_bridge_process(drawing_path=drawing, work_root=root)

    assert calls.bundle == 0


def test_child_harness_overrides_are_closed_and_reject_control_characters() -> None:
    assert subject._validated_harness_child_overrides({"CAD_HARNESS_LIVE_WRITE_VERIFIED": "1"}) == {
        "CAD_HARNESS_LIVE_WRITE_VERIFIED": "1"
    }
    with pytest.raises(ValueError, match="non-allowlisted"):
        subject._validated_harness_child_overrides({"PATH": "attacker"})
    with pytest.raises(ValueError, match="invalid"):
        subject._validated_harness_child_overrides(
            {"CAD_HARNESS_APPROVAL_SECRET": "secret\ncommand"}
        )
