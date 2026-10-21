"""UAE PASS's failures, one class per thing a caller can act on.

``code`` is stable and a host maps it to its own error envelope;
``http_status`` is the answer a host would reasonably give its own caller;
``reason`` is the finer cause for the log and the alerts, and it never
names a person and never carries a code, a token or UAE PASS's own text.
UAE PASS's error descriptions are not echoed to anyone: its Text Message
Guidelines ask a service provider to show its set wording instead.
"""

from __future__ import annotations


class UaePassError(Exception):
    """Base of every error this package raises."""

    code = "uaepass_error"
    http_status = 502

    def __init__(self, message: str = "", *, reason: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.reason = reason or self.code


class UaePassUnconfiguredError(UaePassError):
    """UAE PASS is switched on but its base URL, client, secret or callback is
    missing. Nothing was called."""

    code = "uaepass_unconfigured"
    http_status = 503


class UaePassRejectedError(UaePassError):
    """UAE PASS refused us, or answered with something we cannot use: an
    unknown or expired code (``token_400``), our own credentials refused
    (``token_401``), a token it no longer accepts (``userinfo``), a body that
    is not what it should be (``token_shape``, ``userinfo_shape``), a profile
    with no ``uuid`` (``no_uuid``) or an unknown user type (``user_type``).
    Not retryable as is."""

    code = "uaepass_rejected"
    http_status = 401


class UaePassUnavailableError(UaePassError):
    """UAE PASS did not answer in time (``timeout``), could not be reached
    (``connect``) or answered 5xx (``token_5xx``, ``userinfo_5xx``).
    Retryable later."""

    code = "uaepass_unavailable"
    http_status = 502
