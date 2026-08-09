"""PySide6 approval window; credentials never enter widget state."""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

from apps.engineer_desktop.controller import EngineerDesktopController
from apps.engineer_desktop.view_model import ApprovalViewModel, DiffColor, thaw_json
from cad_harness.domain.errors import HarnessError
from cad_harness.domain.models.metrics import FailureReason
from cad_harness.domain.models.validation import Severity


def _review_json(view: ApprovalViewModel) -> str:
    """Render only decision evidence. Approval credentials are structurally absent."""
    payload = {
        "spec": thaw_json(view.spec_parameters),
        "missing_inputs": [
            {
                "path": item.path,
                "reason": item.reason,
                "accepted_formats": list(item.accepted_formats),
            }
            for item in view.missing_inputs
        ],
        "defaults": [
            {
                "path": item.path,
                "value": thaw_json(item.value),
                "source": item.source,
                "source_version": item.source_version,
                "reason": item.reason,
                "impact": item.impact,
                "override_allowed": item.override_allowed,
            }
            for item in view.defaults_applied
        ],
        "assumptions": [
            {
                "path": item.path,
                "statement": item.statement,
                "affects_geometry": item.affects_geometry,
                "requires_approval": item.requires_approval,
            }
            for item in view.assumptions
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def create_approval_window(
    controller: EngineerDesktopController,
    *,
    job_id: str,
    engineer_id: str,
) -> Any:
    """Create the real Qt window while keeping PySide6 an optional dependency."""
    try:
        qt_core = import_module("PySide6.QtCore")
        qt_gui = import_module("PySide6.QtGui")
        qt_widgets = import_module("PySide6.QtWidgets")
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised without the UI extra
        raise RuntimeError("Install the desktop extra with: uv sync --extra ui") from exc

    class ApprovalWindow(qt_widgets.QMainWindow):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
            super().__init__()
            self._controller = controller
            self._job_id = job_id
            self._engineer_id = engineer_id
            self._view: ApprovalViewModel | None = None
            self.setWindowTitle("AutoCAD Mechanical Harness — Engineer Approval")
            self.resize(1280, 820)

            root = qt_widgets.QWidget()
            layout = qt_widgets.QVBoxLayout(root)
            self.setCentralWidget(root)

            header = qt_widgets.QGridLayout()
            self.document_label = qt_widgets.QLabel()
            self.revision_label = qt_widgets.QLabel()
            self.state_label = qt_widgets.QLabel()
            self.hash_label = qt_widgets.QLabel()
            for widget, name in (
                (self.document_label, "documentLabel"),
                (self.revision_label, "revisionLabel"),
                (self.state_label, "stateLabel"),
                (self.hash_label, "planHashLabel"),
            ):
                widget.setObjectName(name)
            header.addWidget(self.document_label, 0, 0)
            header.addWidget(self.revision_label, 0, 1)
            header.addWidget(self.state_label, 1, 0)
            header.addWidget(self.hash_label, 1, 1)
            layout.addLayout(header)

            splitter = qt_widgets.QSplitter(qt_core.Qt.Orientation.Horizontal)
            layout.addWidget(splitter, 1)

            left_tabs = qt_widgets.QTabWidget()
            self.review_text = qt_widgets.QPlainTextEdit()
            self.review_text.setReadOnly(True)
            self.review_text.setObjectName("reviewEvidence")
            left_tabs.addTab(self.review_text, "Spec / Inputs / Defaults / Assumptions")
            self.before_text = qt_widgets.QPlainTextEdit()
            self.before_text.setReadOnly(True)
            self.before_text.setObjectName("beforePreview")
            self.after_text = qt_widgets.QPlainTextEdit()
            self.after_text.setReadOnly(True)
            self.after_text.setObjectName("afterPreview")
            preview_split = qt_widgets.QSplitter(qt_core.Qt.Orientation.Horizontal)
            preview_split.addWidget(self.before_text)
            preview_split.addWidget(self.after_text)
            left_tabs.addTab(preview_split, "Before / After Preview")
            splitter.addWidget(left_tabs)

            right_tabs = qt_widgets.QTabWidget()
            self.diff_table = qt_widgets.QTableWidget(0, 6)
            self.diff_table.setObjectName("semanticDiff")
            self.diff_table.setHorizontalHeaderLabels(
                ["Change", "Feature", "Entity", "Layer", "Target", "Summary"]
            )
            right_tabs.addTab(self.diff_table, "Semantic Diff")
            self.finding_table = qt_widgets.QTableWidget(0, 6)
            self.finding_table.setObjectName("validationFindings")
            self.finding_table.setHorizontalHeaderLabels(
                ["Rule", "Severity", "Expected", "Actual", "Tolerance", "Acknowledge"]
            )
            self.finding_table.itemChanged.connect(self._update_gate)
            right_tabs.addTab(self.finding_table, "Validation")
            splitter.addWidget(right_tabs)

            controls = qt_widgets.QHBoxLayout()
            self.status_label = qt_widgets.QLabel("Not approved")
            self.status_label.setObjectName("approvalStatus")
            controls.addWidget(self.status_label, 1)
            self.effort_start_button = qt_widgets.QPushButton("Start effort")
            self.effort_start_button.clicked.connect(self._start_effort)
            controls.addWidget(self.effort_start_button)
            self.effort_stop_button = qt_widgets.QPushButton("Stop effort")
            self.effort_stop_button.setEnabled(False)
            self.effort_stop_button.clicked.connect(self._stop_effort)
            controls.addWidget(self.effort_stop_button)
            self.fixup_minutes = qt_widgets.QDoubleSpinBox()
            self.fixup_minutes.setRange(0.0, 1440.0)
            self.fixup_minutes.setDecimals(1)
            self.fixup_minutes.setSuffix(" min fix-up")
            controls.addWidget(self.fixup_minutes)
            self.fixup_button = qt_widgets.QPushButton("Record fix-up")
            self.fixup_button.clicked.connect(self._record_fixup)
            controls.addWidget(self.fixup_button)
            self.edited_entities = qt_widgets.QSpinBox()
            self.edited_entities.setRange(0, 1_000_000)
            self.edited_entities.setSuffix(" edited entities")
            self.edited_entities.setEnabled(self._controller.pilot_effort_enabled)
            controls.addWidget(self.edited_entities)
            self.finalize_pilot_button = qt_widgets.QPushButton("Finalize pilot effort")
            self.finalize_pilot_button.setEnabled(False)
            self.finalize_pilot_button.clicked.connect(self._finalize_pilot_effort)
            controls.addWidget(self.finalize_pilot_button)
            self.failure_reason = qt_widgets.QComboBox()
            for reason in FailureReason:
                if reason is not FailureReason.MISSING_EFFORT_RECORD:
                    self.failure_reason.addItem(reason.value, reason)
            self.failure_reason.setEnabled(self._controller.pilot_effort_enabled)
            controls.addWidget(self.failure_reason)
            self.finalize_failed_button = qt_widgets.QPushButton("Finalize failed case")
            self.finalize_failed_button.setEnabled(self._controller.pilot_effort_enabled)
            self.finalize_failed_button.clicked.connect(self._finalize_failed_pilot_effort)
            controls.addWidget(self.finalize_failed_button)
            self.refresh_button = qt_widgets.QPushButton("Regenerate preview")
            self.refresh_button.clicked.connect(self.refresh)
            controls.addWidget(self.refresh_button)
            self.reject_button = qt_widgets.QPushButton("Reject")
            self.reject_button.clicked.connect(self._reject)
            controls.addWidget(self.reject_button)
            self.approve_button = qt_widgets.QPushButton("Approve")
            self.approve_button.setObjectName("approveButton")
            self.approve_button.clicked.connect(self._approve)
            controls.addWidget(self.approve_button)
            self.commit_button = qt_widgets.QPushButton("Commit approved plan")
            self.commit_button.setEnabled(False)
            self.commit_button.clicked.connect(self._commit)
            controls.addWidget(self.commit_button)
            self.rollback_review_button = qt_widgets.QPushButton("Review rollback")
            self.rollback_review_button.setObjectName("rollbackReviewButton")
            self.rollback_review_button.setEnabled(False)
            self.rollback_review_button.clicked.connect(self._review_rollback)
            controls.addWidget(self.rollback_review_button)
            self.rollback_execute_button = qt_widgets.QPushButton("Execute approved rollback")
            self.rollback_execute_button.setObjectName("rollbackExecuteButton")
            self.rollback_execute_button.setEnabled(False)
            self.rollback_execute_button.clicked.connect(self._execute_rollback)
            controls.addWidget(self.rollback_execute_button)
            layout.addLayout(controls)
            self.refresh()
            self._scope_timer = qt_core.QTimer(self)
            self._scope_timer.setInterval(1000)
            self._scope_timer.timeout.connect(self._update_gate)
            self._scope_timer.start()

        def _start_effort(self) -> None:
            try:
                self._controller.effort_session.start_activity()
            except ValueError as error:
                self.status_label.setText(str(error))
                return
            self.effort_start_button.setEnabled(False)
            self.effort_stop_button.setEnabled(True)
            self.fixup_button.setEnabled(False)
            self.status_label.setText("Engineer effort timing started")

        def _stop_effort(self) -> None:
            try:
                self._controller.effort_session.stop_activity()
            except ValueError as error:
                self.status_label.setText(str(error))
                return
            self.effort_start_button.setEnabled(True)
            self.effort_stop_button.setEnabled(False)
            self.fixup_button.setEnabled(True)
            self.status_label.setText("Engineer effort timing stopped")

        def _record_fixup(self) -> None:
            if self._controller.effort_session.active:
                self.status_label.setText("Stop active effort timing before recording fix-up")
                return
            self._controller.effort_session.add_manual_fixup(self.fixup_minutes.value())
            self.fixup_minutes.setValue(0.0)
            self.status_label.setText("Manual fix-up effort recorded locally")

        def _finalize_pilot_effort(self) -> None:
            try:
                record = self._controller.finalize_pilot_effort(
                    job_id=self._job_id,
                    entities_manually_edited=self.edited_entities.value(),
                )
            except (ValueError, HarnessError) as error:
                self.status_label.setText(f"Pilot effort not saved: {error}")
                return
            self.finalize_pilot_button.setEnabled(False)
            self.effort_start_button.setEnabled(False)
            self.effort_stop_button.setEnabled(False)
            self.fixup_button.setEnabled(False)
            self.failure_reason.setEnabled(False)
            self.finalize_failed_button.setEnabled(False)
            self.status_label.setText(f"Pilot effort saved: {record.record_id}")

        def _finalize_failed_pilot_effort(self) -> None:
            reason = self.failure_reason.currentData()
            if not isinstance(reason, FailureReason):
                self.status_label.setText("Pilot effort not saved: select a failure reason")
                return
            try:
                record = self._controller.finalize_failed_pilot_effort(
                    job_id=self._job_id,
                    failure_reason=reason,
                    entities_manually_edited=self.edited_entities.value(),
                )
            except (ValueError, HarnessError) as error:
                self.status_label.setText(f"Pilot effort not saved: {error}")
                return
            self.finalize_pilot_button.setEnabled(False)
            self.finalize_failed_button.setEnabled(False)
            self.failure_reason.setEnabled(False)
            self.effort_start_button.setEnabled(False)
            self.effort_stop_button.setEnabled(False)
            self.fixup_button.setEnabled(False)
            self.status_label.setText(
                f"Failed pilot effort saved ({reason.value}): {record.record_id}"
            )

        def refresh(self) -> None:
            self._controller.clear_approval()
            self._controller.clear_rollback_approval()
            self.commit_button.setEnabled(False)
            self.rollback_execute_button.setEnabled(False)
            self._view = self._controller.refresh(self._job_id)
            self.rollback_review_button.setEnabled(
                self._controller.rollback_is_available(self._job_id)
            )
            view = self._view
            self.document_label.setText(f"Document: {view.document_id}")
            self.revision_label.setText(
                f"Revision: {view.revision} (current: {view.current_revision})"
            )
            self.state_label.setText(f"State: {view.state.value}")
            self.hash_label.setText(f"Plan: {view.plan_hash_prefix or 'not compiled'}")
            self.review_text.setPlainText(_review_json(view))
            self.before_text.setPlainText(
                "\n".join(item.artifact_ref for item in view.before_preview.artifacts)
            )
            self.after_text.setPlainText(
                "\n".join(item.artifact_ref for item in view.after_preview.artifacts)
            )
            self._populate_diff(view)
            self._populate_findings(view)
            self._update_gate()

        def _populate_diff(self, view: ApprovalViewModel) -> None:
            colors = {
                DiffColor.GREEN: qt_gui.QColor("#b7e4c7"),
                DiffColor.YELLOW: qt_gui.QColor("#ffe66d"),
                DiffColor.RED: qt_gui.QColor("#ffadad"),
            }
            self.diff_table.setRowCount(len(view.semantic_diff))
            for row, entry in enumerate(view.semantic_diff):
                values = (
                    entry.change,
                    entry.feature_id,
                    entry.entity_type,
                    entry.layer,
                    entry.target_entity_ref or "",
                    entry.summary,
                )
                for column, value in enumerate(values):
                    item = qt_widgets.QTableWidgetItem(str(value))
                    item.setBackground(colors[entry.color])
                    self.diff_table.setItem(row, column, item)

        def _populate_findings(self, view: ApprovalViewModel) -> None:
            self.finding_table.blockSignals(True)
            self.finding_table.setRowCount(len(view.findings))
            purple = qt_gui.QColor("#d8b4fe")
            for row, finding in enumerate(view.findings):
                values = (
                    finding.rule_id,
                    finding.severity.value,
                    finding.expected,
                    finding.actual,
                    finding.tolerance,
                )
                for column, value in enumerate(values):
                    item = qt_widgets.QTableWidgetItem(json.dumps(value, ensure_ascii=False))
                    item.setBackground(purple)
                    self.finding_table.setItem(row, column, item)
                acknowledge = qt_widgets.QTableWidgetItem("acknowledged")
                if finding.severity is Severity.WARNING:
                    acknowledge.setFlags(
                        acknowledge.flags() | qt_core.Qt.ItemFlag.ItemIsUserCheckable
                    )
                    acknowledge.setCheckState(qt_core.Qt.CheckState.Unchecked)
                else:
                    acknowledge.setFlags(acknowledge.flags() & ~qt_core.Qt.ItemFlag.ItemIsEnabled)
                self.finding_table.setItem(row, 5, acknowledge)
            self.finding_table.blockSignals(False)

        def _acknowledged_warnings(self) -> frozenset[str]:
            acknowledged: set[str] = set()
            view = self._view
            if view is None:
                return frozenset()
            for row, finding in enumerate(view.findings):
                item = self.finding_table.item(row, 5)
                if (
                    finding.severity is Severity.WARNING
                    and item is not None
                    and item.checkState() == qt_core.Qt.CheckState.Checked
                ):
                    acknowledged.add(finding.rule_id)
            return frozenset(acknowledged)

        def _update_gate(self, *_args: object) -> None:
            if self._controller.has_in_memory_approval:
                if self._controller.approval_scope_is_current():
                    self.approve_button.setEnabled(False)
                    self.commit_button.setEnabled(True)
                    return
                self._controller.clear_approval()
                self.approve_button.setEnabled(False)
                self.commit_button.setEnabled(False)
                self.status_label.setText("Approval invalidated: plan or revision changed")
                return
            eligibility = self._controller.eligibility(self._acknowledged_warnings())
            self.approve_button.setEnabled(eligibility.can_approve)
            self.commit_button.setEnabled(False)
            if eligibility.reasons:
                self.status_label.setText("Approval disabled: " + ", ".join(eligibility.reasons))
            elif not self._controller.has_in_memory_approval:
                self.status_label.setText("Ready for engineer approval")

        def _approve(self) -> None:
            try:
                outcome = self._controller.approve(
                    approved_by=self._engineer_id,
                    acknowledged_warning_rule_ids=self._acknowledged_warnings(),
                )
            except HarnessError as error:
                self.status_label.setText(f"Approval failed: {error.code.value}")
                self.approve_button.setEnabled(False)
                return
            if outcome.approved:
                self.status_label.setText(f"Approved: {outcome.approval_id}")
                self.approve_button.setEnabled(False)
                self.commit_button.setEnabled(True)
            else:
                self.status_label.setText("Approval disabled: " + ", ".join(outcome.reasons))
                self._update_gate()

        def _reject(self) -> None:
            self._controller.clear_approval()
            self.status_label.setText("Rejected by engineer")
            self.approve_button.setEnabled(False)
            self.commit_button.setEnabled(False)

        def _commit(self) -> None:
            from uuid import uuid4

            try:
                result = self._controller.commit_approved(idempotency_key=f"desktop-{uuid4().hex}")
            except HarnessError as error:
                self.status_label.setText(f"Commit failed: {error.code.value}")
                self.commit_button.setEnabled(False)
                self.rollback_review_button.setEnabled(
                    self._controller.rollback_is_available(self._job_id)
                )
                return
            self.status_label.setText(f"Committed revision: {result.new_revision}")
            self.commit_button.setEnabled(False)
            self.rollback_review_button.setEnabled(True)
            self.finalize_pilot_button.setEnabled(self._controller.pilot_effort_enabled)

        def _review_rollback(self) -> None:
            try:
                scope = self._controller.prepare_rollback(self._job_id)
            except HarnessError as error:
                self.status_label.setText(f"Rollback unavailable: {error.code.value}")
                self.rollback_review_button.setEnabled(False)
                return
            message = (
                "Rollback is destructive and discards every drawing change "
                "after the checkpoint.\n\n"
                f"Job: {scope.job_id}\n"
                f"Document: {scope.document_id}\n"
                f"Checkpoint: {scope.checkpoint_id}\n"
                f"Current revision: {scope.current_revision}\n\n"
                "Approve this exact restore scope?"
            )
            answer = qt_widgets.QMessageBox.question(
                self,
                "Approve destructive rollback",
                message,
                qt_widgets.QMessageBox.StandardButton.Yes
                | qt_widgets.QMessageBox.StandardButton.No,
                qt_widgets.QMessageBox.StandardButton.No,
            )
            if answer != qt_widgets.QMessageBox.StandardButton.Yes:
                self._controller.clear_rollback_approval()
                self.rollback_execute_button.setEnabled(False)
                self.status_label.setText("Rollback approval cancelled")
                return
            try:
                outcome = self._controller.approve_rollback(approved_by=self._engineer_id)
            except HarnessError as error:
                self.status_label.setText(f"Rollback approval failed: {error.code.value}")
                self.rollback_execute_button.setEnabled(False)
                return
            self.rollback_execute_button.setEnabled(outcome.approved)
            self.status_label.setText(f"Rollback approved: {outcome.approval_id}")

        def _execute_rollback(self) -> None:
            answer = qt_widgets.QMessageBox.question(
                self,
                "Execute destructive rollback",
                "Execute the separately approved rollback now? "
                "This cannot be undone by the harness.",
                qt_widgets.QMessageBox.StandardButton.Yes
                | qt_widgets.QMessageBox.StandardButton.No,
                qt_widgets.QMessageBox.StandardButton.No,
            )
            if answer != qt_widgets.QMessageBox.StandardButton.Yes:
                return
            try:
                result = self._controller.rollback_approved()
            except HarnessError as error:
                self.status_label.setText(f"Rollback failed: {error.code.value}")
                self.rollback_execute_button.setEnabled(False)
                return
            self.rollback_review_button.setEnabled(False)
            self.rollback_execute_button.setEnabled(False)
            self.status_label.setText(f"Rolled back to revision: {result.restored_revision}")

    return ApprovalWindow()


__all__ = ["create_approval_window"]
