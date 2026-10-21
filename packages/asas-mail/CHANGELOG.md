# Changelog: `asas-mail`

Versions follow semver, and the git tag matches this file: `asas-mail/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production platform's own mailer, the one
every email in it (outbox notifications, candidate letters, welcome emails)
leaves through.

- **`Mailer`**: render a host template inside a shell and deliver it; a
  redirect that pins every message to one mailbox, drops the copies and
  keeps the body; an `off` transport refused BEFORE rendering.
- **`Message` and `Attachment`**: addressed at construction, duplicates and
  blanks dropped, a broken address or an empty `to` refused as
  `MailNotConfigured`.
- **Two errors with one meaning each:** `MailNotConfigured` is a skip the
  host never retries; `MailDeliveryError` is worth retrying and says
  `transient`, `status` and `retry_after`.
- **`GraphTransport`** (graph extra) on an `asas-graph` client: one
  `sendMail` as the sender mailbox, with copies, reply-to and base64 file
  attachments; Graph's error code carried, never the message or the body.
  **`FileTransport`** for a laptop (the HTML, a JSON sidecar and the files).
  **`OffTransport`** with a reason. **`RecordingTransport`** for a host's
  tests.
- **`Renderer`**: the host's templates first and the shipped `base.html`
  shell second, autoescaping on, every variable a string
  (`template_strings`), a shell nameable per message.
- **The process-wide seam**, the `asas-storage` shape: `configure(factory)`
  (lazy), `mailer()`, `set_mailer(...)`, `send(...)`, `is_configured()`.
- Jinja2 is the only dependency; `import asas_mail` loads no Graph client.
