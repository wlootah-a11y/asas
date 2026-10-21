"""Where a message goes: a folder on a laptop, nowhere, or (in ``graph.py``)
a mailbox.

Every transport takes one :class:`Message` and either delivers it, raises
:class:`MailNotConfigured` (never retried) or raises
:class:`MailDeliveryError` (retried by the host's outbox). Nothing here
logs a body or a subject line's contents beyond what a developer opening the
file outbox would see anyway.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .errors import MailNotConfigured
from .message import Message

_logger = logging.getLogger("asas_mail")


class Transport(Protocol):
    async def deliver(self, message: Message) -> None: ...


class OffTransport:
    """Sends nothing, and says so as :class:`MailNotConfigured`.

    ``reason`` names what is missing, so the one log line a host writes on a
    skip can say "graph transport selected but graph_mail_sender is unset"
    rather than "off".
    """

    def __init__(self, reason: str = "mail is off") -> None:
        self.reason = reason

    async def deliver(self, message: Message) -> None:  # noqa: ARG002
        raise MailNotConfigured(self.reason)


class FileTransport:
    """Writes each message where a developer can open it. Sends nothing.

    Two files per message under one stem: ``<stem>.html`` is the body, and
    ``<stem>.json`` says who it was for (``to``, ``cc``, ``bcc``, the
    subject, and the names of any attachments, which are written beside
    them as ``<stem>.<n>.<name>``).
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    async def deliver(self, message: Message) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        (self.directory / f"{stem}.html").write_text(message.html, encoding="utf-8")
        for index, attachment in enumerate(message.attachments, start=1):
            (self.directory / f"{stem}.{index}.{attachment.name}").write_bytes(attachment.content)
        meta = {
            "to": list(message.to),
            "cc": list(message.cc),
            "bcc": list(message.bcc),
            "reply_to": message.reply_to,
            "subject": message.subject,
            "attachments": [a.name for a in message.attachments],
        }
        (self.directory / f"{stem}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _logger.info("mail.written_to_file to=%s file=%s.html", message.recipient, stem)


class RecordingTransport:
    """Keeps every message in memory. For a host's own tests."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def deliver(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def only(self) -> Message:
        assert len(self.sent) == 1, self.sent
        return self.sent[0]
