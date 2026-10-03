"""Notification rows + the per-channel delivery outbox + deviation-only config
(WXL-222; DR 0003).

``notification`` IS the in-app delivery (insert = enqueue, read_at = seen);
``notification_delivery`` exists only for external channels — one row per
(notification, channel) the routing policy selects at emit time. Enums are
plain VARCHARs (``native_enum=False``, dual-engine rule).

DR 0003: a notification **references the application action that caused it**
(``action`` — a free string in the app's ``entity.verb`` grammar, declared
nowhere) and carries two classification axes, ``topic`` and ``importance``
(0.18.0 narrowed DR 0003's four to the two that decide a channel). Management
attaches to the axes, never to individual actions; the config tables (``notification_topic``,
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
from sqlalchemy import Index, String, UniqueConstraint, text
from sqlmodel import Field, SQLModel


# ── enums (stored as plain VARCHAR — native_enum=False) ──────────────────────


class Importance(str, Enum):
    """The rungs this package SEEDS. The axis itself is a catalogue now.

    **This enum is the platform default, not the vocabulary.** Until 0.19.0 it
    WAS the vocabulary: the column was a SQLAlchemy ``Enum``, ``notify`` coerced
    through ``Importance(...)``, and a deployment that wanted a rung between
    "stays in the bell" and "emails everybody" had nowhere to put it. Two rungs
    is what the built-in *fallback* can express; it was never what an
    administrator's matrix could express, and the two were confused.

    So ``notification_importance`` is a real table, seeded with these two by
    migration ``0010`` and extensible per org, exactly as ``notification_topic``
    already is. The enum stays for the same reason ``DEFAULT_TOPIC`` does: the
    seeded keys are referred to by name in code (the fallback for a row that
    predates the table, this package's own tests, a host's defaults), and naming
    them once beats spelling them at each site.

    What the members no longer decide is what may be STORED. ``low`` still means
    the quiet rung and its seeded row carries ``emails_by_default = False``,
    which is what keeps an empty policy table routing exactly as 0.18.0 did:
    ambient activity that emails you is why people mute a product in week two.
    An org that adds ``critical`` gets whatever its own row says, and the
    matrix gains a coordinate rather than a synonym.
    """

    low = "low"
    high = "high"


#: The rungs migration ``0010`` seeds as platform rows, in the order an admin
#: screen should show them: ``(key, name, rank, emails_by_default)``.
#:
#: Two, and deliberately not three. The middle rung 0.18.0 retired is NOT
#: restored here: it was retired because the built-in fallback treated it
#: identically to the top one, and re-seeding it would put the same
#: indistinguishable pair back on every deployment. A rung between the two is
#: now expressible, which is the point, and it is a deployment's call to add
#: one and say what it does rather than the package's to presume it.
PLATFORM_IMPORTANCES: tuple[tuple[str, str, int, bool], ...] = (
    ("low", "Low", 10, False),
    ("high", "High", 20, True),
)


#: The width of every column and catalogue key on this axis. A rung is a key in
#: the host's own words ("critical", "fyi"), so the old ``VARCHAR(6)`` (sized
#: for the literal string "normal") was a limit nobody chose. Matches the
#: package's other short reference columns.
IMPORTANCE_KEY_LENGTH = 40


RETIRED_URGENCY = "normal"


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
    # The two axes (DR 0003 S-1, as narrowed by 0.18.0). `topic` is the
    # management/preference grouping, validated against notification_topic at
    # emit (the one reference that policy and preferences depend on). Nullable
    # only for rows that predate migration 0004 — new emits always carry one.
    topic: Optional[str] = Field(default=None, index=True)
    # `importance` is how loudly it reaches the recipient. Both axes route, and
    # they are the only two that ever did: `nature` (what the notification asks
    # of you) described the host's PRESENTATION and left this package in 0.18.0,
    # because a host that renders its own feed is the side that knows how it
    # wants to render it. A host that needs it keeps it on its own sidecar row.
    # Validated against ``notification_importance`` at emit, exactly like
    # ``topic`` above and for the same reason: both are references the routing
    # matrix keys on, so an unseeded value is a catalog mistake rather than a
    # row to route on a guess. A plain VARCHAR and not a SQLAlchemy ``Enum``
    # since 0.19.0 — an enum column cannot hold a rung an org invented, and it
    # raises on READ as well as on write, so the restriction was not something
    # a host could work around on its own side.
    importance: str = Field(
        sa_column=Column(String(IMPORTANCE_KEY_LENGTH), nullable=False)
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
        # The composite above never arbitrates platform rows — their org_id is
        # NULL and SQL NULLs compare distinct — so platform-key uniqueness is
        # this partial index (migration 0005). Seeders keep check-then-insert
        # as the fast path and catch IntegrityError for the race it loses.
        Index(
            "uq_notification_topic_platform_key",
            "key",
            unique=True,
            postgresql_where=text("org_id IS NULL"),
            sqlite_where=text("org_id IS NULL"),
        ),
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


class NotificationImportance(SQLModel, table=True):
    """A rung on the "how loudly" axis: the second coordinate of the matrix.

    **Why this is a table and not the enum it used to be.** ``importance`` and
    ``topic`` are the two axes routing keys on, and until 0.19.0 they were
    modelled completely differently: topics were rows a deployment seeds and
    extends, while importance was a two-member Python enum welded into the
    column type. That asymmetry had no argument behind it. The argument that
    existed was about the built-in FALLBACK, which can only say "in-app" or
    "in-app and email" and therefore needs no more than two rungs to express
    itself; it was never an argument about how many rungs an administrator's
    matrix could tell apart, and the matrix is the thing a deployment
    configures.

    So: platform rows (``org_id NULL``, seeded by migration ``0010``) plus
    optional org override rows, DR 0001's shared-with-overrides pattern, the
    same shape :class:`NotificationTopic` has. An org adds ``critical`` and
    writes cells against it; nothing in this package needs to know the word.

    ``emails_by_default`` is what the retired enum encoded implicitly. The
    fallback used to be the literal expression ``importance is not low``, so
    "which rungs email when no policy cell matches" was a fact about the enum's
    membership rather than about any rung. It is a column now, which is what
    makes a third rung meaningful before an administrator has written a single
    cell: a new rung declares whether it leaves the building, and the matrix
    then deviates from that per topic.

    ``rank`` orders the axis for a screen, quiet end first. It is presentation
    only and NOTHING routes on it: a threshold rule would mean adding a rung in
    the middle silently re-routed its neighbours, which is exactly the class of
    surprise a deviation-only config table exists to avoid.
    """

    __tablename__ = "notification_importance"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_notification_importance_org_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: Opaque, exactly like the notification row's. NULL is a platform row.
    org_id: Optional[str] = Field(default=None, index=True, max_length=255)
    key: str = Field(index=True, max_length=IMPORTANCE_KEY_LENGTH)
    name: str
    description: Optional[str] = None
    #: With no matching policy cell, do external channels deliver at this rung?
    #: The seeded ``low`` says no and ``high`` says yes, which reproduces the
    #: pre-0.19.0 built-in rule exactly.
    emails_by_default: bool = False
    #: Display order, quiet end first. Never a routing input; see the docstring.
    rank: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationChannelPolicy(SQLModel, table=True):
    """One routing deviation: a cell of the (topic × importance) matrix, with
    either coordinate optional.

    **A row may carry BOTH coordinates**, which was the change in 0.17.0. Before
    that a CHECK forbade the combination: a row was a topic rule or an axis rule,
    never both, so "order notifications, but only the important ones, go to
    email" could not be stored at all — the nearest expressible rules were "all
    order notifications" or "all important notifications", and neither is the
    rule an administrator meant. Nothing warned about the gap because the
    constraint rejected the write.

    So the two coordinates are independent, and NULL means "every value of this
    axis":

    =========================  ========================================
    ``(topic, importance)``    the rule
    =========================  ========================================
    ``("orders", "high")`` this topic, at this importance
    ``("orders", None)``   this topic, every importance
    ``(None, "high")``         every topic, at this importance
    ``(None, None)``           every notification (the org-wide default)
    =========================  ========================================

    Resolution precedence (per channel, most specific wins): both coordinates
    beat topic alone beats importance alone beats the all-NULL row beats the
    built-in code fallback (``low`` → in-app only, else in-app + email). Org
    override rows beat platform rows within a tier. ``mandatory`` marks channels
    user preferences may not disable (U-3).

    **There are exactly two coordinates, and there have been three names for the
    second one.** ``nature`` (what the notification asks of you) stopped being a
    condition in 0.17.0 and left the package altogether in 0.18.0: it described
    PRESENTATION, which is the host's business, and every rule expressible
    against it was expressible against this axis. ``urgency`` was this axis's own
    name until 0.18.0 renamed it and dropped its unusable middle rung. Do not
    reintroduce either: a routing table with a coordinate that decides nothing is
    a rule an administrator can write and never see the effect of."""

    __tablename__ = "notification_channel_policy"

    id: Optional[int] = Field(default=None, primary_key=True)
    #: Opaque, exactly like the notification row's. A platform row is NULL; an
    #: org override row carries the host's own org id, whatever shape that is.
    #: Widened with the row it governs: a host that can be notified but cannot
    #: write a rule for itself is a worse state than one that cannot adopt at
    #: all, because the product looks wired and the rule silently will not save.
    org_id: Optional[str] = Field(default=None, index=True, max_length=255)
    topic: Optional[str] = Field(default=None, index=True)
    # NULL is the wildcard ("every rung"). A plain VARCHAR since 0.19.0: the
    # coordinate is a catalogue key now, so a cell may name a rung this package
    # never heard of, and reading one back must not raise.
    importance: Optional[str] = Field(
        default=None,
        sa_column=Column(String(IMPORTANCE_KEY_LENGTH), nullable=True),
    )
    channel: str  # "in_app", "email", "teams", …
    enabled: bool = True
    mandatory: bool = False  # exempt from user preference narrowing (U-3)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
