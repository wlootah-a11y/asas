"""Routing becomes a real (topic × urgency) matrix, and ``nature`` leaves it.

**The cell that could not be written.** 0.16.0 carried a CHECK that allowed a
policy row to state a topic OR an axis condition, never both. So "interview
notifications, but only the urgent ones, go to email" had nowhere to live: the
nearest storable rules were "all interview notifications" or "all urgent
notifications", and neither is what an administrator meant. Worse, a topic row
was never compared against urgency at all, so the closest available rule applied
far more widely than intended, and nothing warned — the write was simply
rejected, or silently broader.

The two coordinates are independent now. NULL means "every value of this axis",
so ``(topic, urgency)`` reads: both set is one cell, one set is a row or column,
both NULL is the org-wide default. Resolution takes the most specific match.

**Dropping ``nature``.** It described what a notification asks of the recipient,
which is a different question from how loudly to deliver it, and urgency already
answers the second. Every rule expressible against nature was expressible
against urgency, so nothing becomes unsayable. It stays on the ``notification``
row, where it drives the UI treatment and the email subject — only its role as a
routing condition ends here.

**Rows that used it.** A row whose only condition was ``nature`` becomes a row
with no condition at all, which would quietly turn a narrow rule into an
org-wide default — the opposite of a safe no-op. Those rows are DELETED instead,
and the count is logged. A rule that has to be rewritten is a smaller problem
than one that silently widened.

Both directions are honest: the downgrade restores the column and the CHECK, and
cannot restore deleted rows or split a two-coordinate cell into a shape the old
constraint would accept, so it deletes those cells rather than corrupt them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "notification_channel_policy"
_CHECK = "ck_notification_channel_policy_one_condition"

logger = logging.getLogger(__name__)


def _dialect() -> str:
    return op.get_bind().dialect.name


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

    # Rules that lived only on the nature axis. Deleted, not widened: with the
    # column gone their condition would vanish and the row would begin matching
    # every notification.
    orphaned = conn.execute(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "
            f"WHERE nature IS NOT NULL AND topic IS NULL AND urgency IS NULL"
        )
    ).scalar()
    if orphaned:
        conn.execute(
            sa.text(
                f"DELETE FROM {_TABLE} "
                f"WHERE nature IS NOT NULL AND topic IS NULL AND urgency IS NULL"
            )
        )
        logger.warning(
            "asas-notifications 0007 removed %s channel policy row(s) whose only "
            "condition was nature; rewrite them against urgency if still wanted",
            orphaned,
        )

    if _dialect() == "sqlite":
        # SQLite cannot drop a column or a constraint in place: batch mode
        # rebuilds the table. ``copy_from`` is required, not optional — without
        # it alembic describes the new table from the MODEL, which has already
        # lost ``nature``, and the copy step then selects a column the old table
        # still has and the new one does not. Reflecting the live table is what
        # makes the rebuild describe what is actually there.
        #
        # Rebuilding also discards the old CHECK, which is the second half of
        # this migration and needs no separate statement here.
        with op.batch_alter_table(_TABLE, schema=None, copy_from=_live_without_checks(conn, _TABLE)) as batch:
            batch.drop_column("nature")
    else:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}")
        op.drop_column(_TABLE, "nature")


def downgrade() -> None:
    conn = op.get_bind()

    # A cell naming both coordinates cannot exist under the old CHECK. Keeping
    # it would leave a table the constraint rejects; silently blanking one
    # coordinate would change what the rule means. So it goes.
    doomed = conn.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE topic IS NOT NULL AND urgency IS NOT NULL")
    ).scalar()
    if doomed:
        conn.execute(
            sa.text(f"DELETE FROM {_TABLE} WHERE topic IS NOT NULL AND urgency IS NOT NULL")
        )
        logger.warning(
            "asas-notifications 0007 downgrade removed %s (topic, urgency) cell(s) "
            "the restored CHECK constraint cannot express",
            doomed,
        )
    # And the all-NULL default row, which the old CHECK also forbade.
    conn.execute(sa.text(f"DELETE FROM {_TABLE} WHERE topic IS NULL AND urgency IS NULL"))

    nature = sa.Enum("action", "info", name="nature", native_enum=False)
    if _dialect() == "sqlite":
        with op.batch_alter_table(_TABLE, schema=None, copy_from=_live_without_checks(conn, _TABLE)) as batch:
            batch.add_column(sa.Column("nature", nature, nullable=True))
            batch.create_check_constraint(
                _CHECK,
                "(topic IS NOT NULL AND urgency IS NULL AND nature IS NULL) OR "
                "(topic IS NULL AND (urgency IS NOT NULL OR nature IS NOT NULL))",
            )
    else:
        op.add_column(_TABLE, sa.Column("nature", nature, nullable=True))
        op.create_check_constraint(
            _CHECK,
            _TABLE,
            "(topic IS NOT NULL AND urgency IS NULL AND nature IS NULL) OR "
            "(topic IS NULL AND (urgency IS NOT NULL OR nature IS NOT NULL))",
        )
