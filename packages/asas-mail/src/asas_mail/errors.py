"""The two ways a send fails, and why the split matters to an outbox.

A host that queues mail retries a failed delivery up to a cap. That is right
for a mailbox that answered 503 and wrong for a deployment with no mailer at
all: an outbox row retrying against a transport that cannot exist only fills
the outbox, and reaches its cap with nothing to show. So "no mailer here" is
its own exception, raised before any wire is touched, and a host treats it as
a skip rather than a failure.
"""

from __future__ import annotations


class MailError(Exception):
    """Base of every error this package raises."""


class MailNotConfigured(MailError):
    """No mailer in this deployment: a deliberate "not sent", never retried.

    Raised by the ``off`` transport, by a transport selected with a value it
    needs missing, and by a message that cannot be addressed.
    """


class MailDeliveryError(MailError):
    """The mailer was there and the send failed: worth retrying.

    ``status`` is the HTTP status the mail service answered, when it answered;
    ``transient`` says whether a retry is likely to succeed (a throttle, a
    server fault, no answer at all) rather than certain to repeat (a refused
    permission, an unknown mailbox); ``retry_after`` is the service's own
    advice in seconds, when it gave one. The message itself is never carried:
    one of them may hold a password or an entry code.
    """

    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        transient: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.transient = transient
        self.retry_after = retry_after
