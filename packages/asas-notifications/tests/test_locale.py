"""The locale seam: stamped at emit, because dispatch has nobody to ask.

Every test here exists because the alternative design fails silently. A host
that resolved language at dispatch would find `current_user_id` returning None
by contract, get the deployment default, and send a correct-looking email in the
wrong language.
"""

import asas_notifications as notifications
from asas_notifications.models import Notification, NotificationDelivery
from sqlmodel import select

from conftest import emit


def test_unconfigured_leaves_locale_null_and_changes_nothing(session, kind):
    """Additive by default: an existing deployment sees no difference."""
    (row,) = emit(session, kind, [1])
    assert row.locale is None


def test_the_resolver_is_asked_per_recipient(session, kind):
    """One emit can fan out to people who read different languages, so this
    cannot be hoisted out of the recipient loop. A per-EMIT resolution would
    give everyone the first recipient's language, which is the kind of bug that
    looks like a translation problem for months."""
    languages = {1: "ar", 2: "en-GB"}
    notifications.configure_locale_resolver(lambda s, user_id: languages.get(user_id))

    rows = emit(session, kind, [1, 2])
    assert {r.user_id: r.locale for r in rows} == {"1": "ar", "2": "en-GB"}


def test_a_recipient_the_host_knows_nothing_about_is_not_an_error(session, kind):
    """None is a legitimate answer, not a failure: a subject with no account
    row, or one that has expressed no preference, is a recipient the host has
    nothing to say about. An adapter reads it as the deployment default."""
    notifications.configure_locale_resolver(lambda s, user_id: None)
    (row,) = emit(session, kind, [1])
    assert row.locale is None


def test_an_explicit_locale_beats_the_resolver(session, kind):
    """A producer that already holds the answer should not pay for a lookup,
    and a digest job rendering in one language should be able to say so."""
    notifications.configure_locale_resolver(lambda s, user_id: "en")
    (row,) = emit(session, kind, [1], locale="ar")
    assert row.locale == "ar"


def test_the_adapter_receives_it(session, kind, engine):
    """The half that makes the column worth having.

    Stamping a language nothing downstream can read achieves nothing, and
    dispatch is the only place that can read it: it runs outside any request,
    which is the whole reason the value is on the row.
    """
    seen = []

    class _Capture:
        def send(self, payload):
            seen.append(payload)

    notifications.configure_locale_resolver(lambda s, user_id: "ar-AE")
    notifications.register_adapter("email", _Capture())
    emit(session, kind, [1])

    handled = notifications.dispatch_pending(engine)
    assert handled == 1, "the dispatch pass did nothing"
    assert [p.locale for p in seen] == ["ar-AE"], (
        "the language never reached the adapter, so nothing can render with it"
    )


def test_configuring_none_turns_it_back_off(session, kind):
    notifications.configure_locale_resolver(lambda s, user_id: "ar")
    notifications.configure_locale_resolver(None)
    (row,) = emit(session, kind, [1])
    assert row.locale is None
