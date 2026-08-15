from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.live_mcp_corpus_read_acceptance as corpus_acceptance
from mcp import ClientSession
from scripts.live_mcp_corpus_read_acceptance import (
    _READ_ONLY_TOOL_SEQUENCE,
    _enumerate_drawings,
    _has_unsupported_case,
    _prepare_cases,
    _run_read_only_case,
    run_acceptance,
)

from cad_harness.adapters.autocad_com import ComAutoCADAdapter, OwnedComSession
from cad_harness.domain.errors import ComCallFailedError
from cad_harness.security.client_profiles import READ_ONLY_TOOLS


class _McpResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.structuredContent = {"status": "ok", "data": data}
        self.content: list[Any] = []


class _TranscriptSession:
    def __init__(self, transcript: list[tuple[str, dict[str, Any]]]) -> None:
        self.transcript = list(transcript)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _McpResult:
        self.calls.append((name, arguments))
        expected_name, response = self.transcript.pop(0)
        assert name == expected_name
        return _McpResult(response)


def _transcript(*, unsupported: bool = False) -> list[tuple[str, dict[str, Any]]]:
    unsupported_entries = [{"entity_type": "PRIVATE_PROXY_TYPE", "count": 2}] if unsupported else []
    return [
        (
            "cad_status",
            {
                "adapter": {
                    "adapter_type": "dotnet_bridge",
                    "available": True,
                    "active_document_id": "doc-private",
                    "message": "PRIVATE_STATUS_DETAIL",
                }
            },
        ),
        (
            "cad_document_inspect",
            {
                "document_id": "doc-private",
                "revision": "sha256:revision",
                "display_name": "drawing.dwg",
                "path_hash": "sha256:opaque-path",
                "read_only": True,
                "entity_count": 2,
                "layers": [{"name": "PRIVATE_LAYER"}],
            },
        ),
        (
            "cad_drawing_read",
            {
                "document_id": "doc-private",
                "revision": "sha256:revision",
                "display_name": "PRIVATE_CUSTOMER_FILENAME.dwg",
                "entities": [
                    {
                        "entity_ref": "PRIVATE_ENTITY_REF",
                        "geometry": {"text": "PRIVATE_DRAWING_TEXT"},
                    },
                    {"entity_ref": "PRIVATE_ENTITY_REF_2", "geometry": {"radius": 73.125}},
                ],
                "layers": [{"name": "PRIVATE_LAYER"}],
                "dimension_styles": ["PRIVATE_DIMSTYLE"],
                "text_styles": ["PRIVATE_TEXTSTYLE"],
                "unsupported": unsupported_entries,
                "coverage_complete": not unsupported,
            },
        ),
        (
            "cad_feature_recognize",
            {
                "document_id": "doc-private",
                "revision": "sha256:revision",
                "features": [{"entity_refs": ["PRIVATE_ENTITY_REF"]}],
                "ambiguous_groups": [[{"reason": "PRIVATE_REASON"}]],
                "open_contours": [{"entity_refs": ["PRIVATE_ENTITY_REF_2"]}],
            },
        ),
        (
            "cad_audit",
            {
                "audit_id": "PRIVATE_AUDIT_ID",
                "document_id": "doc-private",
                "revision": "sha256:revision",
                "report": {"findings": [{"actual": "PRIVATE_DRAWING_TEXT"}]},
            },
        ),
    ]


def test_fake_mcp_transcript_is_exactly_read_only_and_evidence_is_redacted() -> None:
    session = _TranscriptSession(_transcript())
    evidence = asyncio.run(
        _run_read_only_case(
            cast(ClientSession, session),
            case_id="case-opaque",
            expected_display_name="drawing.dwg",
            expected_path_hash="sha256:opaque-path",
            source_format="dwg",
            max_entities=321,
        )
    )

    assert session.transcript == []
    assert tuple(name for name, _arguments in session.calls) == _READ_ONLY_TOOL_SEQUENCE
    assert frozenset(name for name, _arguments in session.calls) <= READ_ONLY_TOOLS
    read_arguments = session.calls[2][1]
    assert read_arguments == {
        "request": {
            "source": {
                "kind": "active_document",
                "format": "dwg",
                "ref": "doc-private",
            },
            "scope": {"kind": "model_space"},
            "max_entities": 321,
            "max_block_nesting_depth": 5,
            "include_geometry": True,
        }
    }
    assert evidence["counts"] == {
        "inspected_entities": 2,
        "read_entities": 2,
        "layers": 1,
        "dimension_styles": 1,
        "text_styles": 1,
        "recognized_features": 1,
        "ambiguous_groups": 1,
        "open_contours": 1,
        "audit_findings": 1,
    }
    assert evidence["coverage"] == {
        "complete": True,
        "unsupported_type_count": 0,
        "unsupported_entity_count": 0,
    }
    serialized = json.dumps(evidence, sort_keys=True)
    for private_value in (
        "PRIVATE_STATUS_DETAIL",
        "PRIVATE_CUSTOMER_FILENAME",
        "PRIVATE_DRAWING_TEXT",
        "PRIVATE_ENTITY_REF",
        "PRIVATE_LAYER",
        "PRIVATE_DIMSTYLE",
        "PRIVATE_TEXTSTYLE",
        "PRIVATE_REASON",
        "PRIVATE_AUDIT_ID",
    ):
        assert private_value not in serialized


def test_unsupported_entity_evidence_is_aggregate_only_and_fails_acceptance_gate() -> None:
    session = _TranscriptSession(_transcript(unsupported=True))
    evidence = asyncio.run(
        _run_read_only_case(
            cast(ClientSession, session),
            case_id="case-unsupported",
            expected_display_name="drawing.dwg",
            expected_path_hash="sha256:opaque-path",
            source_format="dwg",
            max_entities=100,
        )
    )

    assert evidence["coverage"] == {
        "complete": False,
        "unsupported_type_count": 1,
        "unsupported_entity_count": 2,
    }
    assert _has_unsupported_case((evidence,)) is True
    assert "PRIVATE_PROXY_TYPE" not in json.dumps(evidence, sort_keys=True)


def test_inventory_is_bounded_deterministic_and_copies_to_opaque_cases(tmp_path: Path) -> None:
    source_root = tmp_path / "customer-input"
    nested = source_root / "PRIVATE_PROJECT_FOLDER"
    nested.mkdir(parents=True)
    first = source_root / "PRIVATE_CUSTOMER_A.DWG"
    second = nested / "PRIVATE_CUSTOMER_B.dxf"
    ignored = source_root / "PRIVATE_NOT_A_DRAWING.txt"
    first.write_bytes(b"first drawing bytes")
    second.write_bytes(b"second drawing bytes")
    ignored.write_text("not drawing intake", encoding="utf-8")
    run_root = tmp_path / "work" / "run-test"
    run_root.mkdir(parents=True)

    cases = _prepare_cases(
        input_root=source_root,
        run_root=run_root,
        max_cases=2,
        max_file_bytes=100,
        max_total_bytes=100,
    )

    assert len(cases) == 2
    assert {case.source for case in cases} == {first, second}
    assert all(case.case_id.startswith("case-") for case in cases)
    assert all("PRIVATE" not in case.case_id for case in cases)
    assert {case.scratch.name for case in cases} == {"drawing.dwg", "drawing.dxf"}
    assert first.read_bytes() == b"first drawing bytes"
    assert second.read_bytes() == b"second drawing bytes"
    assert all(case.scratch.read_bytes() == case.source.read_bytes() for case in cases)
    expected_ids = {
        f"case-{hashlib.sha256(first.read_bytes()).hexdigest()}",
        f"case-{hashlib.sha256(second.read_bytes()).hexdigest()}",
    }
    assert {case.case_id for case in cases} == expected_ids

    with pytest.raises(ValueError, match="case limit exceeded"):
        _prepare_cases(
            input_root=source_root,
            run_root=tmp_path / "unused-case-limit",
            max_cases=1,
            max_file_bytes=100,
            max_total_bytes=100,
        )
    with pytest.raises(ValueError, match="file byte limit exceeded"):
        _prepare_cases(
            input_root=source_root,
            run_root=tmp_path / "unused-byte-limit",
            max_cases=2,
            max_file_bytes=5,
            max_total_bytes=100,
        )


def test_inventory_rejects_reparse_entries_before_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "input"
    source_root.mkdir()
    blocked = source_root / "blocked.dwg"
    blocked.write_bytes(b"drawing")
    monkeypatch.setattr(
        corpus_acceptance,
        "_is_reparse_point",
        lambda path: path == blocked,
    )

    with pytest.raises(ValueError, match="symlink or reparse"):
        _enumerate_drawings(source_root)


class _FakeDocument:
    def __init__(self) -> None:
        self.close_arguments: list[bool] = []

    def Close(self, save_changes: bool) -> None:  # noqa: N802 - mirrors AutoCAD COM
        self.close_arguments.append(save_changes)


class _FakeCom:
    def __init__(self, *, close_failure: BaseException | None = None) -> None:
        self.active_pids = {101}
        self._document: _FakeDocument | None = None
        self.opened: list[tuple[Path, bool, _FakeDocument]] = []
        self.close_failure = close_failure

    def _system_filetime_100ns(self) -> int:
        return 1_000

    def _acad_process_ids(self) -> set[int]:
        return set(self.active_pids)

    def connect_isolated(self, *, versioned_prog_id: str) -> OwnedComSession:
        assert versioned_prog_id == "AutoCAD.Application.26"
        self.active_pids.add(202)
        return OwnedComSession(
            prog_id=versioned_prog_id,
            hwnd=44,
            pid=202,
            image_path="C:\\Program Files\\Autodesk\\AutoCAD 2027\\acad.exe",
            creation_time_100ns=2_000,
        )

    def open_owned_document(self, path: Path, *, read_only: bool = True) -> str:
        assert path.is_file()
        document = _FakeDocument()
        self._document = document
        self.opened.append((path, read_only, document))
        return "doc-owned"

    def _require_document(self) -> _FakeDocument:
        if self._document is None:
            raise RuntimeError("no active fake document")
        return self._document

    def close_owned_session(self) -> None:
        if self.close_failure is not None:
            raise self.close_failure


def _install_fake_process_helpers(
    monkeypatch: pytest.MonkeyPatch,
    fake_com: _FakeCom,
    cleanup_calls: list[dict[str, Any]],
    *,
    cleanup_failure: BaseException | None = None,
) -> None:
    monkeypatch.setattr(
        corpus_acceptance,
        "_wait_for_owned_com_readiness",
        lambda _com, *, timeout_seconds: None,
    )

    def track(_com: Any, **kwargs: Any) -> set[int]:
        kwargs["tracked"].setdefault(
            202,
            ("C:\\Program Files\\Autodesk\\AutoCAD 2027\\acad.exe", 2_000, 77),
        )
        return set(fake_com.active_pids)

    def cleanup(_com: Any, **kwargs: Any) -> set[int]:
        cleanup_calls.append(kwargs)
        if cleanup_failure is not None:
            raise cleanup_failure
        fake_com.active_pids = {101}
        return {101}

    monkeypatch.setattr(corpus_acceptance, "_track_acceptance_processes", track)
    monkeypatch.setattr(corpus_acceptance, "_cleanup_acceptance_processes", cleanup)
    monkeypatch.setattr(
        corpus_acceptance,
        "_required_acceptance_bundle_root",
        lambda: Path(r"D:\workspace\data\live-r26\ApplicationPlugins\AutoCADHarness.bundle"),
    )


def _fake_case_result(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "tool_statuses": [{"tool": tool, "status": "ok"} for tool in _READ_ONLY_TOOL_SEQUENCE],
        "document": {
            "inspect_revision": "sha256:revision",
            "read_revision": "sha256:revision",
            "read_only": True,
        },
        "counts": {
            "inspected_entities": 1,
            "read_entities": 1,
            "layers": 1,
            "dimension_styles": 0,
            "text_styles": 0,
            "recognized_features": 0,
            "ambiguous_groups": 0,
            "open_contours": 0,
            "audit_findings": 0,
        },
        "coverage": {
            "complete": True,
            "unsupported_type_count": 0,
            "unsupported_entity_count": 0,
        },
    }


def test_runner_opens_only_copy_read_only_and_restores_process_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("adapter:\n  type: dotnet_bridge\n", encoding="utf-8")
    source_root = tmp_path / "PRIVATE_CORPUS"
    source_root.mkdir()
    source = source_root / "PRIVATE_CUSTOMER_NAME.dxf"
    original = b"customer drawing stays byte-identical"
    source.write_bytes(original)
    fake_com = _FakeCom()
    cleanup_calls: list[dict[str, Any]] = []
    bridge_loads: list[ComAutoCADAdapter] = []
    bridge_bundle_roots: list[str] = []
    workflow_arguments: list[dict[str, Any]] = []
    _install_fake_process_helpers(monkeypatch, fake_com, cleanup_calls)

    def workflow(**kwargs: Any) -> dict[str, Any]:
        workflow_arguments.append(kwargs)
        result = _fake_case_result(kwargs["case_id"])
        result["future_unreviewed_field"] = "PRIVATE_UNREVIEWED_PAYLOAD"
        result["document"]["future_private_field"] = "PRIVATE_NESTED_PAYLOAD"
        result["tool_statuses"][0]["message"] = "PRIVATE_TOOL_MESSAGE"
        return result

    evidence = run_acceptance(
        config_path=config,
        input_root=source_root,
        work_root=tmp_path / "work",
        evidence_root=tmp_path / "evidence",
        max_cases=1,
        max_file_bytes=1_000,
        max_total_bytes=1_000,
        max_entities=50,
        bridge_settle_seconds=0,
        _com_factory=lambda _prog_id, *, startup_wait_seconds: cast(ComAutoCADAdapter, fake_com),
        _case_workflow=workflow,
        _bridge_loader=lambda com: (
            bridge_loads.append(com),
            bridge_bundle_roots.append(os.environ["CAD_HARNESS_ACCEPTANCE_BUNDLE_ROOT"]),
        ),
        _sleep=lambda _seconds: None,
        _run_id_factory=lambda: "run-unittest",
    )

    assert source.read_bytes() == original
    assert len(fake_com.opened) == 1
    opened_path, read_only, document = fake_com.opened[0]
    assert opened_path != source
    assert opened_path.name == "drawing.dxf"
    assert opened_path.read_bytes() == original
    assert read_only is True
    assert document.close_arguments == [False]
    assert len(bridge_loads) == 1
    assert bridge_bundle_roots == [
        r"D:\workspace\data\live-r26\ApplicationPlugins\AutoCADHarness.bundle"
    ]
    assert workflow_arguments[0]["expected_display_name"] == "drawing.dxf"
    assert workflow_arguments[0]["source_format"] == "dxf"
    assert cleanup_calls[0]["preexisting_pids"] == {101}
    assert cleanup_calls[0]["ownership_roots"] == {202}
    assert evidence["process_baseline"] == {
        "preexisting_pids": [101],
        "postexisting_pids": [101],
        "unchanged": True,
    }
    serialized = json.dumps(evidence, sort_keys=True)
    assert "PRIVATE_CUSTOMER_NAME" not in serialized
    assert "PRIVATE_UNREVIEWED_PAYLOAD" not in serialized
    assert "PRIVATE_NESTED_PAYLOAD" not in serialized
    assert "PRIVATE_TOOL_MESSAGE" not in serialized
    assert str(source_root) not in serialized
    persisted = json.loads((tmp_path / "evidence" / "run-unittest.json").read_text())
    assert persisted == evidence


def test_cleanup_failures_do_not_mask_original_workflow_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OriginalWorkflowError(RuntimeError):
        pass

    config = tmp_path / "config.yaml"
    config.write_text("adapter:\n  type: dotnet_bridge\n", encoding="utf-8")
    source_root = tmp_path / "input"
    source_root.mkdir()
    (source_root / "source.dwg").write_bytes(b"drawing")
    fake_com = _FakeCom(close_failure=RuntimeError("close cleanup failed"))
    cleanup_calls: list[dict[str, Any]] = []
    _install_fake_process_helpers(
        monkeypatch,
        fake_com,
        cleanup_calls,
        cleanup_failure=RuntimeError("process cleanup failed"),
    )

    def failing_workflow(**_kwargs: Any) -> dict[str, Any]:
        raise OriginalWorkflowError("original bridge failure")

    with pytest.raises(OriginalWorkflowError, match="original bridge failure"):
        run_acceptance(
            config_path=config,
            input_root=source_root,
            work_root=tmp_path / "work",
            evidence_root=tmp_path / "evidence",
            max_cases=1,
            max_file_bytes=100,
            max_total_bytes=100,
            bridge_settle_seconds=0,
            _com_factory=lambda _prog_id, *, startup_wait_seconds: cast(
                ComAutoCADAdapter, fake_com
            ),
            _case_workflow=failing_workflow,
            _bridge_loader=lambda _com: None,
            _sleep=lambda _seconds: None,
            _run_id_factory=lambda: "run-failuretest",
        )
    assert len(cleanup_calls) == 1


def test_main_renders_structured_safe_harness_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise ComCallFailedError(
            "Isolated AutoCAD startup helper failed",
            details={
                "reason": "isolated_startup_failed",
                "failure_stage": "rot_discovery",
            },
        )

    monkeypatch.setattr(corpus_acceptance, "run_acceptance", fail)

    exit_code = corpus_acceptance.main(
        [
            "--config",
            str(tmp_path / "config.yaml"),
            "--input-root",
            str(tmp_path / "input"),
            "--work-root",
            str(tmp_path / "work"),
            "--evidence-root",
            str(tmp_path / "evidence"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "COM_CALL_FAILED"
    assert payload["error"]["details"] == {
        "reason": "isolated_startup_failed",
        "failure_stage": "rot_discovery",
    }
