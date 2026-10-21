"""Reference data that lives under Fusion's OTHER REST root.

An HCM instance serves ``hcmRestApi``, and a handful of the lists a host needs
(the common lookups, the language table) live under ``fscmRestApi`` on the same
host with the same credentials. The ``hcmRestApi`` path 404s for them, which
reads as "no such list" and is not: it is the wrong root.

Both reads here are nothing about any product: which lookup to ask for and what
to do with the answer stay with the host.
"""

from __future__ import annotations

import re
from typing import Any

from .query import eq, text

#: ``.../hcmRestApi/resources/<version>`` and nothing after it.
_HCM_ROOT = re.compile(r"/hcmRestApi/resources/([^/]+)/?$")

#: Fusion's language table (``languagesLOV``), with an ``InstalledFlag``.
LANGUAGES_RESOURCE = "languagesLOV"
#: ``InstalledFlag`` values that mean the language is on the instance: ``B`` the
#: base language, ``I`` an installed one. ``D`` is defined only (35 of 37 on one
#: measured instance).
INSTALLED_FLAGS = frozenset({"B", "I"})


def fscm_path(base_url: str, resource: str) -> str | None:
    """A path that reaches ``resource`` under ``fscmRestApi`` from a client whose
    base is the ``hcmRestApi`` root, or ``None`` when there is no such route.

    The client prepends its base to every path and ``httpx`` normalises the dot
    segments, so the hop is ``../../../fscmRestApi/resources/<version>/<resource>``.
    ``None`` when the base is not a direct ``hcmRestApi`` URL (a gateway proxies
    named endpoints and carries no such path): the caller then reads nothing
    rather than guessing a route."""
    match = _HCM_ROOT.search(base_url or "")
    if not match:
        return None
    return f"../../../fscmRestApi/resources/{match.group(1)}/{resource}"


async def common_lookup(client: Any, lookup_type: str, *, page_size: int = 500) -> list[tuple[str, str]]:
    """Every ``(LookupCode, Meaning)`` of one common lookup, paged.

    ``[]`` when the base URL gives no route to ``fscmRestApi`` (see
    :func:`fscm_path`). Raises what the client raises on an upstream failure:
    a host that syncs a list in the background decides what a failed read
    costs."""
    path = fscm_path(getattr(client, "base_url", ""), "commonLookupsLOV")
    if path is None:
        return []
    out: list[tuple[str, str]] = []
    offset = 0
    while True:
        rows, more, _total = await client.get_collection(
            path,
            {
                "q": eq("LookupType", lookup_type),
                "limit": page_size,
                "offset": offset,
                "onlyData": "true",
            },
            use_cache=False,
        )
        for row in rows:
            code, meaning = text(row, "LookupCode"), text(row, "Meaning")
            if code and meaning:
                out.append((code, meaning))
        offset += len(rows)
        if not more or not rows:
            return out


async def installed_languages(client: Any, *, page_size: int = 500) -> list[tuple[str, str]]:
    """The languages INSTALLED and active on the instance, as ``(LanguageCode,
    Description)``, off ``languagesLOV``.

    The lookup most people reach for (``PER_CORRESP_LANG``, 39 codes) lists
    every language Fusion can correspond in; a record published in one the
    instance does not carry cannot be used. This is the shorter, truer list: on
    one measured instance two of 37. ``[]`` when there is no ``fscmRestApi``
    route."""
    path = fscm_path(getattr(client, "base_url", ""), LANGUAGES_RESOURCE)
    if path is None:
        return []
    out: list[tuple[str, str]] = []
    offset = 0
    while True:
        rows, more, _total = await client.get_collection(
            path, {"limit": page_size, "offset": offset, "onlyData": "true"}, use_cache=False
        )
        for row in rows:
            code, name = text(row, "LanguageCode"), text(row, "Description")
            installed = text(row, "InstalledFlag") in INSTALLED_FLAGS
            active = text(row, "ActivationStatus").upper() == "ACTIVE"
            if code and name and installed and active:
                out.append((code, name))
        offset += len(rows)
        if not more or not rows:
            return out
