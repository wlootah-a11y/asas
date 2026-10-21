"""asas-mail: render a message and send it, through one seam.

Table-less and router-less variant of the Asas host contract: no session, no
``migrate``, no ``seed``, no ``build_router``. The one host hook is
:func:`configure`, which installs the host's :class:`Mailer` factory (the
``asas-storage`` shape); a host may equally keep a ``Mailer`` on an object it
owns and never call it.

``import asas_mail`` loads Jinja2 and nothing else. The Graph transport
(``GraphTransport``, on an ``asas-graph`` client) is resolved on first use,
so a host that writes mail to a folder pays for no Graph dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import MailDeliveryError, MailError, MailNotConfigured
from .mailer import Mailer, configure, is_configured, mailer, send, set_mailer
from .message import Attachment, Message, is_address
from .rendering import DEFAULT_SHELL, Renderer, template_strings
from .transports import FileTransport, OffTransport, RecordingTransport, Transport

if TYPE_CHECKING:  # pragma: no cover
    from .graph import GraphTransport, send_mail_payload

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "DEFAULT_SHELL",
    "Attachment",
    "FileTransport",
    "GraphTransport",
    "MailDeliveryError",
    "MailError",
    "MailNotConfigured",
    "Mailer",
    "Message",
    "OffTransport",
    "RecordingTransport",
    "Renderer",
    "Transport",
    "configure",
    "is_address",
    "is_configured",
    "mailer",
    "send",
    "send_mail_payload",
    "set_mailer",
    "template_strings",
]

_GRAPH_NAMES = {"GraphTransport", "send_mail_payload"}


def __getattr__(name: str) -> Any:
    # Lazy so that `import asas_mail` never imports asas_graph (and so httpx, msal).
    if name in _GRAPH_NAMES:
        from . import graph

        return getattr(graph, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
