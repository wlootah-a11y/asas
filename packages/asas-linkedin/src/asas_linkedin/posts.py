"""The Posts API request body for a post as an organization Page. Pure.

The shape of LinkedIn's "Posts API" page (li-lms-2026-09, article post) and
its client library's ``examples/create_posts.py``. LinkedIn does not read the
link itself, so the article card is filled by the caller.
"""

from __future__ import annotations

from typing import Any

from .little_text import fit_commentary

ARTICLE_DESCRIPTION_MAX = 300


def article_post(
    *,
    organization_urn: str,
    title: str,
    summary: str,
    url: str,
    commentary: str | None = None,
) -> dict[str, Any]:
    """A public article post as the Page: ``commentary`` (the caller's own
    words, escaped and fitted; the title when none) above a card linking to
    ``url`` with ``title`` and the first 300 characters of ``summary``."""
    text = (commentary or "").strip() or title
    return {
        "author": organization_urn,
        "commentary": fit_commentary(text),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {
            "article": {
                "source": url,
                "title": title,
                "description": summary[:ARTICLE_DESCRIPTION_MAX],
            }
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
