"""Render then send, the redirect, and the process-wide seam."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import asas_mail
from asas_mail import (
    MailDeliveryError,
    Mailer,
    MailNotConfigured,
    OffTransport,
    RecordingTransport,
    Renderer,
)


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    (tmp_path / "welcome.html").write_text("<p>Hello {{ first_name }}: {{ default_password }}</p>")
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_seam():
    asas_mail.set_mailer(None)
    asas_mail.configure(lambda: Mailer(OffTransport("test default")))
    yield
    asas_mail.set_mailer(None)


async def test_send_renders_the_template_for_the_recipient(templates: Path) -> None:
    recorder = RecordingTransport()
    mailer = Mailer(recorder, Renderer(templates))

    await mailer.send(
        to="new.user@example.gov", subject="Welcome", template="welcome.html",
        variables={"first_name": "Noura", "default_password": "Pw-1234"},
    )

    sent = recorder.only
    assert sent.to == ("new.user@example.gov",)
    assert sent.subject == "Welcome"
    assert "Hello Noura: Pw-1234" in sent.html
    assert "sent to new.user@example.gov" in sent.html


async def test_the_redirect_pins_every_message_and_the_body_still_names_the_real_person(templates: Path) -> None:
    recorder = RecordingTransport()
    mailer = Mailer(recorder, Renderer(templates), redirect_to="qa-inbox@example.gov")

    await mailer.send(
        to="real.person@example.gov", subject="Welcome", template="welcome.html",
        variables={"first_name": "Real", "default_password": "pw"}, cc="boss@example.gov",
    )

    sent = recorder.only
    assert sent.to == ("qa-inbox@example.gov",)
    assert sent.cc == ()
    assert "real.person@example.gov" in sent.html


@pytest.mark.parametrize("override", ["", "@", "not-an-address", None])
async def test_no_usable_redirect_sends_to_the_real_address(templates: Path, override) -> None:
    recorder = RecordingTransport()
    mailer = Mailer(recorder, Renderer(templates), redirect_to=override)

    await mailer.send(to="real@example.gov", subject="s", template="welcome.html")

    assert recorder.only.to == ("real@example.gov",)


async def test_the_redirect_may_be_a_live_setting(templates: Path) -> None:
    live = {"to": ""}
    recorder = RecordingTransport()
    mailer = Mailer(recorder, Renderer(templates), redirect_to=lambda: live["to"])

    await mailer.send(to="real@example.gov", subject="s", template="welcome.html")
    live["to"] = "qa@example.gov"
    await mailer.send(to="real@example.gov", subject="s", template="welcome.html")

    assert [m.to for m in recorder.sent] == [("real@example.gov",), ("qa@example.gov",)]


async def test_an_off_mailer_is_a_skip_and_says_so(templates: Path) -> None:
    mailer = Mailer(OffTransport("mail_transport is off"), Renderer(templates))

    assert not mailer.is_configured
    with pytest.raises(MailNotConfigured, match="mail_transport is off"):
        await mailer.send(to="a@b.co", subject="s", template="welcome.html")


async def test_a_failing_transport_surfaces_as_a_delivery_error(templates: Path) -> None:
    class Broken:
        async def deliver(self, message) -> None:
            raise MailDeliveryError("graph sendMail 503", status=503)

    with pytest.raises(MailDeliveryError) as failed:
        await Mailer(Broken(), Renderer(templates)).send(to="a@b.co", subject="s", template="welcome.html")
    assert failed.value.status == 503 and failed.value.transient


async def test_the_body_is_never_logged(templates: Path, caplog) -> None:
    recorder = RecordingTransport()
    with caplog.at_level(logging.DEBUG, logger="asas_mail"):
        await Mailer(recorder, Renderer(templates), redirect_to="qa@example.gov").send(
            to="a@b.co", subject="s", template="welcome.html", variables={"default_password": "Pw-9"}
        )
    assert "Pw-9" not in caplog.text


# ── the process-wide seam ────────────────────────────────────────────────────


async def test_nothing_configured_is_an_off_mailer_not_an_attribute_error() -> None:
    asas_mail.configure(None)  # as a fresh process would have it

    assert not asas_mail.is_configured()
    with pytest.raises(MailNotConfigured, match="no mailer was configured"):
        await asas_mail.send(to="a@b.co", subject="s", template="welcome.html")


async def test_an_off_mailer_renders_nothing() -> None:
    """The template is not even looked up: a deployment that sends no mail
    must not fail on a template it never needed."""
    with pytest.raises(MailNotConfigured):
        await Mailer(OffTransport("off")).send(to="a@b.co", subject="s", template="missing.html")


async def test_configure_is_lazy_and_set_mailer_overrides(templates: Path) -> None:
    built = []

    def factory() -> Mailer:
        built.append(1)
        return Mailer(RecordingTransport(), Renderer(templates))

    asas_mail.configure(factory)
    assert built == [], "built on first use, not at configure time"

    first = asas_mail.mailer()
    assert built == [1] and asas_mail.mailer() is first

    recorder = RecordingTransport()
    asas_mail.set_mailer(Mailer(recorder, Renderer(templates)))
    await asas_mail.send(to="a@b.co", subject="s", template="welcome.html")
    assert recorder.only.to == ("a@b.co",)
    assert asas_mail.is_configured()

    asas_mail.set_mailer(None)
    assert asas_mail.mailer() is not first and built == [1, 1]
