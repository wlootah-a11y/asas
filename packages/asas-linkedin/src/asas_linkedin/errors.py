"""LinkedIn's failures, one class per thing a caller can act on.

``code`` is stable and a host maps it to its own error envelope;
``http_status`` is the answer a host would reasonably give its own caller;
``details`` carries structured facts (LinkedIn's machine-readable ``reason``)
and never a member, a token or LinkedIn's own prose. LinkedIn's error text is
logged (it names the problem, never a person) but not echoed to anyone.
"""

from __future__ import annotations


class LinkedInError(Exception):
    """Base of every error this package raises."""

    code = "linkedin_error"
    http_status = 502

    def __init__(self, message: str = "", *, details: dict[str, str] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, str] = {k: str(v) for k, v in (details or {}).items()}


class LinkedInUnconfiguredError(LinkedInError):
    """A LinkedIn feature is switched on but its app or settings are missing.
    Nothing was called."""

    code = "linkedin_unconfigured"
    http_status = 503


class LinkedInRejectedError(LinkedInError):
    """LinkedIn refused us: an unknown or expired code, a bad redirect URI, a
    token that failed validation, a post it would not take. Not retryable as
    is. A 502 and not a 401: the caller's own session is fine, it is the
    upstream that said no (and a staff portal reads 401 as "sign in again")."""

    code = "linkedin_rejected"
    http_status = 502


class LinkedInUnavailableError(LinkedInError):
    """LinkedIn did not answer, or answered 5xx or 429. Retryable later."""

    code = "linkedin_unavailable"
    http_status = 502


class LinkedInSignatureInvalidError(LinkedInError):
    """An Apply with LinkedIn profile whose signature does not verify."""

    code = "linkedin_signature_invalid"
    http_status = 400


class LinkedInPageNotConnectedError(LinkedInError):
    """No usable Page token: never connected, expired, revoked, or sealed
    under a key this deployment no longer holds."""

    code = "linkedin_page_not_connected"
    http_status = 409
