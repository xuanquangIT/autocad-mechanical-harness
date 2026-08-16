"""Persist the exact approved layer for committed entity mappings.

Revision ID: a41d6b8c9e20
Revises: f91c2a7d4e10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a41d6b8c9e20"
down_revision: str | None = "f91c2a7d4e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("entity_mappings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("expected_layer", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("entity_mappings", schema=None) as batch_op:
        batch_op.drop_column("expected_layer")
