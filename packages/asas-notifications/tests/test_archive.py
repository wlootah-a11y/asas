"""The archive axis and the composable list filters (Teamy TEAMY-693).

`read_at` and `archived_at` are independent on purpose: reading is seeing,
archiving is finishing. The tests that matter most here are the ones pinning that
independence — a host builds an "needs action" view on top of it, and it breaks
quietly if archiving starts implying read (or the reverse).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import asas_notifications as notifications
from asas_notifications import service
from asas_notifications.models import Notification

from conftest import emit


@pytest.fixture()
def client(migrated):
    notifications.configure_context_resolver(lambda s: (1, 1))
    app = FastAPI()

    def get_session():
        with Session(migrated) as s:
            yield s

    app.include_router(notifications.build_router(get_session))
    return TestClient(app)


# ── service ──────────────────────────────────────────────────────────────────


def test_archive_is_idempotent_and_reversible(session, kind):
    n = emit(session, kind, [1])[0]

    archived = service.archive(session, 1, n.id)
    stamped = archived.archived_at
    assert stamped is not None

    assert service.archive(session, 1, n.id).archived_at == stamped  # not re-stamped
    assert service.unarchive(session, 1, n.id).archived_at is None


def test_archiving_does_not_mark_read_and_reading_does_not_archive(session, kind):
    a = emit(session, kind, [1])[0]
    b = emit(session, kind, [1])[0]

    service.archive(session, 1, a.id)
    assert a.read_at is None  # archived, still never seen

    service.mark_read(session, 1, b.id)
    assert b.archived_at is None  # read, still in the inbox


def test_unarchive_leaves_read_state_alone(session, kind):
    n = emit(session, kind, [1])[0]
    service.mark_read(session, 1, n.id)
    service.archive(session, 1, n.id)

    restored = service.unarchive(session, 1, n.id)
    assert restored.archived_at is None
    assert restored.read_at is not None  # restoring is not "mark unread"


def test_archived_rows_leave_the_unread_count(session, kind):
    a = emit(session, kind, [1])[0]
    emit(session, kind, [1])
    assert service.unread_count(session, 1) == 2

    service.archive(session, 1, a.id)
    assert service.unread_count(session, 1) == 1  # no badge pointing at nothing

    service.unarchive(session, 1, a.id)
    assert service.unread_count(session, 1) == 2


def test_mark_all_read_covers_archived_rows_too(session, kind):
    """mark_all_read is a superset of what unread_count counts, so it can never
    leave the badge stuck above zero."""
    n = emit(session, kind, [1])[0]
    service.archive(session, 1, n.id)

    assert service.mark_all_read(session, 1) == 1
    assert session.get(Notification, n.id).read_at is not None


def test_archive_read_spares_unread_rows(session, kind):
    read_one = emit(session, kind, [1])[0]
    unread_one = emit(session, kind, [1])[0]
    service.mark_read(session, 1, read_one.id)

    assert service.archive_read(session, 1) == 1
    assert session.get(Notification, read_one.id).archived_at is not None
    assert session.get(Notification, unread_one.id).archived_at is None

    assert service.archive_read(session, 1) == 0  # nothing left to file


def test_coalescing_never_merges_into_an_archived_row(session, ambient_kind):
    """Archiving an *unread* coalescible row must not make it a sink for later
    events on the same entity: it has left the inbox, so a merge would land the
    event where the recipient can no longer see it."""
    first = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="v1",
    )[0]
    service.archive(session, 1, first.id)

    emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="v2",
    )

    rows = session.exec(
        select(Notification).where(Notification.user_id == "1")
    ).all()
    assert len(rows) == 2, "the archived row was reused instead of a fresh one"
    assert session.get(Notification, first.id).title == "v1"  # untouched
    live = [n for n in rows if n.archived_at is None]
    assert [n.title for n in live] == ["v2"]


def test_coalescing_still_merges_into_a_live_row(session, ambient_kind):
    """The counterpart: an un-archived unread row is still the coalescing target,
    so the archived-row fix did not disable coalescing outright."""
    first = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=7, coalesce_unread=True, title="v1",
    )[0]
    emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=7, coalesce_unread=True, title="v2",
    )

    rows = session.exec(
        select(Notification).where(Notification.user_id == "1")
    ).all()
    assert len(rows) == 1
    assert rows[0].id == first.id and rows[0].title == "v2"


def test_another_users_notification_is_not_reachable(session, kind):
    n = emit(session, kind, [2])[0]
    assert service.archive(session, 1, n.id) is None
    assert service.unarchive(session, 1, n.id) is None
    assert session.get(Notification, n.id).archived_at is None


def test_archive_read_is_scoped_to_the_recipient(session, kind):
    mine = emit(session, kind, [1])[0]
    theirs = emit(session, kind, [2])[0]
    service.mark_read(session, 1, mine.id)
    service.mark_read(session, 2, theirs.id)

    assert service.archive_read(session, 1) == 1
    assert session.get(Notification, theirs.id).archived_at is None


# ── router ───────────────────────────────────────────────────────────────────


def test_state_filter_splits_open_from_archived(session, kind, client):
    a = emit(session, kind, [1])[0]
    emit(session, kind, [1])

    assert client.post(f"/me/notifications/{a.id}/archive").status_code == 200

    assert client.get("/me/notifications").json()["total"] == 1  # open is the default
    assert client.get("/me/notifications?state=archived").json()["total"] == 1
    assert client.get("/me/notifications?state=all").json()["total"] == 2


def test_unread_count_ignores_the_requested_filters(session, kind, client):
    """Every view reports the same badge number, so a host can feed its badge from
    whichever list call it already made."""
    emit(session, kind, [1])
    emit(session, kind, [1])

    for query in ("", "?state=archived", "?state=all", "?unread_only=true"):
        assert client.get(f"/me/notifications{query}").json()["unread_count"] == 2


def test_the_two_state_axes_compose_and_reading_is_not_archiving(session, kind, client):
    """Read and archived are two axes, and each one composes with the other.

    This used to narrow on the presentation axis as well (Teamy's "needs
    action" view), which left the package in 0.18.0 along with the column: a
    host that keeps it on a sidecar row filters on it in its own query. The half
    that was never about that axis is the half that matters and is kept — a row
    stays in the inbox once READ, and archiving is what removes it.
    """
    first = emit(session, kind, [1])[0]
    emit(session, kind, [1])

    assert client.get("/me/notifications").json()["total"] == 2
    assert client.get("/me/notifications?unread_only=true").json()["total"] == 2

    # Read it: it stays in the open view, and leaves the unread one.
    client.post(f"/me/notifications/{first.id}/read")
    assert client.get("/me/notifications").json()["total"] == 2
    assert client.get("/me/notifications?unread_only=true").json()["total"] == 1

    # Archiving is what removes it.
    client.post(f"/me/notifications/{first.id}/archive")
    assert client.get("/me/notifications").json()["total"] == 1
    assert client.get("/me/notifications?state=archived").json()["total"] == 1


def test_archived_at_is_exposed_on_the_read_model(session, kind, client):
    n = emit(session, kind, [1])[0]
    assert client.get("/me/notifications").json()["items"][0]["archived_at"] is None

    client.post(f"/me/notifications/{n.id}/archive")
    item = client.get("/me/notifications?state=archived").json()["items"][0]
    assert item["archived_at"] is not None


def test_archive_read_endpoint_reports_what_it_filed(session, kind, client):
    n = emit(session, kind, [1])[0]
    emit(session, kind, [1])
    client.post(f"/me/notifications/{n.id}/read")

    assert client.post("/me/notifications/archive-read").json() == {"updated": 1}
    assert client.get("/me/notifications").json()["total"] == 1


def test_unknown_notification_is_404(client):
    assert client.post("/me/notifications/9999/archive").status_code == 404
    assert client.post("/me/notifications/9999/unarchive").status_code == 404


def test_bad_state_value_is_422(client):
    assert client.get("/me/notifications?state=nonsense").status_code == 422


def test_paging_applies_within_a_filtered_view(session, kind, client):
    rows = [emit(session, kind, [1])[0] for _ in range(4)]
    client.post(f"/me/notifications/{rows[0].id}/archive")

    page = client.get("/me/notifications?page=1&page_size=2").json()
    assert page["total"] == 3 and len(page["items"]) == 2

    page2 = client.get("/me/notifications?page=2&page_size=2").json()
    assert page2["total"] == 3 and len(page2["items"]) == 1

    seen = {i["id"] for i in page["items"]} | {i["id"] for i in page2["items"]}
    assert rows[0].id not in seen


def test_ordering_is_newest_first_across_states(session, kind, client):
    rows = [emit(session, kind, [1])[0] for _ in range(3)]
    ids = client.get("/me/notifications?state=all").json()["items"]
    assert [i["id"] for i in ids] == [r.id for r in reversed(rows)]


def test_feed_stays_scoped_to_the_recipient(session, kind, client):
    emit(session, kind, [1])
    emit(session, kind, [2])
    body = client.get("/me/notifications?state=all").json()
    assert body["total"] == 1
    assert session.exec(select(Notification)).all().__len__() == 2
