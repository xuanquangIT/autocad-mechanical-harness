from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from scripts.live_session_preflight import issue_existing_live_session_proof

from cad_harness.application.live_session_proof import verify_live_session_proof
from cad_harness.domain.errors import AdapterCapabilityMissingError
from cad_harness.domain.models.document import DocumentSnapshot
from cad_harness.domain.ports.autocad_adapter import AdapterStatus


class RecordingLiveAdapter:
    def __init__(self, *, available: bool = True, process_id: int | None = 9260) -> None:
        self.available = available
        self.process_id = process_id
        self.disconnected = False

    def connect(self, *, launch_if_missing: bool) -> None:
        assert launch_if_missing is False

    def disconnect(self) -> None:
        self.disconnected = True

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            adapter_type="com",
            available=self.available,
            active_document_id="doc-live",
            process_id=self.process_id,
        )

    def inspect_document(self, _request: object) -> DocumentSnapshot:
        return DocumentSnapshot(
            document_id="doc-live",
            revision="sha256:live",
            path_hash="sha256:path",
            display_name="redacted.dwg",
            units="mm",
        )


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "live.yaml"
    path.write_text("adapter:\n  type: com\n", encoding="utf-8")
    return path


def test_preflight_issues_exact_proof_with_read_only_adapter_then_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAD_HARNESS_ADAPTER", raising=False)
    adapter = RecordingLiveAdapter()
    factory_arguments: dict[str, Any] = {}

    def factory(adapter_type: str, **kwargs: Any) -> Any:
        factory_arguments.update(adapter_type=adapter_type, **kwargs)
        return adapter

    token = issue_existing_live_session_proof(
        config_path=_config(tmp_path),
        adapter_type="com",
        secret="test-secret",
        adapter_factory=factory,
    )

    assert factory_arguments["write_enabled"] is False
    assert adapter.disconnected is True
    verify_live_session_proof(
        token,
        "test-secret",
        adapter_type="com",
        process_id=9260,
        document_id="doc-live",
        revision="sha256:live",
        company_profile="demo-profile@1.0",
    )


@pytest.mark.parametrize(("available", "process_id"), [(False, 9260), (True, None)])
def test_unproven_live_identity_fails_closed_and_still_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    process_id: int | None,
) -> None:
    monkeypatch.delenv("CAD_HARNESS_ADAPTER", raising=False)
    adapter = RecordingLiveAdapter(available=available, process_id=process_id)

    with pytest.raises(AdapterCapabilityMissingError):
        issue_existing_live_session_proof(
            config_path=_config(tmp_path),
            adapter_type="com",
            secret="test-secret",
            adapter_factory=lambda *_args, **_kwargs: adapter,  # type: ignore[arg-type]
        )

    assert adapter.disconnected is True
