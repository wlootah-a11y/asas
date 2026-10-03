"""Platform-topic uniqueness, on every database whatever line it came up.

Two lines of this chain ran in the field before they were joined. Upstream
0.16.1 added ``0005``, the partial unique index on ``notification_topic.key``
WHERE ``org_id IS NULL``. The opaque-identity line (0.17 to 0.19) numbered its
own migrations ``0005`` to ``0010`` without it. Joined, the chain runs upstream's
``0005`` first, but a database stamped ``0010`` by the other line is already past
it in Alembic's eyes and would never build the index.

So this revision repeats ``0005``'s work, and it is safe to repeat: the
duplicate collapse deletes nothing on a database that has no duplicates, and the
index is created only when it is missing. On a database that came up the
upstream line it is a no-op.

**Downgrade is deliberately a no-op.** On the upstream line the index belongs to
``0005`` and dropping it here would take away what that revision still claims to
have applied. On the other line, leaving an index that enforces a real invariant
in place is the safer failure.

**Known limit.** A database stamped at the other line's own ``0005`` (opaque
identity applied, nothing after) cannot be told apart from upstream's ``0005``
and would re-run the identity widening. No released version stopped there; hosts
that upgraded went on to ``0010``.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "notification_topic"
_NAME = "uq_notification_topic_platform_key"


def _existing() -> set:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    if _NAME in _existing():
        return
    # Newest platform row per key wins, the same tie-break as 0005.
    op.execute(
        sa.text(
            f"DELETE FROM {_TABLE} WHERE org_id IS NULL AND id NOT IN "
            f"(SELECT MAX(id) FROM {_TABLE} WHERE org_id IS NULL GROUP BY key)"
        )
    )
    op.create_index(
        _NAME,
        _TABLE,
        ["key"],
        unique=True,
        postgresql_where=sa.text("org_id IS NULL"),
        sqlite_where=sa.text("org_id IS NULL"),
    )


def downgrade() -> None:
    return None
