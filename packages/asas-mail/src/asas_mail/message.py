"""One email, ready to deliver: who, what, the rendered HTML, any files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .errors import MailNotConfigured


def _addresses(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    out: list[str] = []
    for address in value:
        address = (address or "").strip()
        if address and address not in out:
            out.append(address)
    return tuple(out)


def is_address(value: str | None) -> bool:
    """A loose shape test, enough to refuse an empty or obviously broken
    address before a mail service does. Not a validator."""
    if not value:
        return False
    local, sep, domain = value.strip().partition("@")
    return bool(sep and local and domain and "." in domain and " " not in value.strip())


@dataclass(frozen=True)
class Attachment:
    """A file on the message. ``content_type`` is what the recipient's client
    is told; it is not sniffed from the bytes."""

    name: str
    content: bytes = field(repr=False)
    content_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("an attachment needs a bare file name")


@dataclass(frozen=True)
class Message:
    """``to``, ``cc`` and ``bcc`` take one address or several; duplicates and
    blanks are dropped. A message with nobody in ``to`` is refused at
    construction as :class:`MailNotConfigured`, the skip kind of failure: there
    is no retry that gives it a recipient."""

    to: tuple[str, ...]
    subject: str
    html: str = field(repr=False)
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def __init__(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        html: str,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        reply_to: str | None = None,
        attachments: Iterable[Attachment] = (),
    ) -> None:
        recipients = _addresses(to)
        if not recipients:
            raise MailNotConfigured("the message names no recipient")
        for address in (*recipients, *_addresses(cc), *_addresses(bcc)):
            if not is_address(address):
                raise MailNotConfigured(f"{address!r} is not an email address")
        object.__setattr__(self, "to", recipients)
        object.__setattr__(self, "subject", (subject or "").strip())
        object.__setattr__(self, "html", html)
        object.__setattr__(self, "cc", _addresses(cc))
        object.__setattr__(self, "bcc", _addresses(bcc))
        object.__setattr__(self, "reply_to", (reply_to or "").strip() or None)
        object.__setattr__(self, "attachments", tuple(attachments))

    @property
    def recipient(self) -> str:
        """The first ``to`` address: the person the message is for."""
        return self.to[0]

    def redirected(self, mailbox: str) -> Message:
        """The same message pinned to ONE mailbox, for an environment whose
        real inboxes must not be written to. ``cc`` and ``bcc`` are dropped,
        because a copy still reaches a real person; the body is untouched,
        so it still names who it was for."""
        return Message(
            to=mailbox,
            subject=self.subject,
            html=self.html,
            reply_to=self.reply_to,
            attachments=self.attachments,
        )
