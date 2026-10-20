"""Signed tokens between two services that share a secret.

One service signs each request it sends to a peer, the peer checks the
signature, and both sit on different hosts whose clocks never agree. Every
rule in this module is about that last fact:

* The issuer stamps ``iat`` BEHIND its own clock (``backdate_s``), so a
  verifier whose clock runs a little behind does not see a token "issued in
  the future" and refuse it as forged. ``exp`` stays measured from now, so the
  backdate costs no lifetime.
* The verifier allows ``leeway_s`` of drift on ``iat`` and ``exp``, seconds
  against a token that lives minutes, so ``exp`` still means something.
* A refusal is a :class:`TokenRefused` with a ``reason`` a host can log and
  branch on, never a sentence to match against.
* The secret is read on every call when it is given as a callable, so a
  rotated secret needs no restart and a test can patch it in place.

Nothing here is read from settings or the environment: the host passes what
it holds.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

Secret = str | Callable[[], str]
"""The shared secret, as a value or as a zero-argument callable read per call."""

Clock = Callable[[], float]

DEFAULT_TTL_S = 300
"""How long a minted token lives, measured from the issuer's clock."""

DEFAULT_BACKDATE_S = 30
"""How far behind its own clock an issuer stamps ``iat``."""

DEFAULT_LEEWAY_S = 60
"""How much drift a verifier allows on ``iat`` and ``exp``."""

DEFAULT_ALGORITHM = "HS256"

REFUSAL_REASONS = (
    "missing",
    "secret_unset",
    "immature",
    "expired",
    "signature",
    "issuer",
    "audience",
    "claims",
    "malformed",
)
"""Every ``TokenRefused.reason`` a verifier can answer."""

_logger = logging.getLogger("asas_servicetoken")


def resolve_secret(secret: Secret) -> str:
    """The secret's value now: a callable is read, an unset one is ``""``."""
    value = secret() if callable(secret) else secret
    return value or ""


def fingerprint(secret: str) -> str:
    """A comparable, non-reversible handle on a secret, for a log line.

    Twelve hex characters of its sha256: enough to hold one side's value
    against the other side's vault entry at a glance, and not invertible. An
    unset secret says so rather than fingerprinting the empty string, which
    would look like a value.
    """
    if not secret:
        return "<unset>"
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:12]


def bearer_token(header: str | None) -> str:
    """The token inside an ``Authorization: Bearer ...`` header, else ``""``."""
    if not header:
        return ""
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return rest.strip()


class ServiceTokenError(Exception):
    """Base of every error this package raises."""

    code = "service_token_error"


class SecretUnset(ServiceTokenError):
    """The shared secret resolved to nothing, so no token can be signed.

    ``hint`` is whatever the host passed as ``unset_hint`` (the name of the
    setting or the vault entry), so the message says where to look.
    """

    code = "secret_unset"

    def __init__(self, hint: str = "") -> None:
        where = f" ({hint})" if hint else ""
        super().__init__(f"The shared secret is unset{where}, so no service token can be signed.")
        self.hint = hint


class TokenRefused(ServiceTokenError):
    """A token the verifier will not accept, with a stable ``reason``.

    ``reason`` is one of :data:`REFUSAL_REASONS`; ``detail`` is the library's
    own message, which never contains the token.
    """

    code = "token_refused"

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        extra = f" ({detail})" if detail else ""
        super().__init__(f"The service token was refused: {reason}{extra}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Claims:
    """What a verified token says. ``raw`` is the whole payload."""

    subject: str | None
    issuer: str | None
    audience: str | list[str] | None
    issued_at: int | None
    expires_at: int | None
    raw: Mapping[str, Any]

    def get(self, name: str, default: Any = None) -> Any:
        """A claim by name, for the extras a host put on the token."""
        return self.raw.get(name, default)


class Issuer:
    """The side that signs: mints a token per call and the header carrying it.

    ``issuer`` names this service (``iss``, and ``sub`` unless ``subject`` is
    given); ``audience`` names the peer (``aud``) and may be left out for a
    peer that does not check one. ``unset_hint`` is the name of the setting
    the secret comes from, repeated in the one log line an unset secret gets.
    ``clock`` exists so a test can move this side's clock without touching
    the verifier's.
    """

    def __init__(
        self,
        secret: Secret,
        *,
        issuer: str,
        audience: str | None = None,
        subject: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        backdate_s: int = DEFAULT_BACKDATE_S,
        algorithm: str = DEFAULT_ALGORITHM,
        clock: Clock = time.time,
        unset_hint: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if backdate_s < 0:
            raise ValueError("backdate_s cannot be negative")
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.subject = subject if subject is not None else issuer
        self.ttl_s = ttl_s
        self.backdate_s = backdate_s
        self.algorithm = algorithm
        self.clock = clock
        self.unset_hint = unset_hint
        self._logger = logger or _logger
        self._warned_unset = False

    def mint(
        self,
        *,
        extra_backdate_s: int = 0,
        claims: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> str:
        """A signed token: ``iat`` backdated, ``exp`` a full ``ttl_s`` from now.

        ``extra_backdate_s`` stamps ``iat`` further back still, which is what a
        retry uses to clear a drift the first attempt could not (see
        ``send_with_service_token``); it widens no window, because ``exp`` is
        measured from now either way. ``claims`` ride along and cannot
        override the registered ones. Raises :class:`SecretUnset`.
        """
        secret = resolve_secret(self.secret)
        if not secret:
            raise SecretUnset(self.unset_hint)
        moment = int(self.clock() if now is None else now)
        payload: dict[str, Any] = dict(claims or {})
        payload.update(
            {
                "iss": self.issuer,
                "sub": self.subject,
                "iat": moment - self.backdate_s - extra_backdate_s,
                "exp": moment + self.ttl_s,
            }
        )
        if self.audience is not None:
            payload["aud"] = self.audience
        return jwt.encode(payload, secret, algorithm=self.algorithm)

    def headers(
        self,
        *,
        extra_backdate_s: int = 0,
        claims: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """The ``Authorization`` header for one call, minted now and never cached.

        With no secret it sends NO header at all and logs that ONCE, so the
        peer's refusal is what surfaces, with a log line naming the cause
        beside it. That is the shape an unresolved vault reference produces in
        a deployment, and it must read as a configuration fault, not as a
        peer that is down. A token baked into a long-lived client at
        construction would expire under it; mint per call instead.
        """
        try:
            token = self.mint(extra_backdate_s=extra_backdate_s, claims=claims)
        except SecretUnset as exc:
            if not self._warned_unset:
                self._warned_unset = True
                self._logger.error(
                    "%s Every call to %s goes out with no Authorization header"
                    " and will be refused.",
                    exc,
                    self.audience or "the peer",
                )
            return {}
        return {"Authorization": f"Bearer {token}"}

    def describe(self, *, now: float | None = None) -> str:
        """One line of the facts this side signed with, for a refusal log.

        The clock, the ``iat`` and ``exp`` it would stamp now, the configured
        backdate and lifetime, and a fingerprint of the secret: everything
        needed to tell a clock apart from a secret, and never the secret.
        """
        moment = int(self.clock() if now is None else now)
        return (
            f"issuer {self.issuer!r} clock reads {moment}, minting"
            f" iat={moment - self.backdate_s} (backdated {self.backdate_s}s)"
            f" exp={moment + self.ttl_s} (ttl {self.ttl_s}s) under secret"
            f" {fingerprint(resolve_secret(self.secret))}"
        )


class Verifier:
    """The side that checks: verifies a token and names why it will not.

    ``issuer`` and ``audience`` are what a token must claim; either may be
    left out to not check that claim. ``require`` lists the claims that must
    be present. ``leeway_s`` is the drift allowed on ``iat`` and ``exp``.
    """

    def __init__(
        self,
        secret: Secret,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        leeway_s: int = DEFAULT_LEEWAY_S,
        algorithm: str = DEFAULT_ALGORITHM,
        require: tuple[str, ...] = ("exp", "iat", "sub"),
        clock: Clock = time.time,
        unset_hint: str = "",
    ) -> None:
        if leeway_s < 0:
            raise ValueError("leeway_s cannot be negative")
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.leeway_s = leeway_s
        self.algorithm = algorithm
        self.require = tuple(require)
        self.clock = clock
        self.unset_hint = unset_hint

    def verify(self, token: str | None) -> Claims:
        """The claims of an acceptable token, or :class:`TokenRefused`.

        Fails closed: an unset secret refuses everything rather than
        accepting anything, and a token signed by nothing is ``missing``.
        """
        if not token:
            raise TokenRefused("missing")
        secret = resolve_secret(self.secret)
        if not secret:
            raise TokenRefused("secret_unset", self.unset_hint)
        options: dict[str, Any] = {
            "require": list(self.require),
            "verify_iss": self.issuer is not None,
            "verify_aud": self.audience is not None,
        }
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_s,
                options=options,
            )
        except jwt.ImmatureSignatureError as exc:
            raise TokenRefused("immature", str(exc)) from exc
        except jwt.ExpiredSignatureError as exc:
            raise TokenRefused("expired", str(exc)) from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenRefused("signature", str(exc)) from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenRefused("issuer", str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenRefused("audience", str(exc)) from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenRefused("claims", str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise TokenRefused("malformed", str(exc)) from exc
        return Claims(
            subject=payload.get("sub"),
            issuer=payload.get("iss"),
            audience=payload.get("aud"),
            issued_at=payload.get("iat"),
            expires_at=payload.get("exp"),
            raw=payload,
        )

    def report(self, token: str | None, *, now: float | None = None) -> str:
        """The facts behind a refusal, from this side, for the log.

        Reads the claims WITHOUT verifying them (a refused token is still a
        readable one) and states the issuer's ``iat`` as a signed offset from
        this clock: ``+600s`` is a clock to fix, ``+0s`` beside a signature
        refusal is a secret to compare. Plus what the token claims as its
        issuer and audience, and a fingerprint of the secret this side holds.
        Never the token.
        """
        moment = int(self.clock() if now is None else now)
        local = f"this side holds secret {fingerprint(resolve_secret(self.secret))}"
        if not token:
            return f"no token was presented; {local}"
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return f"the token could not be read as a JWT; {local}"
        iat = claims.get("iat")
        offset = f"{int(iat) - moment:+d}s from this clock" if isinstance(iat, int | float) else "absent"
        return (
            f"the token claims iss={claims.get('iss')!r} aud={claims.get('aud')!r}"
            f" sub={claims.get('sub')!r}, its iat is {offset}"
            f" (this clock reads {moment}, leeway {self.leeway_s}s); {local}"
        )
