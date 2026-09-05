"""The three compositions, end to end.

These are the tests that justify the reference host existing at all. Each one
exercises a feature that lives *between* packages, which is exactly the class of
behaviour a per-package suite cannot reach: every package here is behaving
correctly in isolation whether or not the composition works.
"""

from __future__ import annotations

from datetime import date, timedelta

import asas_access
import asas_jobs
import asas_notifications
import asas_search
import asas_workflow
import pytest
from sqlmodel import Session, select

from app.models import DEFAULT_ORG_ID, Ticket


def _ticket(session, **kwargs) -> Ticket:
    ticket = Ticket(**{"title": "Printer offline", **kwargs})
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def _notifications_for(session, user_id: int) -> list:
    return session.exec(
        select(asas_notifications.Notification).where(
            asas_notifications.Notification.user_id == user_id
        )
    ).all()


# --------------------------------------------------------------------------
# Composition 1: escalation
#   workflow definition + access CHANGE_APPROVER + notifications
# --------------------------------------------------------------------------


def test_escalation_notifies_the_resolved_approvers(app_module, agents):
    """Opening an escalation tells whoever the host's resolver names.

    The point is the indirection: the definition names a *principal*
    (CHANGE_APPROVER), access defines what that principal means, the host's
    resolver turns it into people, and notifications delivers. Nothing in the
    chain hardcodes a person or a role.
    """
    from app.wiring import workflow as workflow_wiring

    with Session(app_module.engine) as session:
        ticket = _ticket(session, assignee_id=agents["agent"].id)
        requester = session.get(type(agents["agent"]), agents["agent"].id)

        workflow_wiring.request_escalation(session, ticket, requester)

        # Ada is the only admin, and is not the assignee, so she is the approver.
        assert _notifications_for(session, agents["admin"].id)


def test_the_assignee_cannot_approve_their_own_escalation(app_module, agents):
    """The resolver's exclusion rule, which no engine could hold for us.

    Ada is an admin *and* the assignee here, so the approver set is empty — the
    host's rule beats the role.
    """
    from app.wiring import workflow as workflow_wiring

    with Session(app_module.engine) as session:
        ticket = _ticket(session, assignee_id=agents["admin"].id)

        approvers = workflow_wiring._change_approvers(session, "ticket", ticket.id)
        assert agents["admin"].id not in approvers


def test_approval_flips_the_ticket_and_tells_the_requester(app_module, agents):
    """The completion callback: workflow's end node reaching back into the host.

    Workflow does not know a ticket has a status, and notifications does not
    know an approval happened. Both effects come from the host's callback.
    """
    from app.wiring import workflow as workflow_wiring

    with Session(app_module.engine) as session:
        requester = session.get(type(agents["agent"]), agents["agent"].id)
        ticket = _ticket(session, assignee_id=requester.id)
        instance = workflow_wiring.request_escalation(session, ticket, requester)

        asas_workflow.decide(
            session,
            instance,
            actor_id=agents["admin"].id,
            verdict=asas_workflow.Verdict.positive,
        )
        session.commit()

        session.refresh(ticket)
        assert ticket.status == "escalated"

        titles = [n.title for n in _notifications_for(session, requester.id)]
        assert any("approved" in t for t in titles)


def test_rejection_leaves_the_ticket_alone(app_module, agents):
    """The negative path completes with the engine's own "rejected" outcome.

    Worth its own test because the rejection outcome is a string the host has to
    restate — a typo there would silently treat every rejection as an approval.
    """
    from app.wiring import workflow as workflow_wiring

    with Session(app_module.engine) as session:
        requester = session.get(type(agents["agent"]), agents["agent"].id)
        ticket = _ticket(session, assignee_id=requester.id)
        instance = workflow_wiring.request_escalation(session, ticket, requester)

        asas_workflow.decide(
            session,
            instance,
            actor_id=agents["admin"].id,
            verdict=asas_workflow.Verdict.negative,
            # The engine requires a comment on a negative verdict — a rejection
            # with no stated reason is not an auditable decision.
            comment="Not urgent enough to escalate.",
        )
        session.commit()

        session.refresh(ticket)
        assert ticket.status == "open"

        titles = [n.title for n in _notifications_for(session, requester.id)]
        assert any("declined" in t for t in titles)


# --------------------------------------------------------------------------
# Composition 2: a classified record
#   access MAC + search's never-index-restricted-fields rule
# --------------------------------------------------------------------------


def test_search_never_returns_a_ticket_the_caller_cannot_see(app_module, agents):
    """MAC filtering happens at query time, inside the provider.

    Not by post-filtering the response, and not by baking clearance into an
    index — either of those is how a need-to-know layer springs a leak.
    """
    with Session(app_module.engine) as session:
        _ticket(session, title="Restricted incident", classification_code="restricted")
        # Positive control, on every engine. Without it both assertions here are
        # negative, and a provider returning nothing for an unrelated reason
        # would pass while proving no MAC guarantee at all.
        _ticket(session, title="Restricted printer tray")
        viewer = session.get(type(agents["viewer"]), agents["viewer"].id)

        results = asas_search.search(session, viewer, "Restricted")
        titles = {h.title for h in results.get("ticket") or []}

        assert "Restricted printer tray" in titles, (
            "search returned nothing at all — the negative assertion below would "
            "have passed without exercising need-to-know"
        )
        assert "Restricted incident" not in titles, (
            "a ticket classified above the caller's clearance was returned by search"
        )


def test_internal_note_is_never_searchable(app_module, agents):
    """The index is a write-time copy, so a restricted field must never enter it.

    This is a *structural* guarantee, not a filter: searching the exact text of
    an internal note finds nothing, for anybody, including an admin.
    """
    with Session(app_module.engine) as session:
        _ticket(session, title="Laptop swap", internal_note="ZZQX customer is hostile")
        admin = session.get(type(agents["admin"]), agents["admin"].id)

        # Positive control first: the ticket IS findable by its title, so a nil
        # result for the note text means the note is absent from the index —
        # not that search is broken.
        assert asas_search.search(session, admin, "Laptop").get("ticket")
        assert not asas_search.search(session, admin, "ZZQX").get("ticket")


def test_a_classified_ticket_is_404_not_403(client, app_module, agents):
    """Telling an unauthorized caller the record exists is itself the leak."""
    with Session(app_module.engine) as session:
        ticket = _ticket(session, classification_code="restricted")
        ticket_id = ticket.id

    assert client.get(f"/tickets/{ticket_id}").status_code == 404


def test_notification_recipients_are_filtered_by_clearance(app_module, agents):
    """A notification is a copy, so filtering has to happen *before* the write.

    There is no redaction pass afterwards: by then the title is already in
    somebody's inbox.
    """
    with Session(app_module.engine) as session:
        ticket = _ticket(session, classification_code="restricted")

        asas_notifications.notify(
            session,
            [agents["viewer"].id],
            "ticket.assigned",
            topic="tickets",
            nature=asas_notifications.Nature.action,
            urgency=asas_notifications.Urgency.normal,
            reason=asas_notifications.Reason.participant,
            title="Restricted ticket assigned",
            entity_type="ticket",
            entity_id=ticket.id,
            record=ticket,
        )
        session.commit()

        assert not _notifications_for(session, agents["viewer"].id)


# --------------------------------------------------------------------------
# Composition 3: an async notification
#   jobs handler + notifications dispatch
# --------------------------------------------------------------------------


def test_sla_sweep_notifies_through_the_queue(app_module, agents):
    """The whole path: enqueue -> claim -> handler -> notification row.

    `run_once` is the test-facing half of the runner the host configured with
    `poll_seconds=0`, which is how a queue stays drivable without a background
    thread in the suite.
    """
    from app.wiring.jobs import KIND_SLA_SWEEP

    with Session(app_module.engine) as session:
        _ticket(
            session,
            assignee_id=agents["agent"].id,
            due_on=date.today() - timedelta(days=1),
        )
        asas_jobs.enqueue(session, KIND_SLA_SWEEP)
        session.commit()

    asas_jobs.run_once()

    with Session(app_module.engine) as session:
        titles = [n.title for n in _notifications_for(session, agents["agent"].id)]
        assert any("past its due date" in t for t in titles)


def test_the_sweep_is_idempotent(app_module, agents):
    """At-least-once delivery means a handler runs twice sooner or later.

    Idempotence here is a property of the sweep's query rather than a flag, and
    that is the version that survives someone editing the handler later.
    """
    from app.wiring.jobs import KIND_SLA_SWEEP

    with Session(app_module.engine) as session:
        _ticket(
            session,
            assignee_id=agents["agent"].id,
            due_on=date.today() - timedelta(days=1),
        )
        for _ in range(2):
            asas_jobs.enqueue(session, KIND_SLA_SWEEP)
        session.commit()

    asas_jobs.run_once()
    asas_jobs.run_once()

    with Session(app_module.engine) as session:
        breaches = [
            n
            for n in _notifications_for(session, agents["agent"].id)
            if "past its due date" in n.title
        ]
        assert len(breaches) == 1, f"the sweep produced {len(breaches)} rows, not 1"


def test_sla_notification_rows_carry_the_hosts_org(app_module, agents):
    """Rows stamped by the *resolver* carry the host's org, not a swapped one.

    The regression this pins: the context resolver's contract is
    ``(user_id, org_id)`` and an early version returned ``(org_id, actor)``.
    Nothing failed loudly — the emit succeeded, the row was written — but it
    carried org 0, and the org-scoped feed queries then filtered it out. The
    sweep's emit passes no explicit ``org_id``, so it exercises exactly the
    resolver path.
    """
    from app.wiring.jobs import KIND_SLA_SWEEP

    with Session(app_module.engine) as session:
        _ticket(
            session,
            assignee_id=agents["agent"].id,
            due_on=date.today() - timedelta(days=1),
        )
        asas_jobs.enqueue(session, KIND_SLA_SWEEP)
        session.commit()

    asas_jobs.run_once()

    with Session(app_module.engine) as session:
        breaches = [
            n
            for n in _notifications_for(session, agents["agent"].id)
            if "past its due date" in n.title
        ]
        assert breaches, "the sweep emitted nothing"
        assert all(n.org_id == DEFAULT_ORG_ID for n in breaches), (
            f"resolver-stamped rows carry org {[n.org_id for n in breaches]}, "
            f"not the host's org {DEFAULT_ORG_ID} — the resolver tuple is "
            f"probably reversed"
        )


def test_the_schedule_spawned_sweep_binds_its_context(client, app_module):
    """An org-carrying job must survive the context binder.

    The runner calls the binder ``fn(session, org_id)`` — and only for jobs
    that *carry* an org. The host's binder had signature ``(org_id, **kwargs)``,
    so every schedule-spawned sweep (stamped with its schedule's org) died at
    bind time with a TypeError before its handler ran, forever — while the
    suite's hand-enqueued, org-less jobs skipped the binder and passed.
    """
    from asas_jobs.models import BackgroundJob

    asas_jobs.run_once()  # ticks the seeded schedule, spawning an org-carrying job

    with Session(app_module.engine) as session:
        spawned = session.exec(
            select(BackgroundJob).where(BackgroundJob.org_id.is_not(None))
        ).all()
        assert spawned, "the seeded schedule spawned no org-carrying job"
        failed = [j for j in spawned if j.last_error]
        assert not failed, (
            f"org-carrying jobs failed at the binder: "
            f"{[j.last_error for j in failed]}"
        )


def test_sla_breach_is_visible_in_the_assignees_own_feed(fake_auth_app):
    """The whole read path: resolver identity -> org scope -> the feed router.

    The direct-query tests above cannot catch a resolver that misidentifies
    the caller — ``/me/notifications`` derives "me" from the context resolver,
    so a resolver reporting a constant serves everybody the same inbox (the
    reversed tuple served whichever agent's id equalled the org's). Driven
    through HTTP with two different tokens: the assignee sees the breach, a
    bystander does not.
    """
    from fastapi.testclient import TestClient
    from app.models import Agent
    from app.wiring.jobs import KIND_SLA_SWEEP

    with TestClient(fake_auth_app.app) as http:  # lifespan seeds the demo agents
        with Session(fake_auth_app.engine) as session:
            sam = session.exec(
                select(Agent).where(Agent.email == "agent@example.invalid")
            ).one()
            _ticket(
                session,
                assignee_id=sam.id,
                due_on=date.today() - timedelta(days=1),
            )
            asas_jobs.enqueue(session, KIND_SLA_SWEEP)
            session.commit()

        asas_jobs.run_once()

        feed = http.get(
            "/me/notifications", headers={"Authorization": "Bearer token-agent"}
        )
        assert feed.status_code == 200
        assert any(
            "past its due date" in item["title"] for item in feed.json()["items"]
        ), "the assignee's own feed does not show the breach"

        bystander = http.get(
            "/me/notifications", headers={"Authorization": "Bearer token-viewer"}
        )
        assert bystander.status_code == 200
        assert not any(
            "past its due date" in item["title"] for item in bystander.json()["items"]
        ), "another user's feed shows the assignee's breach"


# --------------------------------------------------------------------------
# The single-package seams that still need a host to be visible
# --------------------------------------------------------------------------


def test_restricted_field_is_redacted_for_a_viewer(app_module, agents):
    """Field permissions, applied by one `redact_view` call rather than by
    per-field branching in the router."""
    from app.routers.tickets import _read_model

    with Session(app_module.engine) as session:
        ticket = _ticket(session, internal_note="candid assessment")
        viewer = session.get(type(agents["viewer"]), agents["viewer"].id)

        assert _read_model(session, viewer, ticket).internal_note is None


def test_the_assignee_sees_their_own_ticket_note(app_module, agents):
    """The relationship principal: a right a role alone cannot express.

    Sam is a plain member, so the grant that reaches them is `ticket_assignee` —
    resolved per (user, record) by the host's resolver.
    """
    from app.routers.tickets import _read_model

    with Session(app_module.engine) as session:
        sam = session.get(type(agents["agent"]), agents["agent"].id)
        ticket = _ticket(session, internal_note="candid", assignee_id=sam.id)

        assert _read_model(session, sam, ticket).internal_note == "candid"


def test_validation_rejects_an_incoherent_due_date(client):
    """A semantic rule, declared once, surfacing as a native 422."""
    response = client.post(
        "/tickets",
        json={
            "title": "Backwards",
            "due_on": str(date.today() - timedelta(days=30)),
        },
    )
    assert response.status_code == 422


def test_unconfigured_verb_is_admin_only(app_module, agents):
    """`ticket.classify` has no grant rows, so only admin holds it.

    The safe default, and worth pinning: a verb someone forgot to configure must
    close, never open.
    """
    with Session(app_module.engine) as session:
        admin = session.get(type(agents["admin"]), agents["admin"].id)
        member = session.get(type(agents["agent"]), agents["agent"].id)

        assert asas_access.action_allowed(session, admin, "ticket.classify")
        assert not asas_access.action_allowed(session, member, "ticket.classify")


def test_classifying_needs_the_verb_not_just_edit_rights(client, app_module):
    """A regression, found by driving the running app rather than the engine.

    `test_unconfigured_verb_is_admin_only` above asserts the *engine* answers
    correctly — and it passed while the router never asked. `classification_code`
    has no field-permission rows, so `forbidden_edits` allows it under the
    safe-by-default rule, and a plain member could stamp a ticket restricted.

    The lesson generalises past this file: a test that exercises a package
    directly cannot tell you whether the host called it.
    """
    with Session(app_module.engine) as session:
        ticket_id = _ticket(session).id

    response = client.patch(
        f"/tickets/{ticket_id}", json={"classification_code": "restricted"}
    )

    assert response.status_code == 403, (
        "classification_code was accepted without the ticket.classify verb"
    )


# --------------------------------------------------------------------------
# The optional tier. Untested until CodeRabbit pointed out that registering a
# provider is not the same as having one that answers.
# --------------------------------------------------------------------------


def test_deep_search_index_is_populated_and_answers(client, app_module):
    """Postgres only: prove the FTS arm actually returns hits.

    The trap this pins: the deep provider was *registered* and completely inert
    — no extractor ever ran, `search_document` held zero rows, and every search
    was quietly answered by the portable provider alone. Both prior assertions
    were negative ("no hits"), so they passed either way.

    The query is chosen so only the deep arm can match: "emitting" appears
    nowhere literally, so the portable `ilike` provider cannot find it, and only
    stemming in the FTS index can. A `TIER_CONTENT` hit is the proof.
    """
    if app_module.engine.dialect.name != "postgresql":
        pytest.skip("deep search is the Postgres-only tier")

    import sqlalchemy as sa

    with Session(app_module.engine) as session:
        _ticket(session, title="Widget failure", body="the apparatus emits smoke")

        indexed = session.execute(
            sa.text("SELECT count(*) FROM search_document")
        ).scalar()
        assert indexed, "the write listener indexed nothing"

        hits = asas_search.search(session, None, "emitting").get("ticket") or []

    assert hits, "the deep arm found nothing for a stem-only query"
    assert any(h.rank_tier == asas_search.TIER_CONTENT for h in hits), (
        f"expected a TIER_CONTENT hit from the FTS arm, got tiers "
        f"{[h.rank_tier for h in hits]} — the portable provider answered instead"
    )


def test_mcp_tools_apply_need_to_know(client, app_module, monkeypatch):
    """The MCP surface must not be a way around MAC.

    An MCP tool is a thin allowlist over capability the host already has, which
    means it inherits the host's *checks*. Querying the table directly and
    skipping `mac_allows` would give the protocol surface different permissions
    from the REST API it mirrors.
    """
    monkeypatch.setenv("MCP_TOKEN", "secret")
    from app.wiring import mcp as mcp_wiring

    with Session(app_module.engine) as session:
        open_id = _ticket(session, title="Ordinary jam").id
        secret_id = _ticket(
            session, title="Ordinary looking", classification_code="restricted"
        ).id

    found = {t["id"] for t in mcp_wiring._run_tool(None, "search_tickets", {"query": "Ordinary"})}
    assert open_id in found
    assert secret_id not in found, "MCP search returned a classified ticket"

    assert mcp_wiring._run_tool(None, "get_ticket", {"ticket_id": secret_id}) == {
        "error": "not found"
    }


def test_mcp_endpoint_verifies_its_token(monkeypatch):
    """Without a verifier the endpoint mounts with no authentication at all."""
    import asyncio

    monkeypatch.setenv("MCP_TOKEN", "secret")
    import importlib

    import app.config
    from app.wiring import mcp as mcp_wiring

    importlib.reload(app.config)
    importlib.reload(mcp_wiring)
    try:
        verifier = mcp_wiring._StaticTokenVerifier()
        assert asyncio.run(verifier.verify_token("secret")) is not None
        assert asyncio.run(verifier.verify_token("wrong")) is None
    finally:
        # monkeypatch restores the env var, but not these module objects — they
        # would keep the token and leak into any later test that does not use
        # the app_module fixture.
        #
        # `undo()` rather than `delenv`: it puts MCP_TOKEN back to whatever it
        # was *before* this test, which is not necessarily unset. Deleting it
        # and then reloading would initialise both modules from a state the
        # process was never actually in.
        monkeypatch.undo()
        importlib.reload(app.config)
        importlib.reload(mcp_wiring)


def test_overlapping_sweeps_announce_a_breach_once(app_module, agents):
    """The race the sequential idempotence test above cannot reach.

    Two sweeps running *concurrently* — what an at-least-once queue produces
    whenever a lease is reclaimed mid-run — both read "not yet notified" before
    either writes. The old read-then-write check passed the sequential test and
    would have produced two notifications here.

    Simulated by interleaving two sessions by hand: both claim, only one may win.
    """
    from sqlalchemy.exc import IntegrityError
    from app.models import SlaNotice

    with Session(app_module.engine) as session:
        ticket = _ticket(
            session,
            assignee_id=agents["agent"].id,
            due_on=date.today() - timedelta(days=1),
        )
        ticket_id = ticket.id

    # Two independent sessions, both claiming the same ticket before either
    # commits — the shape a reclaimed lease produces.
    with Session(app_module.engine) as a, Session(app_module.engine) as b:
        a.add(SlaNotice(ticket_id=ticket_id))
        a.commit()

        b.add(SlaNotice(ticket_id=ticket_id))
        with pytest.raises(IntegrityError):
            b.commit()
        b.rollback()

    with Session(app_module.engine) as session:
        claims = session.exec(
            select(SlaNotice).where(SlaNotice.ticket_id == ticket_id)
        ).all()

    assert len(claims) == 1, (
        f"{len(claims)} claims survived — the uniqueness constraint is not "
        f"arbitrating, so two sweeps could both announce this breach"
    )
