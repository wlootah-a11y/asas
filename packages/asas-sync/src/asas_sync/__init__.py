"""Asas sync: mirror a remote paginated collection into your own tables, incrementally.

For any product that keeps a local copy of an ERP, HRIS or CRM collection (to
list, search and join it without asking the remote each time). The remote is
behind :class:`RemoteCollection`, the rows are yours (``upsert``,
``on_deleted``), and this package owns the cursor, the seen-marks and the rules
that make the copy trustworthy: walk orders that survive unstable tie order,
the offset ceiling, a watermark that never passes the walk's start, deletion
only after two misses (verified by key where the walk is not exact), resumable
capped passes, and one pass per collection at a time.

Extracted from a production mirror of an HR system's collections.

Host contract (table-owning variant, router-less, no seed):

- :func:`migrate` - the package-owned Alembic chain.
- :func:`run_pass` / :func:`reconcile` - async; take the host's session
  factory, so each page commits in its own short transaction.
- :func:`cursor_status` - the cursor row, for an admin card.
- :func:`refresh_keys` - read records named by a change notification by key.
- :func:`upsert_newer` - an upsert that never goes backwards, which is what
  makes :func:`refresh_keys` safe to race a pass.
- :func:`saved_copy_is_current` - whether the mirror vouches for a copy of a
  record saved beside it.
"""

from .engine import (
    PassResult,
    ReconcileResult,
    RefreshResult,
    RemoteCollection,
    RemotePage,
    SyncBusyError,
    SyncSpec,
    SyncStuckError,
    cursor_status,
    reconcile,
    refresh_keys,
    run_pass,
    saved_copy_is_current,
    upsert_newer,
)
from .migrate import migrate
from .models import SyncCursor, SyncSeen

__version__ = "0.1.0"

__all__ = [
    "PassResult",
    "ReconcileResult",
    "RefreshResult",
    "RemoteCollection",
    "RemotePage",
    "SyncBusyError",
    "SyncCursor",
    "SyncSeen",
    "SyncSpec",
    "SyncStuckError",
    "cursor_status",
    "migrate",
    "reconcile",
    "refresh_keys",
    "run_pass",
    "saved_copy_is_current",
    "upsert_newer",
    "__version__",
]
