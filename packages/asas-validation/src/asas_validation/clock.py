"""The clock seam (DR 0004 R-9 analog): every temporal check asks this module
for "today" instead of calling ``date.today()`` directly, so a host can supply
a timezone-aware answer (the user's today, not the server's) and tests can
freeze time. Without configuration the server-local date is used — correct for
single-timezone deployments, documented as the fallback."""

from datetime import date, datetime
from typing import Callable, Optional

_clock: Optional[Callable[[], date]] = None


def configure_clock(fn: Optional[Callable[[], date]]) -> None:
    """Install (or, with ``None``, remove) the host's today-provider. The
    callable returns a ``date`` — typically the current date in the
    deployment's or the request's timezone."""
    global _clock
    _clock = fn


def today() -> date:
    value = _clock() if _clock else date.today()
    return value.date() if isinstance(value, datetime) else value
