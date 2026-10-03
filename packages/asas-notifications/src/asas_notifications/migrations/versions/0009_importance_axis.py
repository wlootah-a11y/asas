"""``urgency`` becomes ``importance`` with two rungs, and ``nature`` leaves.

Two changes to the same idea: this package routes on ``topic`` and one loudness
axis, and it now says so in one word with no unusable value in it.

**The rung that decided nothing.** ``urgency`` was ``low | normal | high`` and
the built-in fallback under the policy table was literally ``urgency is not
Urgency.low``: ``normal`` and ``high`` selected the same channels. An
administrator could only ever have told them apart by writing two policy cells
that said one thing, so the rung is gone and the axis is ``low | high``.

- **Notification ROWS fold UP**, ``normal`` to ``high``. Their loudness is spent
  by the time it is stored (routing already happened), and such a row DID email
  somebody, so reading it back as ``high`` is what actually happened to it.
  Reading it as ``low`` would be a false claim about the past.
- **Policy CELLS are DELETED**, not folded, and the count is logged. Folding
  would silently widen a rule somebody wrote for one rung; leaving it would put
  a value the vocabulary no longer offers into a table an administrator reads.
  A rule that has to be re-stated is a smaller problem than one that quietly
  changed meaning, which is the same call ``0007`` made about a rule it could no
  longer express.

**``nature`` leaves the package.** It described what a notification asks of its
recipient, which is PRESENTATION: it stopped being a routing condition in
``0007`` and has decided nothing since. A host that renders its own feed is the
side that knows how it wants to render it, so the column moves to the host, on
its own sidecar row keyed by ``notification_id``. **A host that wants to keep
those values must copy them out BEFORE this revision runs**, which is the one
thing this migration cannot do for it: it does not know where the host would
like them.

Dropped rather than left nullable. A column nothing writes and nothing reads is
a field that looks available and is always empty, which is worse for the next
reader than its absence (``0008``'s reasoning, applied again).

**A HOST WITH ROW LEVEL SECURITY ON THESE TABLES MUST FOLD THE ROWS ITSELF, and
this migration says so out loud rather than quietly doing nothing.** A migration
runs unauthenticated by nature. Where the host has put a policy on
``notification`` that reads a session variable (a tenant GUC, say) and forced it
on the owner too, the fold below matches ZERO rows and reports success: every
retired value survives, and the host's next read hits a value its vocabulary no
longer has. That is not something this package can fix, since the policy is the
host's and lifting it here would be a security decision made in the wrong
file. So the fold is attempted, the result is COUNTED, and a leftover is a
loud warning naming the fix. It was found by rehearsing this migration against a
clone of a real host's database, where every ``normal`` row came through
untouched.

**The stored width is left alone.** After the rename the column is still the
``VARCHAR(6)`` that "normal" needed, where the model now declares four. A
narrower type buys nothing (the values are validated in code) and on Postgres
would mean a full table rewrite of the largest table this package owns.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)

_FEED = "notification"
_POLICY = "notification_channel_policy"
#: The retired rung. Spelled here rather than imported from the models, on the
#: standing migration rule: a revision describes the database at a point in
#: time, and importing a constant that later changes would make this file mean
#: something different than it did when it ran.
_RETIRED = "normal"


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

    # ── the data, while the column still has its old name ─────────────────────
    folded = conn.execute(
        sa.text(f"UPDATE {_FEED} SET urgency = 'high' WHERE urgency = :u"),
        {"u": _RETIRED},
    ).rowcount
    if folded:
        logger.info(
            "asas-notifications 0009 folded %s notification row(s) from the "
            "retired '%s' rung up to 'high': the built-in rule already routed "
            "them that way",
            folded,
            _RETIRED,
        )

    doomed = conn.execute(
        sa.text(f"SELECT count(*) FROM {_POLICY} WHERE urgency = :u"), {"u": _RETIRED}
    ).scalar()
    if doomed:
        conn.execute(sa.text(f"DELETE FROM {_POLICY} WHERE urgency = :u"), {"u": _RETIRED})
        logger.warning(
            "asas-notifications 0009 removed %s channel policy cell(s) on the "
            "retired '%s' rung; the axis is importance (low | high) now, and "
            "such a cell can no longer match an emit. Re-state them against "
            "'low' or 'high' if they are still wanted",
            doomed,
            _RETIRED,
        )

    # ── the schema ────────────────────────────────────────────────────────────
    # (Verified after the rename, below: a host policy can hide rows from the
    # fold above, and silence is the one outcome that must not pass.)
    if _dialect() == "sqlite":
        # One rebuild for both changes on the feed table: SQLite cannot drop a
        # column in place, and doing the rename in the same batch means the
        # table is copied once instead of twice.
        with op.batch_alter_table(
            _FEED, schema=None, copy_from=_live_without_checks(conn, _FEED)
        ) as batch:
            batch.alter_column(
                "urgency", new_column_name="importance", existing_type=sa.String(6)
            )
            batch.drop_column("nature")
        with op.batch_alter_table(
            _POLICY, schema=None, copy_from=_live_without_checks(conn, _POLICY)
        ) as batch:
            batch.alter_column(
                "urgency", new_column_name="importance", existing_type=sa.String(6)
            )
    else:
        op.alter_column(
            _FEED, "urgency", new_column_name="importance", existing_type=sa.String(6)
        )
        op.drop_column(_FEED, "nature")
        op.alter_column(
            _POLICY, "urgency", new_column_name="importance", existing_type=sa.String(6)
        )

    _warn_if_the_fold_was_invisible(conn)


def _warn_if_the_fold_was_invisible(conn) -> None:
    """Say so when rows still hold the retired rung after the fold.

    The only way this happens is a host policy filtering the UPDATE (see the
    module docstring), and the host is the only side that can act on it: it must
    re-run the fold with its own policy lifted, as a host with a filtering
    policy does in its own migration. A warning rather than a raise, because failing the boot of a
    host that cannot fix it mid-migration trades one broken state for another,
    and the rows are readable either way.
    """
    left = conn.execute(
        sa.text(f"SELECT count(*) FROM {_FEED} WHERE importance = :u"),
        {"u": _RETIRED},
    ).scalar()
    if left:
        logger.warning(
            "asas-notifications 0009 could not fold %s notification row(s) off "
            "the retired '%s' rung: this migration binds no session, so a row "
            "level security policy on %r hides them from it. Fold them with the "
            "policy lifted (UPDATE %s SET importance = 'high' WHERE importance = "
            "'%s'), or reads will return a value the vocabulary no longer has.",
            left,
            _RETIRED,
            _FEED,
            _FEED,
            _RETIRED,
        )


def downgrade() -> None:
    """Restores the columns, NOT the values.

    Three things cannot come back, and each is a consequence of the upgrade
    rather than an oversight here:

    * the ``nature`` of every row, which this package no longer holds. The
      column is restored as NOT NULL with every row reading ``info``, the rung
      that asks nothing of its recipient: a default, not a recovery.
    * the policy cells the upgrade deleted, which cannot be invented back, and a
      downgrade must not guess which rung an operator meant. That is the whole
      reason the upgrade deleted them rather than folding them.
    * which ``high`` rows were once ``normal``. The fold is not reversible: they
      are the same value now, and they were routed identically before.
    """
    conn = op.get_bind()
    nature = sa.Enum("action", "info", "warning", name="nature", native_enum=False)

    if _dialect() == "sqlite":
        with op.batch_alter_table(
            _POLICY, schema=None, copy_from=_live_without_checks(conn, _POLICY)
        ) as batch:
            batch.alter_column(
                "importance", new_column_name="urgency", existing_type=sa.String(6)
            )
        # Three steps, and not the Postgres branch's one: SQLite rebuilds the
        # table by copying it, and the copy's INSERT names the columns it is
        # carrying over, so a brand-new NOT NULL column is not covered by its own
        # DEFAULT and the copy fails. Add it nullable, fill it, then tighten it
        # (the ``0008`` downgrade's pattern).
        with op.batch_alter_table(
            _FEED, schema=None, copy_from=_live_without_checks(conn, _FEED)
        ) as batch:
            batch.alter_column(
                "importance", new_column_name="urgency", existing_type=sa.String(6)
            )
            batch.add_column(sa.Column("nature", nature, nullable=True))
        op.execute(f"UPDATE {_FEED} SET nature = 'info' WHERE nature IS NULL")
        with op.batch_alter_table(
            _FEED, schema=None, copy_from=_live_without_checks(conn, _FEED)
        ) as batch:
            batch.alter_column("nature", existing_type=nature, nullable=False)
    else:
        op.alter_column(
            _POLICY, "importance", new_column_name="urgency", existing_type=sa.String(6)
        )
        op.alter_column(
            _FEED, "importance", new_column_name="urgency", existing_type=sa.String(6)
        )
        # The value comes from the column DEFINITION rather than a following
        # UPDATE: metadata-only on PG 11+, and it leaves no window in which the
        # column exists and is NULL.
        op.add_column(
            _FEED, sa.Column("nature", nature, nullable=False, server_default="info")
        )
        op.alter_column(_FEED, "nature", server_default=None)
