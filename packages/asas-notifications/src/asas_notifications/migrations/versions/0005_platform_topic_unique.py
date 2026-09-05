"""Enforce platform-topic uniqueness at the database.

``uq_notification_topic_org_key`` (org_id, key) never arbitrates *platform*
rows: their ``org_id`` is NULL, and SQL NULLs compare distinct, so the
constraint admits any number of platform rows sharing a key. That turns every
host's topic seeding into an unguardable read-then-write — two replicas
booting at once both find no row and both insert, and policy resolution keyed
on the topic then has two rows to answer from. The claim has to be a
uniqueness rule the database arbitrates: a partial unique index on ``key``
WHERE ``org_id IS NULL`` closes the gap, and a seeder keeps check-then-insert
as the fast path with an ``IntegrityError`` catch for the race it loses.

Duplicates already present are collapsed first — newest row per key wins, the
package's usual tie-break (an admin's latest change must take effect) —
because the unique build would otherwise fail on exactly the databases that
need it. Nothing references a topic row by id (policy rows key on the string),
so dropping the older twins orphans nothing.

Plain (non-CONCURRENTLY) index DDL, deliberately: unlike 0003's composites on
the hot ``notification`` table, ``notification_topic`` holds a handful of
config rows written at seed time and on admin edits, so the brief lock a
plain build takes cannot stall a producer. The create/drop is still guarded
by an existence check so a partially-applied run retries cleanly.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "notification_topic"
_NAME = "uq_notification_topic_platform_key"


def _existing() -> set:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    op.execute(
        sa.text(
            f"DELETE FROM {_TABLE} WHERE org_id IS NULL AND id NOT IN "
            f"(SELECT MAX(id) FROM {_TABLE} WHERE org_id IS NULL GROUP BY key)"
        )
    )
    if _NAME not in _existing():
        op.create_index(
            _NAME,
            _TABLE,
            ["key"],
            unique=True,
            postgresql_where=sa.text("org_id IS NULL"),
            sqlite_where=sa.text("org_id IS NULL"),
        )


def downgrade() -> None:
    if _NAME in _existing():
        op.drop_index(_NAME, table_name=_TABLE)
