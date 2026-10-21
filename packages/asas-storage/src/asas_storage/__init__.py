"""Asas storage — pluggable object/file storage seam.

Every stored byte goes through here: writers call ``storage().put(key, …)``, servers
stream via ``storage().fetch(key)``, deleters call ``delete``/``delete_prefix``. Keys
are ``/``-separated relative paths — exactly the strings the host stores in its DB —
so swapping backends never touches data. Extracted from Teamy (storage seam
TEAMY-248; extraction epic TEAMY-466, design record 0017).

Public surface — the Asas host contract (table-less **and** router-less variant: no
session dependency, no ``seed``/``migrate``, no ``build_routers`` — serving bytes
over HTTP is host auth territory):

- :func:`configure` — the host installs a zero-arg factory that builds the backend
  from *its own* settings; the library itself reads no configuration.
- :func:`storage` — the process-wide backend, lazily built via the factory.
- :func:`set_storage` — direct instance override (tests point this at a tmp-dir
  ``LocalStorage``); ``None`` resets to lazy re-selection via the factory.
- :class:`Storage` / :class:`FileStat` — the backend protocol.
- :class:`LocalStorage` / :class:`S3Storage` / :class:`AzureBlobStorage` — built-in
  backends (S3 needs the ``[s3]`` extra, Azure Blob the ``[azure]`` extra; both
  modules import their SDK lazily, so the classes stay importable without it).
  Azure Blob speaks no S3 API, hence a native backend rather than a gateway —
  and it is the one backend that can run with no stored credential at all
  (managed identity via ``account_url``).
- :func:`safe_filename` / :func:`valid_key` — key hygiene helpers.
"""

from __future__ import annotations

from typing import Callable, Optional

from .azure_blob import AzureBlobStorage
from .base import FileStat, RangeNotSatisfiable, Storage, safe_filename, valid_key
from .local import LocalStorage
from .s3 import S3Storage

__version__ = "0.15.1"

__all__ = [
    "AzureBlobStorage",
    "FileStat",
    "RangeNotSatisfiable",
    "LocalStorage",
    "S3Storage",
    "Storage",
    "configure",
    "safe_filename",
    "set_storage",
    "storage",
    "valid_key",
    "__version__",
]

_factory: Optional[Callable[[], Storage]] = None
_instance: Optional[Storage] = None


def configure(factory: Optional[Callable[[], Storage]]) -> None:
    """Install the host's backend factory, called lazily on first ``storage()``.

    Reconfiguring drops any lazily-built instance so the next ``storage()``
    call re-selects through the new factory."""
    global _factory, _instance
    _factory = factory
    _instance = None


def storage() -> Storage:
    """The process-wide storage backend (lazy; see ``set_storage`` for tests)."""
    global _instance
    if _instance is None:
        if _factory is None:
            raise RuntimeError(
                "asas_storage is not configured: call asas_storage.configure(factory) "
                "at startup, or set_storage(instance) directly"
            )
        _instance = _factory()
    return _instance


def set_storage(instance: Optional[Storage]) -> None:
    """Override the backend (tests point this at a tmp-dir LocalStorage);
    ``None`` resets to lazy re-selection via the configured factory."""
    global _instance
    _instance = instance
