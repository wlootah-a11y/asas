"""``reason`` leaves the notification row.

It answered "why THIS recipient" — GitHub's participating-vs-watching,
generalised — and it was a required axis on every emit, a stored column, a field
on the read schema and a field on ``DeliveryPayload``. It never chose a channel:
routing keys on topic and urgency, and this was reserved for the preference
layer that has not been built.

**What goes with it.** The axis a person would have narrowed on ("stop emailing
me about things I am only watching") loses its stored basis. That preference was
never implemented, so nothing working stops working — but the option is
foreclosed rather than postponed, and re-adding the column later cannot
reconstruct the value for rows already written.

Dropped rather than made nullable. A nullable column nothing writes and nothing
reads is a field that looks available and is always empty, which is worse for
the next reader than its absence.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "notification"


def _live_without_checks(conn, table: str) -> sa.Table:
    """The live table, reflected WITH its indexes, minus its CHECK constraints.

    Three requirements collide on the SQLite rebuild path, and getting any one
    wrong fails loudly except the third, which fails silently:

    * describing the new table from the MODEL selects columns the old table has
      and the new one does not, so the copy step errors;
    * carrying a reflected CHECK along breaks the CREATE when that CHECK names
      the column being dropped — SQLite validates the expression against the new
      table's columns as it creates them;
    * describing the table by its COLUMNS ALONE loses every index, and a rebuild
      that quietly drops the feed indexes is a performance regression nothing
      would notice until production.

    So: reflect everything (columns, primary key, indexes), then discard only
    the CHECKs.
    """
    live = sa.Table(table, sa.MetaData(), autoload_with=conn)
    for constraint in list(live.constraints):
        if isinstance(constraint, sa.CheckConstraint):
            live.constraints.discard(constraint)
    return live

def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, schema=None, copy_from=_live_without_checks(conn, _TABLE)) as batch:
            batch.drop_column("reason")
    else:
        op.drop_column(_TABLE, "reason")


def downgrade() -> None:
    """Restores the column, NOT the values: they are gone, and there is nothing
    to derive them from. ``participant`` is the honest backfill — it was the
    common case and the one a caller omitting the axis would most likely have
    meant — but it is a default, not a recovery, and rows written before this
    migration cannot be told apart from rows written after it."""
    conn = op.get_bind()
    reason = sa.Enum("requested", "participant", "watching", name="reason", native_enum=False)
    if conn.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, schema=None, copy_from=_live_without_checks(conn, _TABLE)) as batch:
            batch.add_column(sa.Column("reason", reason, nullable=True))
    else:
        op.add_column(_TABLE, sa.Column("reason", reason, nullable=True))
    op.execute(f"UPDATE {_TABLE} SET reason = 'participant' WHERE reason IS NULL")
    if conn.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, schema=None, copy_from=_live_without_checks(conn, _TABLE)) as batch:
            batch.alter_column("reason", nullable=False)
    else:
        op.alter_column(_TABLE, "reason", nullable=False)
