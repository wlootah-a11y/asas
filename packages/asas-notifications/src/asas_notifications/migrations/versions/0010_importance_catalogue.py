"""``importance`` becomes a catalogue: rows an org extends, not an enum.

0.18.0 narrowed this axis to ``low | high`` and welded the survivors into the
column type. The narrowing was right and the welding was the overcorrection,
and the two got confused because one argument was made for both.

**The argument that held.** The built-in FALLBACK, the rule that applies when no
policy cell matches, can only say "in-app" or "in-app and email". Two rungs is
all it can tell apart, so a third rung buys the fallback nothing. True, and
unchanged by this revision.

**The argument that did not.** That was read as "two rungs is the whole of what
the routing can express", which is about the MATRIX, and the matrix is a table
an administrator writes cells into. ``(orders, critical) -> email`` was
always expressible; the only thing stopping it was that ``critical`` could not
be spelled. Meanwhile ``topic``, the other coordinate of the same matrix, was a
seeded table an org extends. The asymmetry had no reason behind it.

So this revision gives the second coordinate the shape the first one already
has, DR 0001's shared-with-overrides pattern: platform rows (``org_id NULL``)
seeded here, optional org override rows on top.

**``emails_by_default`` is the fallback, made explicit.** It used to be the
expression ``importance is not low``, which is to say the fallback was a fact
about which members the enum happened to have. It is a column now, so a new rung
declares whether it leaves the building and is meaningful before an
administrator has written a single cell. The two seeded rows reproduce the old
rule exactly: ``low`` no, ``high`` yes. **A deployment that upgrades and changes
nothing routes identically**, which is the property that made this safe to ship.

**``rank`` orders the axis for a screen and NOTHING routes on it.** A threshold
rule would mean inserting a rung in the middle silently re-routed its
neighbours, which is the class of surprise a deviation-only config table exists
to avoid.

**The columns widen from the six characters "normal" needed.** A rung is a key
in the host's own words, so ``VARCHAR(6)`` was a limit nobody chose: "critical"
does not fit in it. On Postgres a widening is a catalogue update with no table
rewrite, so this costs nothing even on the largest table the package owns. On
SQLite the declared width is advisory and never enforced, so the rebuild a batch
alter would need is skipped rather than run for no effect.

**No CHECK constraint is added in its place, deliberately.** The reference is
validated in code against the catalogue (``service._importance_known``), which
is where ``topic`` is validated too, and for the same reason: an org adding a
rung must not need DDL.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-04

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)

_FEED = "notification"
_POLICY = "notification_channel_policy"
_CATALOGUE = "notification_importance"

#: The platform rungs, spelled here rather than imported from the models, on the
#: standing migration rule: a revision describes the database at a point in time,
#: and importing a constant that later changes would make this file mean
#: something other than it did when it ran.
#: ``(key, name, rank, emails_by_default)``.
_SEED = (
    ("low", "Low", 10, False),
    ("high", "High", 20, True),
)

#: The width every key and coordinate on this axis gets.
_WIDTH = 40


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        _CATALOGUE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Opaque host identity, exactly like every other org column here: NULL
        # is a platform row, a value is that org's override.
        sa.Column("org_id", sa.String(length=255), nullable=True),
        sa.Column("key", sa.String(length=_WIDTH), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "emails_by_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "key", name="uq_notification_importance_org_key"),
    )
    op.create_index(f"ix_{_CATALOGUE}_org_id", _CATALOGUE, ["org_id"])
    op.create_index(f"ix_{_CATALOGUE}_key", _CATALOGUE, ["key"])

    # The seed is INSERT-if-absent rather than a bare INSERT: a host may seed
    # these rows itself at boot (the topic catalogue's pattern), and this
    # revision must not race it into a duplicate. The unique constraint would
    # not catch one either, since NULLs are distinct to it.
    for key, name, rank, emails in _SEED:
        exists = conn.execute(
            sa.text(f"SELECT 1 FROM {_CATALOGUE} WHERE org_id IS NULL AND key = :k"),
            {"k": key},
        ).first()
        if exists:
            continue
        conn.execute(
            sa.text(
                f"INSERT INTO {_CATALOGUE} "
                "(org_id, key, name, rank, emails_by_default) "
                "VALUES (NULL, :k, :n, :r, :e)"
            ),
            {"k": key, "n": name, "r": rank, "e": emails},
        )

    # SQLite never enforced the width; widening there would mean a full table
    # rebuild to change a value the engine ignores.
    if _dialect() != "sqlite":
        op.alter_column(
            _FEED,
            "importance",
            existing_type=sa.String(6),
            type_=sa.String(_WIDTH),
            existing_nullable=False,
        )
        op.alter_column(
            _POLICY,
            "importance",
            existing_type=sa.String(6),
            type_=sa.String(_WIDTH),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Fold every org rung back onto ``low``/``high``, then drop the catalogue.

    **The fold is computed from the catalogue while it still exists**, which is
    what makes it honest rather than a guess: a rung whose row says
    ``emails_by_default`` routed like ``high`` and one whose row says otherwise
    routed like ``low``, so each stored notification is folded onto the rung
    that describes what actually happened to it. That is ``0009``'s rule
    ("folding down would be a false claim about the past") applied to values it
    could not have known about.

    **Policy CELLS are DELETED rather than folded**, again following ``0009``: a
    cell an administrator wrote for ``critical`` must not silently start
    matching every ``high`` notification. The count is logged with the fix.

    **A host with RLS forced on these tables will see this do nothing**, and it
    is counted and warned about rather than assumed, for the reason ``0009``
    records at length: a migration binds no tenant, so a policy reading a tenant
    GUC hides every row from it and the statement reports success.
    """
    conn = op.get_bind()

    retired = [
        row[0]
        for row in conn.execute(
            sa.text(
                f"SELECT DISTINCT key FROM {_CATALOGUE} "
                "WHERE key NOT IN ('low', 'high')"
            )
        ).all()
    ]
    for key in retired:
        emails = conn.execute(
            sa.text(
                f"SELECT emails_by_default FROM {_CATALOGUE} "
                # The platform row first: NULLS FIRST is not portable, and the
                # platform row is the one that describes the rung generally.
                "WHERE key = :k "
                "ORDER BY CASE WHEN org_id IS NULL THEN 0 ELSE 1 END"
            ),
            {"k": key},
        ).scalar()
        target = "high" if emails else "low"
        folded = conn.execute(
            sa.text(f"UPDATE {_FEED} SET importance = :t WHERE importance = :k"),
            {"t": target, "k": key},
        ).rowcount
        if folded:
            logger.info(
                "asas-notifications 0010 folded %s notification row(s) from the "
                "org rung '%s' onto '%s': that is the channel set they were "
                "actually delivered on",
                folded,
                key,
                target,
            )

    if retired:
        doomed = conn.execute(
            sa.text(
                f"SELECT count(*) FROM {_POLICY} WHERE importance NOT IN ('low', 'high')"
            )
        ).scalar()
        if doomed:
            conn.execute(
                sa.text(
                    f"DELETE FROM {_POLICY} WHERE importance NOT IN ('low', 'high')"
                )
            )
            logger.warning(
                "asas-notifications 0010 removed %s channel policy cell(s) on "
                "org-defined rungs (%s); the axis is low | high again and such "
                "a cell can no longer match an emit. Re-state them against "
                "'low' or 'high' if they are still wanted",
                doomed,
                ", ".join(sorted(retired)),
            )

    left = conn.execute(
        sa.text(f"SELECT count(*) FROM {_FEED} WHERE importance NOT IN ('low', 'high')")
    ).scalar()
    if left:
        logger.warning(
            "asas-notifications 0010 downgrade: %s notification row(s) still "
            "hold a rung outside 'low' | 'high'. Row level security on %s hides "
            "rows from an unauthenticated migration, so the fold above could "
            "not see them. Fold them with the tenant bound before reading this "
            "column through the 0.18.0 models, which validate it as an enum",
            left,
            _FEED,
        )

    if _dialect() != "sqlite":
        op.alter_column(
            _POLICY,
            "importance",
            existing_type=sa.String(_WIDTH),
            type_=sa.String(6),
            existing_nullable=True,
        )
        op.alter_column(
            _FEED,
            "importance",
            existing_type=sa.String(_WIDTH),
            type_=sa.String(6),
            existing_nullable=False,
        )

    op.drop_index(f"ix_{_CATALOGUE}_key", table_name=_CATALOGUE)
    op.drop_index(f"ix_{_CATALOGUE}_org_id", table_name=_CATALOGUE)
    op.drop_table(_CATALOGUE)
