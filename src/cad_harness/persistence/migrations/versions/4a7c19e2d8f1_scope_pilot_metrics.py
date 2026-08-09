"""Scope pilot metrics to one immutable run.

Revision ID: 4a7c19e2d8f1
Revises: 8d4f2c7a1b90
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4a7c19e2d8f1"
down_revision: str | None = "8d4f2c7a1b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "baseline_cases_scoped",
        sa.Column("baseline_record_id", sa.String(length=260), nullable=False),
        sa.Column("case_id", sa.String(length=128), nullable=False),
        sa.Column("pilot_run_id", sa.String(length=128), nullable=False),
        sa.Column("capability_group", sa.String(length=1), nullable=False),
        sa.Column("work_label", sa.String(length=32), nullable=False),
        sa.Column("manual_minutes", sa.Float(), nullable=False),
        sa.Column("manual_measured_by", sa.String(length=128), nullable=False),
        sa.Column("manual_measurement_biased", sa.Boolean(), nullable=False),
        sa.Column("manual_measured_in_single_session", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("baseline_record_id"),
        sa.UniqueConstraint("pilot_run_id", "case_id", name="uq_baseline_run_case"),
    )
    op.execute(
        """
        INSERT INTO baseline_cases_scoped (
            baseline_record_id, case_id, pilot_run_id, capability_group, work_label,
            manual_minutes, manual_measured_by, manual_measurement_biased,
            manual_measured_in_single_session, created_at
        )
        SELECT
            'legacy:' || case_id, case_id, 'legacy', capability_group, work_label,
            manual_minutes, manual_measured_by, manual_measurement_biased,
            manual_measured_in_single_session, created_at
        FROM baseline_cases
        """
    )
    op.drop_table("baseline_cases")
    op.rename_table("baseline_cases_scoped", "baseline_cases")
    op.create_index("ix_baseline_cases_pilot_run_id", "baseline_cases", ["pilot_run_id"])

    for table_name in ("effort_records", "operation_metrics"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "pilot_run_id",
                    sa.String(length=128),
                    nullable=False,
                    server_default="legacy",
                )
            )
            batch_op.create_index(f"ix_{table_name}_pilot_run_id", ["pilot_run_id"])
    with op.batch_alter_table("effort_records") as batch_op:
        batch_op.add_column(sa.Column("failure_reason", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE effort_records SET failure_reason = 'unsupported_feature' WHERE completed = 0"
    )
    op.execute(
        """
        UPDATE effort_records
        SET pilot_run_id = 'legacy:' || record_id
        WHERE record_id NOT IN (
            SELECT MIN(record_id) FROM effort_records GROUP BY case_id
        )
        """
    )
    with op.batch_alter_table("effort_records") as batch_op:
        batch_op.create_unique_constraint("uq_effort_run_case", ["pilot_run_id", "case_id"])


def downgrade() -> None:
    with op.batch_alter_table("effort_records") as batch_op:
        batch_op.drop_constraint("uq_effort_run_case", type_="unique")
        batch_op.drop_column("failure_reason")
    for table_name in ("operation_metrics", "effort_records"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_pilot_run_id")
            batch_op.drop_column("pilot_run_id")
    op.create_table(
        "baseline_cases_legacy",
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
    op.execute(
        """
        INSERT INTO baseline_cases_legacy
        SELECT case_id, capability_group, work_label, manual_minutes, manual_measured_by,
               manual_measurement_biased, manual_measured_in_single_session, created_at
        FROM baseline_cases
        """
    )
    op.drop_table("baseline_cases")
    op.rename_table("baseline_cases_legacy", "baseline_cases")
