"""Delivery through Microsoft Graph ``sendMail``, on an ``asas-graph`` client.

Needs the ``graph`` extra. The package imports nothing from it until this
module is used.

The message is sent AS one mailbox (``sender``, a shared no-reply address) on
an app-only token. The permission is ``Mail.Send``, which an Exchange
Application Access Policy scopes to that one mailbox, so the application
cannot send as anybody else. Everything about the token, the TLS trust store,
throttling headers and Graph's error envelope is ``asas-graph``'s; this
module only builds the payload and maps the failure.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from asas_graph import GraphClient, GraphConfigError, GraphError, GraphRequestError

from .errors import MailDeliveryError, MailNotConfigured
from .message import Message

_logger = logging.getLogger("asas_mail")

#: Statuses that mean the mailbox or the permission is wrong, not the moment:
#: a retry repeats them. Everything else Graph answers is worth another try.
_SETTLED = frozenset({400, 401, 403, 404, 413})


def _recipients(addresses: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": address}} for address in addresses]


def send_mail_payload(message: Message, *, save_to_sent_items: bool) -> dict[str, Any]:
    """Graph's ``sendMail`` body for ``message``. Pure, so a host can assert
    on it."""
    body: dict[str, Any] = {
        "subject": message.subject,
        "body": {"contentType": "HTML", "content": message.html},
        "toRecipients": _recipients(message.to),
    }
    if message.cc:
        body["ccRecipients"] = _recipients(message.cc)
    if message.bcc:
        body["bccRecipients"] = _recipients(message.bcc)
    if message.reply_to:
        body["replyTo"] = _recipients((message.reply_to,))
    if message.attachments:
        body["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment.name,
                "contentType": attachment.content_type,
                "contentBytes": base64.b64encode(attachment.content).decode("ascii"),
            }
            for attachment in message.attachments
        ]
    return {"message": body, "saveToSentItems": save_to_sent_items}


class GraphTransport:
    """``POST /users/{sender}/sendMail`` through the given client.

    ``sender`` is the mailbox the message leaves from; it must be a real,
    licensed mailbox the application may send as. The client is the host's
    to close.
    """

    def __init__(
        self,
        client: GraphClient,
        *,
        sender: str,
        save_to_sent_items: bool = True,
    ) -> None:
        if not sender or "@" not in sender:
            raise MailNotConfigured("the Graph transport needs a sender mailbox")
        self._client = client
        self.sender = sender
        self.save_to_sent_items = save_to_sent_items

    async def deliver(self, message: Message) -> None:
        payload = send_mail_payload(message, save_to_sent_items=self.save_to_sent_items)
        path = f"/users/{self.sender}/sendMail"
        try:
            await self._client.post(path, payload)
        except GraphConfigError as exc:
            raise MailNotConfigured(f"graph sendMail: {exc}") from exc
        except GraphRequestError as exc:
            # Graph's error envelope names the problem (an unscoped mailbox, a
            # missing permission) and never the message, so the code is safe
            # to carry; the body is not carried.
            raise MailDeliveryError(
                f"graph sendMail {exc.status or 'no answer'}: {exc.code or type(exc).__name__}",
                status=exc.status or None,
                transient=exc.is_transient or exc.status not in _SETTLED,
                retry_after=exc.retry_after,
            ) from exc
        except GraphError as exc:
            # A token that could not be had, or a body that was not JSON.
            raise MailDeliveryError(f"graph sendMail: {type(exc).__name__}") from exc
        _logger.info("mail.sent via=graph to=%s sender=%s", message.recipient, self.sender)
