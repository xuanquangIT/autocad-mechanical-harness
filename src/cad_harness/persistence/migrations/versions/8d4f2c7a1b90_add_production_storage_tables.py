"""add production storage tables

Revision ID: 8d4f2c7a1b90
Revises: c2245193093b
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d4f2c7a1b90"
down_revision: str | None = "c2245193093b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "writer_leases",
        sa.Column("lease_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("lease_id"),
        sa.UniqueConstraint("document_id", name="uq_writer_lease_document"),
    )
    op.create_table(
        "takeoff_reports",
        sa.Column("report_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.String(length=80), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("total_mass_kg", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index("ix_takeoff_reports_document_id", "takeoff_reports", ["document_id"])
    op.create_table(
        "drawing_audits",
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.String(length=80), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("info_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index("ix_drawing_audits_document_id", "drawing_audits", ["document_id"])
    op.create_table(
        "effort_records",
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("harness_minutes", sa.Float(), nullable=False),
        sa.Column("idle_minutes_excluded", sa.Float(), nullable=False),
        sa.Column("manual_fixup_minutes", sa.Float(), nullable=False),
        sa.Column("spec_change_count", sa.Integer(), nullable=False),
        sa.Column("entities_created", sa.Integer(), nullable=False),
        sa.Column("entities_manually_edited", sa.Integer(), nullable=False),
        sa.Column("first_preview_clean", sa.Boolean(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"]),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index("ix_effort_records_case_id", "effort_records", ["case_id"])
    op.create_index("ix_effort_records_job_id", "effort_records", ["job_id"])
    op.create_table(
        "baseline_cases",
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("capability_group", sa.String(length=1), nullable=False),
        sa.Column("work_label", sa.String(length=32), nullable=False),
        sa.Column("manual_minutes", sa.Float(), nullable=False),
        sa.Column("manual_measured_by", sa.String(length=128), nullable=False),
        sa.Column("manual_measurement_biased", sa.Boolean(), nullable=False),
        sa.Column("manual_measured_in_single_session", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("case_id"),
    )
    op.create_table(
        "operation_metrics",
        sa.Column("metric_id", sa.String(length=64), nullable=False),
        sa.Column("operation_name", sa.String(length=128), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("metric_id"),
    )
    op.create_index("ix_operation_metrics_operation_name", "operation_metrics", ["operation_name"])


def downgrade() -> None:
    op.drop_table("operation_metrics")
    op.drop_table("baseline_cases")
    op.drop_table("effort_records")
    op.drop_table("drawing_audits")
    op.drop_table("takeoff_reports")
    op.drop_table("writer_leases")
