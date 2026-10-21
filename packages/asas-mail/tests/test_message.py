"""A message is addressed at construction, and a redirect keeps the body."""

from __future__ import annotations

import pytest

from asas_mail import Attachment, MailNotConfigured, Message, is_address


def test_recipients_are_trimmed_and_deduplicated() -> None:
    m = Message(to=[" a@b.co ", "a@b.co", "", "c@d.co"], subject=" Hi ", html="<p>x</p>")

    assert m.to == ("a@b.co", "c@d.co")
    assert m.recipient == "a@b.co"
    assert m.subject == "Hi"
    assert m.cc == () and m.bcc == () and m.reply_to is None and m.attachments == ()


def test_one_address_or_several() -> None:
    assert Message(to="a@b.co", subject="s", html="h").to == ("a@b.co",)
    assert Message(to="a@b.co", subject="s", html="h", cc="c@d.co", bcc=["e@f.co"]).cc == ("c@d.co",)


def test_nobody_to_send_to_is_a_skip_not_a_retry() -> None:
    with pytest.raises(MailNotConfigured):
        Message(to=[], subject="s", html="h")
    with pytest.raises(MailNotConfigured):
        Message(to="  ", subject="s", html="h")


@pytest.mark.parametrize("bad", ["not-an-address", "@", "a@b", "a b@c.co", "@c.co"])
def test_a_broken_address_is_refused_before_a_mail_service_sees_it(bad: str) -> None:
    assert not is_address(bad)
    with pytest.raises(MailNotConfigured):
        Message(to=bad, subject="s", html="h")
    with pytest.raises(MailNotConfigured):
        Message(to="a@b.co", subject="s", html="h", cc=bad)


def test_redirect_pins_to_one_mailbox_and_drops_the_copies() -> None:
    original = Message(
        to="real@b.co", subject="s", html="<p>for real@b.co</p>", cc="boss@b.co", bcc="log@b.co",
        reply_to="r@b.co", attachments=[Attachment("a.txt", b"x", "text/plain")],
    )

    pinned = original.redirected("qa@b.co")

    assert pinned.to == ("qa@b.co",)
    assert pinned.cc == () and pinned.bcc == ()
    assert pinned.html == original.html, "the body still names who it was for"
    assert pinned.reply_to == "r@b.co"
    assert pinned.attachments == original.attachments


def test_an_attachment_needs_a_bare_file_name() -> None:
    with pytest.raises(ValueError):
        Attachment("../etc/passwd", b"x")
    with pytest.raises(ValueError):
        Attachment("", b"x")
    assert Attachment("report.pdf", b"%PDF", "application/pdf").content_type == "application/pdf"
