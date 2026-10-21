"""A folder on a laptop, nowhere, and the recorder a host's tests use."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asas_mail import (
    Attachment,
    FileTransport,
    MailNotConfigured,
    Message,
    OffTransport,
    RecordingTransport,
)


def message(**over) -> Message:
    base = dict(to="layla@example.test", subject="Your code", html="<p>Your code is 004182.</p>")
    base.update(over)
    return Message(**base)


async def test_off_says_so_as_not_configured_and_names_why() -> None:
    with pytest.raises(MailNotConfigured, match="graph_mail_sender is unset"):
        await OffTransport("graph transport selected but graph_mail_sender is unset").deliver(message())


async def test_the_file_transport_writes_the_message_and_sends_nothing(tmp_path: Path) -> None:
    await FileTransport(tmp_path / "outbox").deliver(
        message(cc="boss@example.test", attachments=[Attachment("code.txt", b"004182", "text/plain")])
    )

    outbox = tmp_path / "outbox"
    (html_file,) = outbox.glob("*.html")
    (meta_file,) = outbox.glob("*.json")
    (attachment,) = outbox.glob("*.code.txt")
    assert "004182" in html_file.read_text()
    assert attachment.read_bytes() == b"004182"
    assert json.loads(meta_file.read_text()) == {
        "to": ["layla@example.test"],
        "cc": ["boss@example.test"],
        "bcc": [],
        "reply_to": None,
        "subject": "Your code",
        "attachments": ["code.txt"],
    }


async def test_every_message_gets_its_own_files(tmp_path: Path) -> None:
    transport = FileTransport(tmp_path)
    await transport.deliver(message())
    await transport.deliver(message())

    assert len(list(tmp_path.glob("*.html"))) == 2


async def test_the_recorder_keeps_messages_for_a_hosts_tests() -> None:
    transport = RecordingTransport()
    await transport.deliver(message())

    assert transport.only.recipient == "layla@example.test"
