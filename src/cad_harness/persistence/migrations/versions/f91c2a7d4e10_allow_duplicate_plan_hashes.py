"""Allow canonical plan hashes to repeat across jobs.

Revision ID: f91c2a7d4e10
Revises: e6b3f91a2c40
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f91c2a7d4e10"
down_revision: str | None = "e6b3f91a2c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_LOOKUP_INDEX = "ix_plans_job_id_plan_hash"


def upgrade() -> None:
    # The initial SQLite schema created this unique constraint without an explicit
    # name. A naming convention lets Alembic address it during the batch table copy.
    with op.batch_alter_table("plans", naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint("uq_plans_plan_hash", type_="unique")
        batch_op.create_index(_LOOKUP_INDEX, ["job_id", "plan_hash"], unique=False)


def downgrade() -> None:
    # This intentionally fails rather than discarding data when duplicate hashes exist.
    # Operators must resolve duplicate rows before restoring the legacy invariant.
    with op.batch_alter_table("plans", naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_index(_LOOKUP_INDEX)
        batch_op.create_unique_constraint("uq_plans_plan_hash", ["plan_hash"])
