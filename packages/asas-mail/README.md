# asas-mail

Render a message from your own templates and send it, through one seam. A
Microsoft Graph `sendMail` transport (on an `asas-graph` client) for a
deployment, a folder for a laptop, and an `off` transport that refuses as a
skip rather than as a failure to retry.

Table-less and router-less variant of the Asas host contract: no session, no
`migrate`, no `seed`, no `build_router`. The one host hook is `configure`,
which installs the host's `Mailer` factory (the `asas-storage` shape); a host
may equally keep a `Mailer` on an object it owns and never call it. Jinja2 is
the only dependency; the Graph transport is the `graph` extra, imported on
first use.

## Quickstart

```
asas-mail[graph] @ git+https://github.com/wlootah-a11y/asas.git@asas-mail/v0.1.0#subdirectory=packages/asas-mail
```

```python
import asas_graph
import asas_mail

def build_mailer() -> asas_mail.Mailer:
    # read *your* settings; the library reads no configuration itself
    renderer = asas_mail.Renderer(settings.mail_templates_dir)   # your Jinja files
    if settings.mail_transport == "graph":
        graph = asas_graph.GraphClient(asas_graph.GraphSettings(
            tenant_id=settings.graph_tenant_id,
            client_id=settings.graph_client_id,
            client_secret=settings.graph_client_secret,
        ))
        transport = asas_mail.GraphTransport(graph, sender=settings.mail_sender)
    elif settings.mail_transport == "file":
        transport = asas_mail.FileTransport(settings.mail_outbox_dir)
    else:
        transport = asas_mail.OffTransport("mail_transport is off")
    return asas_mail.Mailer(transport, renderer, redirect_to=lambda: settings.mail_redirect_to)

asas_mail.configure(build_mailer)   # lazy: built on the first send

# anywhere in the host
await asas_mail.send(
    to="new.user@example.gov",
    subject="Welcome",
    template="welcome.html",
    variables={"first_name": "Noura", "role": "Recruiter"},
)
```

A test points the seam at a recorder:

```python
recorder = asas_mail.RecordingTransport()
asas_mail.set_mailer(asas_mail.Mailer(recorder, asas_mail.Renderer(templates_dir)))
...
assert "Noura" in recorder.only.html
```

## What it gets right, so you do not have to

- **"No mailer here" is a skip, not a retry.** An outbox retries a failed
  delivery up to a cap. That is right for a mailbox that answered 503 and
  wrong for a deployment with no mailer at all, where every row retries to
  the cap against something that will never answer. `MailNotConfigured` is
  raised before any wire is touched (the `off` transport, a transport
  missing a value it needs, a message with nobody to send to) and a host
  treats it as "not sent"; `MailDeliveryError` is the one worth retrying,
  and it says whether a retry is likely to help (`transient`), the status,
  and the service's own `retry_after`.
- **An `off` mailer renders nothing.** The template is not even looked up,
  so a deployment that sends no mail cannot fail on a template it never
  needed.
- **The redirect pins every message to one mailbox** for an environment
  whose real inboxes must not be written to (a staging copy of production
  data). `cc` and `bcc` are dropped too, because a copy still reaches a real
  person; the body is untouched, so it still names who it was for. The
  value may be a callable read per send, so a live setting is honoured, and
  a value that is not an address means no redirect.
- **The body is never logged.** One message may carry a password or an
  entry code. A Graph refusal carries Graph's error code (an unscoped
  mailbox, a missing permission) and never the message or the response
  body.
- **Graph `sendMail` is one POST as one mailbox** on an app-only token, with
  copies, reply-to and file attachments in Graph's own shape;
  `send_mail_payload` is pure so a host can assert on it. The permission is
  `Mail.Send`, which an Exchange Application Access Policy scopes to that one
  mailbox, so the application cannot send as anybody else. The token, the
  trust store, throttling and the error envelope are `asas-graph`'s.
- **Every variable reaches a template as a string**, the shape templates are
  written for: a flag is `"true"` or `""` so `{% if candidate_facing %}` reads
  either way, `None` is left out. Autoescaping is on; a template that must
  render markup from a variable says `| safe` at that one place.
- **A shell around every message.** The package ships a plain `base.html`
  (title band, content, a footer naming the recipient); a host's own
  `base.html` shadows it, and a message may name another shell.

## Surface

- `Mailer(transport, renderer=None, *, redirect_to=None)`: `send(...)`,
  `message(...)` (rendered, unsent), `deliver(message)`, `is_configured`.
- `Renderer(templates_dir=None, *, shell="base.html")`: `render(template,
  subject=, variables=, shell=)`, `render_body(template, variables)`.
- `Message(to=, subject=, html=, cc=, bcc=, reply_to=, attachments=)`,
  `Attachment(name, content, content_type)`.
- Transports: `GraphTransport(client, *, sender, save_to_sent_items=True)`
  (graph extra), `FileTransport(directory)`, `OffTransport(reason)`,
  `RecordingTransport()`; the `Transport` protocol for your own.
- The seam: `configure(factory)`, `mailer()`, `set_mailer(instance)`,
  `send(**kwargs)`, `is_configured()`.
- `template_strings`, `is_address`, `send_mail_payload`.

## Not in scope

No SMTP (a new transport is one class with one `deliver` method), no queue
or retry loop (`asas-notifications` owns the outbox and calls this once per
delivery), no template authoring UI, no inbound mail.

Extracted from a production platform whose backend took mail delivery over
from its AI tier (which had held the Graph credentials and the templates),
and learned the skip-versus-retry split from an outbox that filled against a
transport that could not exist.
