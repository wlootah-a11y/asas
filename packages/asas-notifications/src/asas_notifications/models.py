"""Notification rows + the per-channel delivery outbox + deviation-only config
(WXL-222; DR 0003).

``notification`` IS the in-app delivery (insert = enqueue, read_at = seen);
``notification_delivery`` exists only for external channels — one row per
(notification, channel) the routing policy selects at emit time. Enums are
plain VARCHARs (``native_enum=False``, dual-engine rule).

DR 0003: a notification **references the application action that caused it**
(``action`` — a free string in the app's ``entity.verb`` grammar, declared
nowhere) and carries four classification axes. Management attaches to the axes,
never to individual actions; the config tables (``notification_topic``,
``notification_channel_policy``) store **deviations** from code defaults —
platform rows (``org_id NULL``) plus optional org override rows, DR 0001's
shared-with-overrides pattern.

``org_id`` follows the tenancy epic's mapping (WXL-218): notifications are
tenant data — org-scoped in the catalog, stamped from the producing request's
context.

**Identity columns are opaque strings.** ``org_id``, ``user_id`` and
``entity_id`` used to be ``int``. That reads as decoupling and is not: an integer
column is an assertion about the host's schema, namely that it numbers its users
and organisations sequentially. A host on UUID primary keys had nothing to put
there and no seam that widened it, so it could not adopt the package at all.

The package never interprets these values. It groups, filters and compares them,
all of which text does, so they are stored as VARCHAR and normalised at the
boundary by ``normalize_id``. An int host keeps passing ints and reads back their
decimal string; a UUID host passes its own keys. The visibility filter and the
context resolver are deliberately unaffected, because they are handed the host's
own values rather than the storage form: a filter written against ints that
silently stops dropping anyone is a leak, and that is the one failure the seam
exists to prevent.

"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


# ── enums (stored as plain VARCHAR — native_enum=False) ──────────────────────


class Nature(str, Enum):
    """What the notification demands of the recipient (drives UI + email subject).
    One of DR 0003's four axes; ``category`` until 0.15 — renamed because the
    word now belongs to nothing (the old kind catalog is gone)."""

    action = "action"
    info = "info"
    warning = "warning"


#: Deprecated alias for :class:`Nature` (0.15's name). One release, then gone.
Category = Nature


class Urgency(str, Enum):
    """How interruptive delivery may be (Apple interruption levels, coarsened)."""

    low = "low"
    normal = "normal"
    high = "high"


class DeliveryStatus(str, Enum):
    pending = "pending"
    sending = "sending"  # claimed by a dispatch pass (TEAMY-475); stale claims reclaim
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class Notification(SQLModel, table=True):
    __tablename__ = "notification"
    __table_args__ = (
        # The feed: WHERE user_id = ? [AND org_id = ?] AND archived_at IS (NOT)
        # NULL ORDER BY created_at DESC, id DESC. org_id sits second so the
        # org-scoped queries filter on the index while unscoped single-tenant
        # queries still use the user_id prefix. Subsumes the old single-column
        # user_id index (dropped in migration 0003). id trails as the ORDER BY
        # tiebreaker so tie-heavy batch emits still stream straight off the
        # index.
        Index(
            "ix_notification_user_org_archived_created",
            "user_id", "org_id", "archived_at", "created_at", "id",
        ),
        # The badge: WHERE user_id = ? [AND org_id = ?]
        # AND read_at IS NULL AND archived_at IS NULL.
        Index(
            "ix_notification_user_org_read_archived",
            "user_id", "org_id", "read_at", "archived_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Opaque host identity. No host FK, and deliberately no assertion about the
    # host's key TYPE either: see the module docstring. ``normalize_id`` in
    # service.py is the one place a value becomes one of these, and nothing in
    # the package parses them.
    #
    # 255 and not 64: a UUID is 36 characters, but a host whose principal
    # subject is an EMAIL can reach 254 (RFC 5321), and a column that truncates
    # or rejects a recipient is worse than a wide one. Postgres varchar stores
    # only what is present, so the declared bound costs nothing for short
    # values, including an int host's decimal strings.
    org_id: str = Field(index=True, max_length=255)
    user_id: str = Field(max_length=255)  # the recipient; indexed via the composites above
    # The application action that caused this notification (DR 0003 S-2):
    # provenance + coalescing identity + the future actions-layer join key.
    # A *reference without declaration* — never validated against a catalog.
    # NULL for ad hoc emits (a one-off "import finished"), which therefore
    # never coalesce.
    action: Optional[str] = Field(default=None, index=True)
    # The four axes (DR 0003 S-1). `topic` is the management/preference
    # grouping, validated against notification_topic at emit (the one
    # reference that policy and preferences depend on). Nullable only for
    # rows that predate migration 0004 — new emits always carry one.
    topic: Optional[str] = Field(default=None, index=True)
    nature: Nature = Field(
        sa_column=Column(SAEnum(Nature, native_enum=False), nullable=False)
    )
    urgency: Urgency = Field(
        sa_column=Column(SAEnum(Urgency, native_enum=False), nullable=False)
    )
    # Generic subject reference (never an FK — the package is entity-agnostic).
    entity_type: Optional[str] = None
    entity_id: Optional[str] = Field(default=None, max_length=255)
    title: str
    body: Optional[str] = None
    link: Optional[str] = None  # frontend deep link, e.g. "/teams/42"
    # DR 0003 S-4: the template reference + structured payload stored alongside
    # the rendered text, so a future localization DR can move the feed to
    # read-time rendering without a migration. `data` is a denormalized
    # presentation payload — the structured sibling of `title`, same
    # PII/retention posture as the row. Rendering itself lands with U-4.
    template: Optional[str] = None
    data: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    read_at: Optional[datetime] = None
    # Dealt with — out of the recipient's inbox. A separate axis from `read_at`:
    # reading is seeing, archiving is finishing, and a host may well want an
    # action notification to survive being read (Teamy TEAMY-692 does).
    archived_at: Optional[datetime] = None
    # Reserved for auto-clearing `action` notifications when the underlying task
    # completes. Deliberately unused: Teamy weighed it for TEAMY-692 and chose
    # the archive gesture instead, so nothing writes this column today.
    resolved_at: Optional[datetime] = None
    #: The recipient's language at emit time, as a BCP-47 tag.
    #:
    #: Stamped HERE rather than resolved at dispatch, and that is the whole
    #: point of the column. ``dispatch_pending`` runs on raw connections outside
    #: any request, where the context resolver returns ``None`` by contract, so
    #: a renderer sitting between the outbox and the adapter has nobody to ask
    #: what language this recipient reads. A product that ships two languages
    #: needs the answer recorded at the moment the fact happened.
    #:
    #: ``None`` means the host wired no resolver, which an adapter should read
    #: as "render in the deployment default" rather than as an error.
    locale: Optional[str] = Field(default=None, max_length=16)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationDelivery(SQLModel, table=True):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        # The dispatcher's scan (status = pending) and the stale-claim sweep
        # (status = sending AND claimed_at < cutoff). Prefix covers the old
        # single-column status index (dropped in migration 0003).
        Index("ix_notification_delivery_status_claimed", "status", "claimed_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    notification_id: int = Field(foreign_key="notification.id", index=True)
    channel: str = Field(index=True)  # "email" now; "slack"/"teams" later
    status: DeliveryStatus = Field(
        default=DeliveryStatus.pending,
        sa_column=Column(SAEnum(DeliveryStatus, native_enum=False), nullable=False),
    )
    attempts: int = Field(default=0)
    # When the row was CAS-claimed (status → sending); a claim older than
    # ``service.STALE_CLAIM_SECONDS`` belongs to a crashed pass and reclaims.
    claimed_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    last_error: Optional[str] = None


# ── deviation-only configuration (DR 0003 S-3) ───────────────────────────────


class NotificationTopic(SQLModel, table=True):
    """A preference/management grouping (~5–8 per app; Android-channel-shaped).

    Platform rows have ``org_id NULL``; an org override row (same ``key``,
    org set) beats the platform row. Migration 0004 seeds one platform row,
    ``general`` — the designated topic for ad hoc emits and the legacy
    ``register_kind`` shim."""

    __tablename__ = "notification_topic"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_notification_topic_org_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: Opaque, exactly like the notification row's. A platform row is NULL; an
    #: org override row carries the host's own org id, whatever shape that is.
    #: Widened with the row it governs: a host that can be notified but cannot
    #: write a rule for itself is a worse state than one that cannot adopt at
    #: all, because the product looks wired and the rule silently will not save.
    org_id: Optional[str] = Field(default=None, index=True, max_length=255)
    key: str = Field(index=True)  # e.g. "approvals", "activity"
    name: str
    description: Optional[str] = None
    # Locked topics (e.g. security) never appear on the preference screen.
    # Enforced by U-3's preference API, carried here so admin UI can read it.
    user_configurable: bool = True
    sort_order: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationChannelPolicy(SQLModel, table=True):
    """One routing deviation: a cell of the (topic × urgency) matrix, with either
    coordinate optional.

    **A row may now carry BOTH a topic and an urgency**, which is the change from
    0.16.0's shape. Before, a CHECK forbade the combination: a row was a topic
    rule or an axis rule, never both, so "interview notifications, but only the
    urgent ones, go to email" could not be stored at all — the nearest
    expressible rules were "all interview notifications" or "all urgent
    notifications", and neither is the rule an administrator meant. Nothing
    warned about the gap because the constraint rejected the write.

    So the two coordinates are independent now, and NULL means "every value of
    this axis":

    ======================  ===========================================
    ``(topic, urgency)``    the rule
    ======================  ===========================================
    ``("interviews", …)``   this topic, at this urgency  ← the new cell
    ``("interviews", None)``this topic, every urgency
    ``(None, "high")``      every topic, at this urgency
    ``(None, None)``        every notification (the org-wide default)
    ======================  ===========================================

    Resolution precedence (per channel, most specific wins): both coordinates
    beat topic alone beats urgency alone beats the all-NULL row beats the
    built-in code fallback (``low`` → in-app only, else in-app + email). Org
    override rows beat platform rows within a tier. ``mandatory`` marks channels
    user preferences may not disable (U-3).

    ``nature`` is NOT a routing condition. It stays on the notification row,
    where it drives the UI treatment and the email subject, but it never decided
    a channel: what a notification asks of you is not the same question as how
    loudly to deliver it, and urgency already answers the second. Every rule
    written against it could be written against urgency instead."""

    __tablename__ = "notification_channel_policy"

    id: Optional[int] = Field(default=None, primary_key=True)
    #: Opaque, exactly like the notification row's. A platform row is NULL; an
    #: org override row carries the host's own org id, whatever shape that is.
    #: Widened with the row it governs: a host that can be notified but cannot
    #: write a rule for itself is a worse state than one that cannot adopt at
    #: all, because the product looks wired and the rule silently will not save.
    org_id: Optional[str] = Field(default=None, index=True, max_length=255)
    topic: Optional[str] = Field(default=None, index=True)
    urgency: Optional[Urgency] = Field(
        default=None,
        sa_column=Column(SAEnum(Urgency, native_enum=False), nullable=True),
    )
    channel: str  # "in_app", "email", "teams", …
    enabled: bool = True
    mandatory: bool = False  # exempt from user preference narrowing (U-3)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
