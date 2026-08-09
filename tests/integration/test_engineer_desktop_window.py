"""Offscreen Qt acceptance for the human approval surface."""

from __future__ import annotations

from typing import Any

from apps.engineer_desktop.approval_window import create_approval_window
from apps.engineer_desktop.controller import EngineerDesktopController
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTableWidget

from cad_harness.adapters.fake import FakeAutoCADAdapter
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.domain.models.validation import ValidationStage


def test_window_renders_decision_evidence_and_never_copies_or_renders_token(
    service: HarnessService,
    adapter: FakeAutoCADAdapter,
    base_plate_spec: dict[str, Any],
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    job = service.create_job()
    service.submit_spec(job.job_id, base_plate_spec)
    service.preview(job.job_id)
    service.validate(job.job_id, ValidationStage.PRE_COMMIT)

    controller = EngineerDesktopController(service)
    sentinel = "approval_test.SUPER_SECRET_TOKEN"

    def approve(
        job_id: str,
        engineer: str,
        acknowledged: tuple[str, ...],
        *,
        displayed_plan_hash: str | None = None,
        displayed_revision: str | None = None,
    ):
        assert job_id == job.job_id
        assert engineer == "engineer-qt"
        assert acknowledged == ("STD-PROFILE-PROVENANCE",)
        assert displayed_plan_hash is not None
        assert displayed_revision == job.expected_revision
        return "approval_test", sentinel

    monkeypatch.setattr(service, "approve", approve)
    clipboard = application.clipboard()
    clipboard.setText("clipboard-marker")
    window = create_approval_window(
        controller,
        job_id=job.job_id,
        engineer_id="engineer-qt",
    )
    window.show()
    application.processEvents()

    assert job.document_id in window.document_label.text()
    assert job.expected_revision in window.revision_label.text()
    assert not window.approve_button.isEnabled()
    findings = window.findChild(QTableWidget, "validationFindings")
    assert findings is not None
    acknowledgement = findings.item(0, 5)
    acknowledgement.setCheckState(Qt.CheckState.Checked)
    application.processEvents()
    assert window.approve_button.isEnabled()

    window.approve_button.click()
    application.processEvents()
    assert controller.has_in_memory_approval
    assert clipboard.text() == "clipboard-marker"
    rendered: list[str] = []
    for widget in application.allWidgets():
        text_method = getattr(widget, "text", None)
        if callable(text_method):
            rendered.append(str(text_method()))
        if isinstance(widget, QPlainTextEdit):
            rendered.append(widget.toPlainText())
        rendered.append(widget.accessibleName())
        rendered.append(widget.accessibleDescription())
    assert sentinel not in " ".join(rendered)
    assert "approval_test" in window.status_label.text()

    adapter.document.write_counter += 1
    window._update_gate()
    assert not controller.has_in_memory_approval
    assert not window.commit_button.isEnabled()
    assert "invalidated" in window.status_label.text().lower()
    window.close()
