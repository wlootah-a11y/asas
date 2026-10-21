"""asas-uaepass: UAE PASS, the UAE's national digital identity, as a sign-in.

The authorization-code exchange exactly as UAE PASS documents it, the user
information parsed and normalised into identity facts, and UAE PASS's own
nationality list. Nothing about any one product: what a host does with the
person (link an account, create one, ask) is the host's.

Table-less and router-less variant of the Asas host contract: no session, no
``migrate``, no ``seed``, no ``build_router``, no ``configure``. Everything
is an object the host constructs and owns (the ``asas-graph`` shape).
"""

from __future__ import annotations

from . import nationalities
from .client import (
    ACR_VALUES,
    AUTHORIZE_PATH,
    LOGOUT_PATH,
    SCOPE,
    TOKEN_PATH,
    USERINFO_PATH,
    UaePassClient,
    install_log_redaction,
)
from .errors import (
    UaePassError,
    UaePassRejectedError,
    UaePassUnavailableError,
    UaePassUnconfiguredError,
)
from .profile import (
    FACT_FOR_ATTRIBUTE,
    USER_TYPES,
    UaePassProfile,
    join_name_parts,
    kept_attributes,
    normalize_email,
    normalize_mobile,
    normalize_name,
    parse_userinfo,
)
from .settings import (
    DEFAULT_KEPT_ATTRIBUTES,
    PRODUCTION_BASE_URL,
    STAGING_BASE_URL,
    UaePassSettings,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ACR_VALUES",
    "AUTHORIZE_PATH",
    "DEFAULT_KEPT_ATTRIBUTES",
    "FACT_FOR_ATTRIBUTE",
    "LOGOUT_PATH",
    "PRODUCTION_BASE_URL",
    "SCOPE",
    "STAGING_BASE_URL",
    "TOKEN_PATH",
    "USERINFO_PATH",
    "USER_TYPES",
    "UaePassClient",
    "UaePassError",
    "UaePassProfile",
    "UaePassRejectedError",
    "UaePassSettings",
    "UaePassUnavailableError",
    "UaePassUnconfiguredError",
    "install_log_redaction",
    "join_name_parts",
    "kept_attributes",
    "nationalities",
    "normalize_email",
    "normalize_mobile",
    "normalize_name",
    "parse_userinfo",
]
