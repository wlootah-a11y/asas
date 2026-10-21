"""What Graph is sent, and how each of its answers is reported.

No network: the ``asas-graph`` client runs over an ``httpx.MockTransport``
with a static token, the same way that package tests itself.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import httpx
import pytest
from asas_graph import GraphClient, GraphSettings, StaticTokenProvider

from asas_mail import Attachment, MailDeliveryError, MailNotConfigured, Message
from asas_mail.graph import GraphTransport, send_mail_payload


@dataclass
class FakeGraph:
    status: int = 202
    body: object = None
    headers: dict[str, str] = field(default_factory=dict)
    calls: list[httpx.Request] = field(default_factory=list)
    fail: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail is not None:
            raise self.fail
        if self.body is None:
            return httpx.Response(self.status, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def transport(fake: FakeGraph, **kw) -> GraphTransport:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    client = GraphClient(
        GraphSettings(tenant_id="t", client_id="c", client_secret="s", base_url="https://graph.example/v1.0"),
        token_provider=StaticTokenProvider("app-only-token"),
        http=http,
    )
    kw.setdefault("sender", "no-reply@example.gov")
    return GraphTransport(client, **kw)


def message(**over) -> Message:
    base = dict(to="layla@example.test", subject="Hello", html="<p>Hi</p>")
    base.update(over)
    return Message(**base)


async def test_graph_gets_sendmail_as_the_shared_mailbox_with_the_html_body() -> None:
    fake = FakeGraph()

    await transport(fake).deliver(message())

    (request,) = fake.calls
    assert str(request.url) == "https://graph.example/v1.0/users/no-reply@example.gov/sendMail"
    assert request.headers["authorization"] == "Bearer app-only-token"
    body = json.loads(request.content)
    assert body["saveToSentItems"] is True
    assert body["message"]["subject"] == "Hello"
    assert body["message"]["body"] == {"contentType": "HTML", "content": "<p>Hi</p>"}
    assert body["message"]["toRecipients"] == [{"emailAddress": {"address": "layla@example.test"}}]
    assert "ccRecipients" not in body["message"] and "attachments" not in body["message"]


def test_the_payload_carries_copies_reply_to_and_files_base64() -> None:
    payload = send_mail_payload(
        message(
            cc="a@b.co", bcc=["c@d.co"], reply_to="r@s.co",
            attachments=[Attachment("report.pdf", b"%PDF-1.4", "application/pdf")],
        ),
        save_to_sent_items=False,
    )

    m = payload["message"]
    assert payload["saveToSentItems"] is False
    assert m["ccRecipients"] == [{"emailAddress": {"address": "a@b.co"}}]
    assert m["bccRecipients"] == [{"emailAddress": {"address": "c@d.co"}}]
    assert m["replyTo"] == [{"emailAddress": {"address": "r@s.co"}}]
    assert m["attachments"] == [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "report.pdf",
            "contentType": "application/pdf",
            "contentBytes": base64.b64encode(b"%PDF-1.4").decode(),
        }
    ]


@pytest.mark.parametrize(
    ("status", "transient"),
    [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
async def test_a_graph_refusal_is_a_delivery_error_that_says_whether_to_retry(status, transient) -> None:
    fake = FakeGraph(status=status, body={"error": {"code": "ErrorAccessDenied", "message": "nope"}})

    with pytest.raises(MailDeliveryError) as failed:
        await transport(fake).deliver(message())

    assert failed.value.status == status
    assert failed.value.transient is transient
    assert "ErrorAccessDenied" in str(failed.value)
    assert "nope" not in str(failed.value), "never the body"


async def test_a_throttle_carries_graphs_retry_after() -> None:
    fake = FakeGraph(status=429, headers={"Retry-After": "7"})

    with pytest.raises(MailDeliveryError) as failed:
        await transport(fake).deliver(message())

    assert failed.value.transient and failed.value.retry_after == 7


async def test_a_network_failure_is_a_transient_delivery_error() -> None:
    fake = FakeGraph(fail=httpx.ConnectError("no route"))

    with pytest.raises(MailDeliveryError) as failed:
        await transport(fake).deliver(message())

    assert failed.value.transient and failed.value.status is None


def test_a_transport_without_a_sender_is_not_configured() -> None:
    with pytest.raises(MailNotConfigured):
        transport(FakeGraph(), sender="")
