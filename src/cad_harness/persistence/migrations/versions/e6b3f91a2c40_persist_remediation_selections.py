"""Persist immutable remediation selection evidence.

Revision ID: e6b3f91a2c40
Revises: 4a7c19e2d8f1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b3f91a2c40"
down_revision: str | None = "4a7c19e2d8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remediation_selections",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=80), nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"]),
        sa.PrimaryKeyConstraint("job_id"),
    )


def downgrade() -> None:
    op.drop_table("remediation_selections")
