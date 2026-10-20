"""The package's two tables: where each walk got to, and when each key was last met.

``org_id`` is a string (a UUID host passes ``str(uuid)``, an int host
``str(n)``; the empty string is the platform itself) and ``resource`` is the
host's name for the collection. No foreign keys to host tables: removing the
package is a table drop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, Index
from sqlmodel import Field, SQLModel


def _ts(nullable: bool = True, index: bool = False) -> Column:
    # A plain SQL DateTime holding naive UTC: SQLModel's own type refuses
    # naive values, and SQLite hands back naive whatever was written.
    return Column(DateTime(), nullable=nullable, index=index)


class SyncCursor(SQLModel, table=True):
    __tablename__ = "asas_sync_cursor"

    org_id: str = Field(default="", primary_key=True, max_length=64)
    resource: str = Field(primary_key=True, max_length=64)
    #: Everything the remote changed before this has been read.
    watermark: Optional[datetime] = Field(default=None, sa_column=_ts())
    #: A key-ordered walk that stopped early resumes after this key.
    last_key: Optional[str] = Field(default=None, max_length=128)
    last_complete: bool = Field(default=False)
    backfilled_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    last_run_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    last_run_ms: Optional[int] = None
    last_error: Optional[str] = Field(default=None, max_length=500)
    #: The START of the last complete reconcile walk.
    reconciled_at: Optional[datetime] = Field(default=None, sa_column=_ts())
    lease_owner: Optional[str] = Field(default=None, max_length=64)
    lease_until: Optional[datetime] = Field(default=None, sa_column=_ts())


class SyncSeen(SQLModel, table=True):
    __tablename__ = "asas_sync_seen"
    __table_args__ = (Index("ix_asas_sync_seen_unseen", "org_id", "resource", "seen_at"),)

    org_id: str = Field(default="", primary_key=True, max_length=64)
    resource: str = Field(primary_key=True, max_length=64)
    key: str = Field(primary_key=True, max_length=128)
    seen_at: datetime = Field(sa_column=_ts(nullable=False))
