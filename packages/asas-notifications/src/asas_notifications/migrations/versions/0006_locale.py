"""The recipient's language, recorded at emit.

``dispatch_pending`` runs on raw connections outside any request, where the
context resolver returns ``None`` by contract, so nothing between the outbox and
an adapter can ask what language a recipient reads. A notification emitted today
and mailed by tomorrow's sweep would render in the deployment default, which for
a reader of the other language is simply the wrong email.

So the answer is stamped on the row when the fact happens.

Nullable, with no backfill and no default. NULL means "the host wired no locale
resolver", which an adapter reads as the deployment default: that is what every
existing row means and what every existing deployment already does, so this is
additive and changes nothing until a host configures the seam.

16 characters holds a BCP-47 tag with room to spare (``ar``, ``ar-AE``,
``zh-Hant-TW``).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("notification", sa.Column("locale", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("notification", "locale")
