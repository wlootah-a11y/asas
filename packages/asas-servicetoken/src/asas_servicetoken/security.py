"""A FastAPI dependency that admits only a peer holding the shared secret.

Needs the ``fastapi`` extra. The package imports nothing from it until this
module is used.

The refusal the CALLER sees is one generic 401, whatever the cause: the body
of a refused request is read by whoever sent it, and telling a stranger
whether the secret was wrong or the clock was off helps only them. The
cause goes to this side's log, through ``Verifier.report``, which is where
the operator reading it will look.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from .tokens import Claims, TokenRefused, Verifier, bearer_token

_logger = logging.getLogger("asas_servicetoken")


def require_service_token(
    verifier: Verifier | Callable[[], Verifier],
    *,
    detail: str = "A service token is required.",
    logger: logging.Logger | None = None,
) -> Callable[[Request], Claims]:
    """A dependency returning the token's :class:`Claims`, or raising 401.

    ``verifier`` may be a zero-argument callable, read on every request, for
    a host whose settings are live. Use it on a router::

        router = APIRouter(dependencies=[Depends(require_service_token(verifier))])

    or on a route that wants the claims::

        @app.get("/internal")
        def internal(claims: Claims = Depends(require_service_token(verifier))): ...
    """
    log = logger or _logger

    def dependency(request: Request) -> Claims:
        active = verifier() if callable(verifier) else verifier
        token = bearer_token(request.headers.get("Authorization"))
        try:
            return active.verify(token)
        except TokenRefused as exc:
            log.warning(
                "A service token was refused (%s) on %s %s: %s",
                exc.reason,
                request.method,
                request.url.path,
                active.report(token),
            )
            headers: dict[str, Any] = {"WWW-Authenticate": "Bearer"}
            raise HTTPException(status_code=401, detail=detail, headers=headers) from exc

    return dependency
