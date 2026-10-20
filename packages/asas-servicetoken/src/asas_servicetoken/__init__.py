"""asas-servicetoken: signed tokens between two services that share a secret.

Table-less and router-less variant of the Asas host contract: no session, no
``migrate``, no ``seed``, no ``configure``. A host builds an :class:`Issuer`
for each peer it calls and a :class:`Verifier` for each peer that calls it,
from the secret it holds, and that is the whole adoption.

``import asas_servicetoken`` loads only PyJWT. The httpx helpers
(``send_with_service_token``, ``stream_with_service_token``, ``log_refusal``)
and the FastAPI dependency (``require_service_token``) are resolved on first
use, so a host that wants neither extra pays for neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tokens import (
    DEFAULT_ALGORITHM,
    DEFAULT_BACKDATE_S,
    DEFAULT_LEEWAY_S,
    DEFAULT_TTL_S,
    REFUSAL_REASONS,
    Claims,
    Issuer,
    Secret,
    SecretUnset,
    ServiceTokenError,
    TokenRefused,
    Verifier,
    bearer_token,
    fingerprint,
    resolve_secret,
)

if TYPE_CHECKING:  # pragma: no cover
    from .client import (
        UNAUTHORIZED,
        log_refusal,
        send_with_service_token,
        stream_with_service_token,
    )
    from .security import require_service_token

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "DEFAULT_ALGORITHM",
    "DEFAULT_BACKDATE_S",
    "DEFAULT_LEEWAY_S",
    "DEFAULT_TTL_S",
    "REFUSAL_REASONS",
    "UNAUTHORIZED",
    "Claims",
    "Issuer",
    "Secret",
    "SecretUnset",
    "ServiceTokenError",
    "TokenRefused",
    "Verifier",
    "bearer_token",
    "fingerprint",
    "log_refusal",
    "require_service_token",
    "resolve_secret",
    "send_with_service_token",
    "stream_with_service_token",
]

_HTTPX_NAMES = {"UNAUTHORIZED", "log_refusal", "send_with_service_token", "stream_with_service_token"}
_FASTAPI_NAMES = {"require_service_token"}


def __getattr__(name: str) -> Any:
    # Lazy so that `import asas_servicetoken` never imports httpx or fastapi.
    if name in _HTTPX_NAMES:
        from . import client

        return getattr(client, name)
    if name in _FASTAPI_NAMES:
        from . import security

        return getattr(security, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
