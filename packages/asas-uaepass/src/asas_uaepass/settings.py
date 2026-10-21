"""What a host must tell the library about its UAE PASS registration.

The library reads no configuration of its own: the host builds a
:class:`UaePassSettings` from *its* settings object and hands it over. A
settings object may be built with values MISSING, on purpose: a deployment
that has not switched UAE PASS on still constructs the client at boot, and
every call then fails as :class:`UaePassUnconfiguredError` rather than at
import. ``configured`` says which it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STAGING_BASE_URL = "https://stg-id.uaepass.ae"
PRODUCTION_BASE_URL = "https://id.uaepass.ae"

#: The attributes a deployment keeps by default: everything the parser
#: knows. The Emirates ID number, gender and titles are never read whatever
#: this says (``profile.FACT_FOR_ATTRIBUTE`` is the whole list).
DEFAULT_KEPT_ATTRIBUTES = (
    "email,mobile,firstnameEN,lastnameEN,firstnameAR,lastnameAR,"
    "fullnameEN,fullnameAR,nationalityEN"
)


@dataclass(frozen=True)
class UaePassSettings:
    """``base_url`` is the UAE PASS host (staging or production); the library
    appends ``/idshub/authorize``, ``/token``, ``/userinfo`` and ``/logout``.
    ``callback_url`` is the host's own callback, exactly as registered with
    UAE PASS (production allows one). ``client_id`` is treated as a secret
    too: UAE PASS hands production credentials over in one protected file.
    ``kept_attributes`` names, in UAE PASS's own spelling, which attributes
    the host keeps; what UAE PASS SENDS is decided by the registration."""

    base_url: str = ""
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    callback_url: str = ""
    timeout_seconds: float = 10.0
    kept_attributes: str = DEFAULT_KEPT_ATTRIBUTES
    user_agent: str = "asas-uaepass"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("UaePassSettings.timeout_seconds must be > 0")
        object.__setattr__(self, "base_url", (self.base_url or "").strip().rstrip("/"))
        object.__setattr__(self, "callback_url", (self.callback_url or "").strip())

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.client_id and self.client_secret and self.callback_url)

    @property
    def missing(self) -> tuple[str, ...]:
        """The names of the values a sign-in needs and this object lacks."""
        return tuple(
            name
            for name in ("base_url", "client_id", "client_secret", "callback_url")
            if not getattr(self, name)
        )
