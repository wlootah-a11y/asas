"""DR 0003 (U-1 + U-2): action-referenced emits and axis-based routing.

The two guarantees that matter most: (1) EQUIVALENCE — empty policy tables
reproduce 0.15 routing bit-for-bit for the whole Teamy reference catalog; and
(2) PRECEDENCE — topic row beats axis row beats fallback, org override beats
platform, per channel.
"""

import warnings

import pytest
from sqlmodel import select

import asas_notifications as notifications
from asas_notifications import service
from asas_notifications.models import (
    Notification,
    NotificationChannelPolicy,
    NotificationDelivery,
    NotificationTopic,
)


def emit_axes(session, recipients, action, **kw):
    kw.setdefault("topic", "general")
    kw.setdefault("nature", "info")
    kw.setdefault("urgency", "normal")
    kw.setdefault("title", "Hello")
    rows = notifications.notify(session, recipients, action, **kw)
    session.commit()
    return rows


def add_topic(session, key, org_id=None, **kw):
    session.add(NotificationTopic(key=key, name=key.title(), org_id=org_id, **kw))
    session.commit()
    service.config_cache_clear()


def add_policy(session, channel, **kw):
    session.add(NotificationChannelPolicy(channel=channel, **kw))
    session.commit()
    service.config_cache_clear()


def deliveries(session, notification_id):
    return session.exec(
        select(NotificationDelivery.channel).where(
            NotificationDelivery.notification_id == notification_id
        )
    ).all()


# ── the new emit ─────────────────────────────────────────────────────────────


def test_axes_emit_persists_everything(session):
    n = emit_axes(
        session, [1], "job.publish",
        nature="action", urgency="high",
        template="job_published", data={"job_title": "Analyst"},
    )[0]
    assert (n.action, n.topic, n.nature, n.urgency) == (
        "job.publish", "general", "action", "high",
    )
    assert n.template == "job_published" and n.data == {"job_title": "Analyst"}


def test_ad_hoc_emit_has_no_action_and_never_coalesces(session):
    first = emit_axes(
        session, [1], None, urgency="low",
        entity_type="import", entity_id=1, coalesce_unread=True, title="run 1",
    )[0]
    second = emit_axes(
        session, [1], None, urgency="low",
        entity_type="import", entity_id=1, coalesce_unread=True, title="run 2",
    )[0]
    assert first.action is None and second.id != first.id  # no action → no fold


def test_coalesce_keys_on_action_and_keeps_latest_data(session):
    kw = dict(urgency="low", entity_type="job", entity_id=7, coalesce_unread=True)
    first = emit_axes(session, [1], "job.update", data={"v": 1}, title="v1", **kw)[0]
    folded = emit_axes(session, [1], "job.update", data={"v": 2}, title="v2", **kw)[0]
    other = emit_axes(session, [1], "job.comment", title="c1", **kw)[0]
    assert folded.id == first.id and folded.data == {"v": 2}  # latest data wins
    assert other.id != first.id  # a different action never folds


# ── equivalence: empty tables reproduce 0.15 routing ─────────────────────────

# The Teamy reference catalog (adoption guide §6.2) mapped to axes.
TEAMY = [
    ("workflow.request_approval", "approvals", "action", "normal"),
    ("workflow.request_info", "approvals", "action", "normal"),
    ("workflow.provide_info", "approvals", "action", "normal"),
    ("workflow.decide", "approvals", "info", "normal"),
    ("team.add_member", "system", "info", "normal"),
    ("comment.mention", "mentions", "info", "normal"),
    ("work_item.assign", "assignments", "info", "normal"),
    ("work_item.update", "activity", "info", "low"),
    ("work_item.comment", "activity", "info", "low"),
    ("wiki_page.comment", "activity", "info", "low"),
    ("verification.complete", "system", "info", "normal"),
    ("vcs.open_pr", "code", "info", "normal"),
    ("vcs.merge_pr", "code", "info", "normal"),
]


def test_empty_tables_reproduce_015_routing_for_the_teamy_catalog(session):
    """The DR's equivalence guarantee: with no policy rows, `low` is in-app
    only and `normal`/`high` add exactly one email delivery row — for every
    kind in the adoption guide's reference wiring."""
    for topic in {t for _, t, *_ in TEAMY}:
        if topic != "general":
            add_topic(session, topic)
    for action, topic, nature, urgency in TEAMY:
        n = emit_axes(
            session, [1], action,
            topic=topic, nature=nature, urgency=urgency,
        )[0]
        expected = [] if urgency == "low" else ["email"]
        assert deliveries(session, n.id) == expected, action


# ── precedence ───────────────────────────────────────────────────────────────


def test_topic_policy_row_beats_axis_row_and_fallback(session):
    add_topic(session, "approvals")
    # axis row: high urgency adds teams
    add_policy(session, "teams", urgency="high", enabled=True)
    # topic row: approvals disables email despite normal/high urgency
    add_policy(session, "email", topic="approvals", enabled=False)
    n = emit_axes(
        session, [1], "workflow.request_approval",
        topic="approvals", nature="action", urgency="high",
    )[0]
    assert deliveries(session, n.id) == ["teams"]  # email off (topic), teams on (axis)


def test_a_row_forces_a_channel_the_fallback_would_not(session):
    """A rule beats the built-in fallback, and only where it applies.

    Rewritten when ``nature`` stopped being a routing condition. It used to
    discriminate on nature — warning emails even at low urgency, info does not —
    and that shape is genuinely gone: two low-urgency notifications differing
    only in nature now route identically. A rule that needs to separate them has
    to say so with a topic, which is what this does.
    """
    add_topic(session, "alerts")
    add_topic(session, "jobs")
    add_policy(session, "email", topic="alerts", enabled=True)
    alert_low = emit_axes(
        session, [1], "system.alert", topic="alerts", urgency="low", title="w",
    )[0]
    job_low = emit_axes(
        session, [1], "job.update", topic="jobs", urgency="low", title="i",
    )[0]
    assert deliveries(session, alert_low.id) == ["email"]  # the row wins
    assert deliveries(session, job_low.id) == []           # fallback untouched


def test_a_topic_and_urgency_cell_beats_either_coordinate_alone(session):
    """The cell 0.16.0 could not store, and the precedence it introduces.

    Under the old CHECK a row stated a topic or an urgency, never both, so
    "interviews, but only the urgent ones" was unwritable and the nearest rule
    applied to every interview notification. Here the broad topic rule turns
    email OFF for the topic, and the narrower cell turns it back ON for the
    urgent ones only.
    """
    add_topic(session, "interviews")
    add_policy(session, "email", topic="interviews", enabled=False)
    add_policy(session, "email", topic="interviews", urgency="high", enabled=True)

    urgent = emit_axes(
        session, [1], "interview.cancelled", topic="interviews", urgency="high", title="u",
    )[0]
    ordinary = emit_axes(
        session, [1], "interview.noted", topic="interviews", urgency="normal", title="o",
    )[0]
    assert deliveries(session, urgent.id) == ["email"], "the two-coordinate cell should win"
    assert deliveries(session, ordinary.id) == [], "the topic rule should still apply here"


def test_an_urgency_row_applies_across_every_topic(session):
    """The other single coordinate: a column of the matrix rather than a row."""
    add_topic(session, "interviews")
    add_topic(session, "candidates")
    add_policy(session, "email", urgency="low", enabled=True)  # even the quiet rung
    a = emit_axes(session, [1], "interview.noted", topic="interviews", urgency="low", title="a")[0]
    b = emit_axes(session, [1], "candidate.viewed", topic="candidates", urgency="low", title="b")[0]
    assert deliveries(session, a.id) == ["email"]
    assert deliveries(session, b.id) == ["email"]


def test_the_all_null_row_is_the_org_wide_default(session):
    """Both coordinates NULL was forbidden before; it is the default row now."""
    add_topic(session, "interviews")
    add_policy(session, "email", enabled=False)  # no topic, no urgency
    n = emit_axes(session, [1], "interview.cancelled", topic="interviews", urgency="high")[0]
    assert deliveries(session, n.id) == [], "the default row should suppress email everywhere"


def test_org_override_row_beats_platform_row(session):
    add_topic(session, "code")
    add_policy(session, "email", topic="code", enabled=False)            # platform
    add_policy(session, "email", topic="code", enabled=True, org_id=1)   # org 1
    n = emit_axes(session, [1], "vcs.open_pr", topic="code")[0]  # ctx org is 1
    assert deliveries(session, n.id) == ["email"]


def test_in_app_disabled_by_policy_suppresses_the_insert(session):
    add_topic(session, "activity")
    add_policy(session, "in_app", topic="activity", enabled=False)
    assert emit_axes(session, [1], "work_item.update", topic="activity") == []
    assert session.exec(select(Notification)).all() == []


def test_policy_cache_ttl_serves_stale_until_cleared(session):
    add_topic(session, "code")
    n1 = emit_axes(session, [1], "vcs.open_pr", topic="code")[0]
    assert deliveries(session, n1.id) == ["email"]
    session.add(NotificationChannelPolicy(channel="email", topic="code", enabled=False))
    session.commit()  # no cache clear — the admin API will clear; TTL covers the rest
    n2 = emit_axes(session, [1], "vcs.open_pr", topic="code")[0]
    assert deliveries(session, n2.id) == ["email"]  # stale ≤ 60s by design
    service.config_cache_clear()
    n3 = emit_axes(session, [1], "vcs.open_pr", topic="code")[0]
    assert deliveries(session, n3.id) == []


# ── legacy shims (one release) ───────────────────────────────────────────────


def test_register_kind_shim_supplies_axes_and_warns(session):
    with pytest.warns(DeprecationWarning, match="register_kind"):
        notifications.register_kind(
            "workflow.approval_requested",
            category="action", urgency="normal",
        )
    rows = notifications.notify(
        session, [1], "workflow.approval_requested", title="Budget change"
    )
    session.commit()
    n = rows[0]
    assert (n.action, n.topic, n.nature, n.urgency) == (
        "workflow.approval_requested", "general", "action", "normal",
    )
    assert deliveries(session, n.id) == ["email"]  # spec urgency routed as before


def test_kind_and_category_keyword_aliases_warn_and_map(session):
    with pytest.warns(DeprecationWarning, match="kind"):
        n = notifications.notify(
            session, [1], kind="job.close",
            topic="general", nature="info", urgency="low", title="x",
        )[0]
    assert n.action == "job.close"
    with pytest.warns(DeprecationWarning, match="category"):
        n = notifications.notify(
            session, [1], "job.close",
            topic="general", category="warning", urgency="low", title="x",
        )[0]
    assert n.nature == "warning"


# ── payload + feed surface ───────────────────────────────────────────────────


def test_delivery_payload_carries_the_new_vocabulary(migrated, session):
    adapter = notifications.LoggingAdapter()
    notifications.register_adapter("email", adapter)
    emit_axes(
        session, [1], "job.publish",
        nature="action", urgency="high",
        data={"job_title": "Analyst"},
    )
    assert notifications.dispatch_pending(migrated) == 1
    p = adapter.sent[0]
    assert (p.action, p.topic, p.nature, p.urgency) == (
        "job.publish", "general", "action", "high",
    )
    assert p.data == {"job_title": "Analyst"}
    assert not hasattr(p, "kind") and not hasattr(p, "category")


def test_feed_filters_on_nature_with_category_alias(migrated, session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    notifications.configure_context_resolver(lambda s: (1, 1))
    emit_axes(session, [1], "job.publish", nature="action", urgency="low")
    emit_axes(session, [1], "job.update", nature="info", urgency="low")

    app = FastAPI()

    def get_session():
        with Session(migrated) as s:
            yield s

    app.include_router(notifications.build_router(get_session))
    client = TestClient(app)
    assert client.get("/me/notifications?nature=action").json()["total"] == 1
    # the 0.15 name keeps working for one release
    assert client.get("/me/notifications?category=action").json()["total"] == 1
    item = client.get("/me/notifications?nature=action").json()["items"][0]
    assert item["action"] == "job.publish" and item["nature"] == "action"
    assert "kind" not in item and "category" not in item


# ── review fixes (PR #43) ────────────────────────────────────────────────────


def test_unknown_topic_fails_loud_inside_suppression(session):
    """Suppression silences delivery, never catalog mistakes — the 0.15
    unregistered-kind guarantee, carried to the topic reference."""
    with notifications.suppressed():
        with pytest.raises(LookupError, match="topic"):
            emit_axes(session, [1], "job.publish", topic="aprovals")


def test_registered_kind_with_partial_axes_fails_loud(session):
    """The shim covers only fully-legacy calls: a site that states even one
    axis has been converted and gets the new contract, not silent backfill."""
    with pytest.warns(DeprecationWarning):
        notifications.register_kind(
            "job.publish", category="action", urgency="normal", reason="participant"
        )
    with pytest.raises(TypeError, match="axis"):
        notifications.notify(session, [1], "job.publish", nature="info", title="x")


def test_policy_tie_break_prefers_the_newest_row(session):
    add_topic(session, "billing")
    add_policy(session, "email", topic="billing", enabled=False)
    add_policy(session, "email", topic="billing", enabled=True)  # admin's newer row
    n = emit_axes(session, [1], "invoice.send", topic="billing")[0]
    assert deliveries(session, n.id) == ["email"]


def test_coalesce_fold_refreshes_topic_and_template(session):
    kw = dict(urgency="low", entity_type="job", entity_id=3, coalesce_unread=True)
    first = emit_axes(session, [1], "job.update", template="v1", title="t1", **kw)[0]
    # simulate a pre-0004 row: topic never labeled
    first.topic = None
    session.add(first)
    session.commit()
    folded = emit_axes(session, [1], "job.update", template="v2", title="t2", **kw)[0]
    assert folded.id == first.id
    assert folded.topic == "general" and folded.template == "v2"


def test_list_feed_category_alias_warns_and_filters(session):
    emit_axes(session, [1], "job.publish", nature="action", urgency="low")
    emit_axes(session, [1], "job.update", nature="info", urgency="low")
    with pytest.warns(DeprecationWarning, match="nature"):
        rows, total = service.list_feed(
            session, 1, category=notifications.Nature.action
        )
    assert total == 1 and rows[0].nature == "action"


def test_topic_seeded_within_ttl_is_found_by_fresh_requery(session):
    """A topic seeded on another replica inside the TTL window costs one extra
    SELECT — never a transaction-aborting false LookupError."""
    emit_axes(session, [1], "job.publish")  # warms the topic cache
    session.add(NotificationTopic(key="late", name="Late"))
    session.commit()  # deliberately NO config_cache_clear()
    n = emit_axes(session, [1], "job.publish", topic="late")[0]
    assert n.topic == "late"
