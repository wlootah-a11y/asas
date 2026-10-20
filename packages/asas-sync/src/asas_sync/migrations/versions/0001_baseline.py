"""Baseline: the cursor and the seen-marks.

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "asas_sync_cursor",
        sa.Column("org_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("resource", sa.String(64), nullable=False),
        sa.Column("watermark", sa.DateTime(), nullable=True),
        sa.Column("last_key", sa.String(128), nullable=True),
        sa.Column("last_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("backfilled_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("org_id", "resource"),
    )
    op.create_table(
        "asas_sync_seen",
        sa.Column("org_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("resource", sa.String(64), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("org_id", "resource", "key"),
    )
    op.create_index("ix_asas_sync_seen_unseen", "asas_sync_seen", ["org_id", "resource", "seen_at"])


def downgrade() -> None:
    op.drop_index("ix_asas_sync_seen_unseen", table_name="asas_sync_seen")
    op.drop_table("asas_sync_seen")
    op.drop_table("asas_sync_cursor")
