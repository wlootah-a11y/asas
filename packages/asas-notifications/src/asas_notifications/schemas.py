"""Read models for the /me/notifications API. Kept in sync with the TS types in
``frontend/src/lib/api.ts`` (house rule)."""

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel

from typing import Any



class NotificationRead(SQLModel):
    id: int
    #: The application action that caused this row (DR 0003) — None for ad hoc
    #: emits and for rows predating 0.16 that were never re-labeled.
    action: Optional[str] = None
    topic: Optional[str] = None
    #: The routing axis, as a catalogue KEY. A plain string since 0.19.0: the
    #: rungs are rows in ``notification_importance``, so typing this as the
    #: seeded enum would make a read of an org's own rung fail validation on the
    #: way out, which is a 500 on a feed that stored the value happily.
    #: ``nature`` sat beside it until 0.18.0 and is the host's now: it drove
    #: presentation, and a host that renders its own feed decides how.
    importance: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    template: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    read_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    created_at: datetime


class NotificationList(SQLModel):
    items: list[NotificationRead]
    #: Rows matching the request's filters — the paging total, not an inbox size.
    total: int
    #: Unread and un-archived, ignoring the request's filters: the same number on
    #: every view, so a badge fed from any list call agrees with every other.
    unread_count: int


class ReadAllResult(SQLModel):
    updated: int


class ArchiveResult(SQLModel):
    updated: int
