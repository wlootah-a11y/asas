"""Plain text as LinkedIn's "little Text Format", which the Posts API's
``commentary`` is written in. Pure.

LinkedIn ("little Text Format", Microsoft Learn, li-lms-2026-03): the
reserved characters ``| { } @ [ ] ( ) < > # \\ * _ ~`` must each be escaped
with a backslash, "even if those characters are not used in one of the
supported elements or templates". Unescaped, they can turn a person's words
into a mention or a hashtag, or the post is refused.
"""

from __future__ import annotations

RESERVED = frozenset("|{}@[]()<>#\\*_~")

#: LinkedIn's limit on a post's commentary (Posts API).
COMMENTARY_MAX = 3000


def escape(text: str) -> str:
    return "".join(f"\\{ch}" if ch in RESERVED else ch for ch in text)


def fit_commentary(text: str, *, limit: int = COMMENTARY_MAX) -> str:
    """Escaped first, then cut to LinkedIn's limit, so the escapes count
    against it. A cut never leaves half an escape: in escaped text every
    backslash opens a pair, so an odd run of them at the end is one too many."""
    escaped = escape(text)
    if len(escaped) <= limit:
        return escaped
    cut = escaped[:limit]
    trailing = len(cut) - len(cut.rstrip("\\"))
    return cut[:-1] if trailing % 2 else cut
