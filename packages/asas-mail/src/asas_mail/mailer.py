"""The thing a host calls: render a template for somebody and send it.

A :class:`Mailer` is a renderer plus a transport plus one rule, the
redirect. The host builds it from its own settings and either keeps it on an
object it owns or installs it process-wide with :func:`configure`, the
``asas-storage`` seam, so a welcome email in one module and an outbox adapter
in another reach the same mailer without either importing settings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from .errors import MailNotConfigured
from .message import Attachment, Message, is_address
from .rendering import Renderer
from .transports import OffTransport, Transport

_logger = logging.getLogger("asas_mail")

Redirect = str | Callable[[], str] | None


class Mailer:
    """``redirect_to`` pins every message to one mailbox, for an environment
    whose real inboxes must not be written to (a staging copy of production
    data). The body still names the real recipient. It is a value or a
    zero-argument callable read per send, so a live setting is honoured; a
    value that is not an address means no redirect."""

    def __init__(
        self,
        transport: Transport,
        renderer: Renderer | None = None,
        *,
        redirect_to: Redirect = None,
    ) -> None:
        self.transport = transport
        self.renderer = renderer or Renderer()
        self._redirect_to = redirect_to

    @property
    def is_configured(self) -> bool:
        """Whether a send can leave the process at all."""
        return not isinstance(self.transport, OffTransport)

    def redirect(self) -> str | None:
        value = self._redirect_to() if callable(self._redirect_to) else self._redirect_to
        value = (value or "").strip()
        return value if is_address(value) else None

    def message(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        template: str,
        variables: dict[str, Any] | None = None,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        reply_to: str | None = None,
        attachments: Iterable[Attachment] = (),
        shell: str | None = None,
    ) -> Message:
        """Render ``template`` for ``to`` into a :class:`Message`, unsent.

        The first recipient rides into the template as ``recipient``, so a
        shell can say who the message was for, which still reads true after
        a redirect.
        """
        recipients = (to,) if isinstance(to, str) else tuple(to)
        context = dict(variables or {})
        context.setdefault("recipient", recipients[0] if recipients else "")
        html = self.renderer.render(template, subject=subject, variables=context, shell=shell)
        return Message(
            to=to, subject=subject, html=html, cc=cc, bcc=bcc, reply_to=reply_to,
            attachments=attachments,
        )

    async def deliver(self, message: Message) -> None:
        """Send a message that is already rendered, honouring the redirect."""
        if isinstance(self.transport, OffTransport):
            raise MailNotConfigured(self.transport.reason)
        mailbox = self.redirect()
        if mailbox is not None:
            _logger.info("mail.redirected original=%s", message.recipient)
            message = message.redirected(mailbox)
        await self.transport.deliver(message)

    async def send(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        template: str,
        variables: dict[str, Any] | None = None,
        cc: str | Iterable[str] | None = None,
        bcc: str | Iterable[str] | None = None,
        reply_to: str | None = None,
        attachments: Iterable[Attachment] = (),
        shell: str | None = None,
    ) -> None:
        """Render and send. Raises :class:`MailNotConfigured` when this
        deployment sends no mail and :class:`MailDeliveryError` when a send
        failed. The body is never logged. An ``off`` transport refuses BEFORE
        rendering, so a deployment that sends no mail renders none either."""
        if isinstance(self.transport, OffTransport):
            raise MailNotConfigured(self.transport.reason)
        await self.deliver(
            self.message(
                to=to, subject=subject, template=template, variables=variables, cc=cc,
                bcc=bcc, reply_to=reply_to, attachments=attachments, shell=shell,
            )
        )


# ── the process-wide seam (the asas-storage shape) ───────────────────────────

_factory: Callable[[], Mailer] | None = None
_mailer: Mailer | None = None


def configure(factory: Callable[[], Mailer] | None) -> None:
    """Install the host's mailer factory, called lazily on the first
    :func:`mailer` call. The library reads no configuration itself. ``None``
    puts the process back to the ``off`` default."""
    global _factory, _mailer
    _factory = factory
    _mailer = None


def mailer() -> Mailer:
    """The process-wide mailer. With nothing configured it is an ``off``
    mailer, so a send is a :class:`MailNotConfigured` and never an
    AttributeError in a code path nobody exercised."""
    global _mailer
    if _mailer is None:
        _mailer = _factory() if _factory is not None else Mailer(
            OffTransport("no mailer was configured")
        )
    return _mailer


def set_mailer(instance: Mailer | None) -> None:
    """Override the mailer (a test points it at a ``RecordingTransport``);
    ``None`` resets to lazy re-selection through the factory."""
    global _mailer
    _mailer = instance


async def send(**kwargs: Any) -> None:
    """``mailer().send(...)``."""
    await mailer().send(**kwargs)


def is_configured() -> bool:
    return mailer().is_configured


__all__ = [
    "Mailer",
    "MailNotConfigured",
    "configure",
    "is_configured",
    "mailer",
    "send",
    "set_mailer",
]
