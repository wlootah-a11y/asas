"""THE file: the shipped library of checks — plain functions, nothing else.

Adding a check to your application means writing a function shaped like these
in your own checks file and handing that module to
:func:`asas_validation.include_checks` once at startup. There is no
registration call. A check:

- is named so it completes the sentence "this field must be ...";
- takes the value(s) under test first, settings as named arguments;
- takes ``field=`` so the error lands under the right form input, and an
  optional ``message_key=`` for hosts that render translations;
- returns ``None`` when valid, or a :class:`~asas_validation.engine.Violation`;
- passes when a value it tests is absent (partial edits supply only touched
  fields) — except the presence family, whose subject is absence itself;
- starts its docstring with the sentence shown to humans.

The ``validate`` namespace and ``catalog()`` (see ``library.py``) are built by
reading this module — the signature says what a check takes, the docstring's
first line is its sentence — so nothing is declared twice.
"""

import math
import re
import uuid as _uuid_mod
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

from .clock import today as _today
from .engine import Violation

__all__ = []  # the public surface is the validate namespace, not this module


def _v(field: str, code: str, message: str, message_key: Optional[str], **params) -> Violation:
    return Violation(
        field=field,
        code=code,
        message=message.format(**params) if params else message,
        params=params,
        message_key=message_key or f"validation.{code}",
    )


def _d(value):
    """Date-normalize: a datetime meeting a date must compare, never crash."""
    return value.date() if isinstance(value, datetime) else value


def _years_ago(anchor: date, years: int) -> date:
    try:
        return anchor.replace(year=anchor.year - years)
    except ValueError:  # Feb 29 → non-leap target year
        return anchor.replace(month=2, day=28, year=anchor.year - years)


def _present(value) -> bool:
    return value is not None and value != ""


# ── dates and time ────────────────────────────────────────────────────────────

def not_in_future(value, *, field="", message_key=None):
    """{field} must not be in the future"""
    if value is None: return None
    if _d(value) > _today():
        return _v(field, "not_in_future", "Must not be in the future", message_key)
    return None


def not_in_past(value, *, field="", message_key=None):
    """{field} must not be in the past"""
    if value is None: return None
    if _d(value) < _today():
        return _v(field, "not_in_past", "Must not be in the past", message_key)
    return None


def after(value, reference, *, days=0, field="", message_key=None):
    """{field} must be after {reference}, by at least {days} day(s)"""
    if value is None or reference is None: return None
    if _d(value) < _d(reference) + timedelta(days=days):
        return _v(field, "after", "Must be at least {days} day(s) after {reference}",
                  message_key, days=days, reference=str(_d(reference)))
    return None


def before(value, reference, *, days=0, field="", message_key=None):
    """{field} must be before {reference}, by at least {days} day(s)"""
    if value is None or reference is None: return None
    if _d(value) > _d(reference) - timedelta(days=days):
        return _v(field, "before", "Must be at least {days} day(s) before {reference}",
                  message_key, days=days, reference=str(_d(reference)))
    return None


def between(value, start, end, *, field="", message_key=None):
    """{field} must fall between {start} and {end}"""
    if value is None or start is None or end is None: return None
    value, start, end = _d(value), _d(start), _d(end)
    if not (start <= value <= end):
        return _v(field, "between", "Must fall between {start} and {end}",
                  message_key, start=str(start), end=str(end))
    return None


def _age_window(years, days):
    if (years is None) == (days is None):
        raise TypeError("pass exactly one of years= or days=")
    return _years_ago(_today(), years) if years is not None else _today() - timedelta(days=days)


def not_older_than(value, *, years=None, days=None, field="", message_key=None):
    """{field} must be no older than {years} year(s) / {days} day(s)"""
    if value is None: return None
    if _d(value) < _age_window(years, days):
        return _v(field, "not_older_than", "Must be no older than {limit}",
                  message_key, limit=f"{years} year(s)" if years is not None else f"{days} day(s)")
    return None


def not_beyond(value, *, years=None, days=None, field="", message_key=None):
    """{field} must be at most {years} year(s) / {days} day(s) ahead of today"""
    if value is None: return None
    if (years is None) == (days is None):
        raise TypeError("pass exactly one of years= or days=")
    limit = (_today().replace(year=_today().year + years) if years is not None
             else _today() + timedelta(days=days))
    if _d(value) > limit:
        return _v(field, "not_beyond", "Must be at most {limit} ahead of today",
                  message_key, limit=f"{years} year(s)" if years is not None else f"{days} day(s)")
    return None


def age_at_least(value, *, years=18, field="", message_key=None):
    """{field} must imply an age of at least {years} year(s)"""
    if value is None: return None
    if _d(value) > _years_ago(_today(), years):
        return _v(field, "age_at_least", "Must imply an age of at least {years}",
                  message_key, years=years)
    return None


def age_at_most(value, *, years, field="", message_key=None):
    """{field} must imply an age of at most {years} year(s)"""
    if value is None: return None
    if _d(value) < _years_ago(_today(), years + 1) + timedelta(days=1):
        return _v(field, "age_at_most", "Must imply an age of at most {years}",
                  message_key, years=years)
    return None


def on_weekday(value, *, allowed=(0, 1, 2, 3, 4), field="", message_key=None):
    """{field} must fall on an allowed weekday"""
    if value is None: return None
    if _d(value).weekday() not in allowed:
        return _v(field, "on_weekday", "Must fall on an allowed weekday",
                  message_key, allowed=list(allowed))
    return None


def within_period(start, end, period_start, period_end, *, field="", message_key=None):
    """{field} range must lie inside the parent range {period_start}..{period_end}"""
    if any(x is None for x in (start, end, period_start, period_end)): return None
    if not (_d(period_start) <= _d(start) and _d(end) <= _d(period_end)):
        return _v(field, "within_period", "Must lie inside {period_start} to {period_end}",
                  message_key, period_start=str(_d(period_start)), period_end=str(_d(period_end)))
    return None


def no_overlap(start, end, other_start, other_end, *, field="", message_key=None):
    """{field} range must not overlap {other_start}..{other_end}"""
    if any(x is None for x in (start, end, other_start, other_end)): return None
    if _d(start) <= _d(other_end) and _d(other_start) <= _d(end):
        return _v(field, "no_overlap", "Must not overlap {other_start} to {other_end}",
                  message_key, other_start=str(_d(other_start)), other_end=str(_d(other_end)))
    return None


# ── numbers ───────────────────────────────────────────────────────────────────

def at_least(value, minimum, *, field="", message_key=None):
    """{field} must be at least {minimum}"""
    if value is None or minimum is None: return None
    if value < minimum:
        return _v(field, "at_least", "Must be at least {minimum}", message_key, minimum=minimum)
    return None


def at_most(value, maximum, *, field="", message_key=None):
    """{field} must be at most {maximum}"""
    if value is None or maximum is None: return None
    if value > maximum:
        return _v(field, "at_most", "Must be at most {maximum}", message_key, maximum=maximum)
    return None


def positive(value, *, field="", message_key=None):
    """{field} must be greater than zero"""
    if value is None: return None
    if not value > 0:
        return _v(field, "positive", "Must be greater than zero", message_key)
    return None


def non_negative(value, *, field="", message_key=None):
    """{field} must be zero or greater"""
    if value is None: return None
    if value < 0:
        return _v(field, "non_negative", "Must be zero or greater", message_key)
    return None


def multiple_of(value, step, *, field="", message_key=None):
    """{field} must be a multiple of {step}"""
    if value is None or step is None: return None
    try:
        ok = Decimal(str(value)) % Decimal(str(step)) == 0
    except InvalidOperation:
        ok = False
    if not ok:
        return _v(field, "multiple_of", "Must be a multiple of {step}", message_key, step=step)
    return None


def max_decimals(value, *, places=2, field="", message_key=None):
    """{field} must have at most {places} decimal place(s)"""
    if value is None: return None
    exponent = Decimal(str(value)).normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -places:
        return _v(field, "max_decimals", "Must have at most {places} decimal place(s)",
                  message_key, places=places)
    return None


def percent(value, *, field="", message_key=None):
    """{field} must be between 0 and 100"""
    if value is None: return None
    if not (0 <= value <= 100):
        return _v(field, "percent", "Must be between 0 and 100", message_key)
    return None


def luhn(value, *, field="", message_key=None):
    """{field} must carry a valid checksum"""
    if value is None: return None
    digits = re.sub(r"[ \-]", "", str(value))
    if not digits.isdigit():
        return _v(field, "luhn", "Must contain digits only", message_key)
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9: d -= 9
        total += d
    if total % 10 != 0:
        return _v(field, "luhn", "Must carry a valid checksum", message_key)
    return None


# ── comparing fields ──────────────────────────────────────────────────────────

def greater_than(value, reference, *, field="", message_key=None):
    """{field} must be greater than the reference value"""
    if value is None or reference is None: return None
    if not value > reference:
        return _v(field, "greater_than", "Must be greater than {reference}",
                  message_key, reference=reference)
    return None


def less_than(value, reference, *, field="", message_key=None):
    """{field} must be less than the reference value"""
    if value is None or reference is None: return None
    if not value < reference:
        return _v(field, "less_than", "Must be less than {reference}",
                  message_key, reference=reference)
    return None


def equal_to(value, reference, *, field="", message_key=None):
    """{field} must match the reference value"""
    if value is None or reference is None: return None
    if value != reference:
        return _v(field, "equal_to", "Must match", message_key)
    return None


def different_from(value, reference, *, field="", message_key=None):
    """{field} must differ from the reference value"""
    if value is None or reference is None: return None
    if value == reference:
        return _v(field, "different_from", "Must be different", message_key)
    return None


def sums_to(*parts, total, field="", message_key=None):
    """{field} parts must add up to {total}"""
    if any(p is None for p in parts) or total is None or not parts: return None
    if not math.isclose(sum(parts), total, rel_tol=0, abs_tol=1e-9):
        return _v(field, "sums_to", "Must add up to {total}", message_key, total=total)
    return None


def ratio_at_least(value, reference, *, ratio=1.0, field="", message_key=None):
    """{field} must be at least the reference value times {ratio}"""
    if value is None or reference is None: return None
    if value < reference * ratio:
        return _v(field, "ratio_at_least", "Must be at least {ratio} times {reference}",
                  message_key, ratio=ratio, reference=reference)
    return None


# ── present or absent (the deliberate exception: absence is the subject) ─────

def required_when(value, when, *, field="", message_key=None):
    """{field} must be present when the condition holds"""
    if when and not _present(value):
        return _v(field, "required_when", "Required", message_key)
    return None


def forbidden_when(value, when, *, field="", message_key=None):
    """{field} must be absent when the condition holds"""
    if when and _present(value):
        return _v(field, "forbidden_when", "Must be left empty", message_key)
    return None


def required_together(*values, field="", message_key=None):
    """{field} fields must be present together or absent together"""
    present = [_present(v) for v in values]
    if any(present) and not all(present):
        return _v(field, "required_together", "These fields go together: fill all or none",
                  message_key)
    return None


def at_least_one(*values, field="", message_key=None):
    """{field} at least one of the fields must be present"""
    if not any(_present(v) for v in values):
        return _v(field, "at_least_one", "At least one of these is required", message_key)
    return None


def exactly_one(*values, field="", message_key=None):
    """{field} exactly one of the fields must be present"""
    if sum(1 for v in values if _present(v)) != 1:
        return _v(field, "exactly_one", "Provide exactly one of these", message_key)
    return None


# ── formats ───────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def email(value, *, domains=None, field="", message_key=None):
    """{field} must be a valid email address"""
    if value is None: return None
    if not _EMAIL_RE.match(str(value)):
        return _v(field, "email", "Must be a valid email address", message_key)
    if domains and str(value).rsplit("@", 1)[1].lower() not in {d.lower() for d in domains}:
        return _v(field, "email", "Email must be in an allowed domain",
                  message_key, domains=list(domains))
    return None


def url(value, *, schemes=("https",), field="", message_key=None):
    """{field} must be a valid web address"""
    if value is None: return None
    parsed = urlparse(str(value))
    if parsed.scheme not in schemes or not parsed.netloc:
        return _v(field, "url", "Must be a valid web address ({schemes})",
                  message_key, schemes=list(schemes))
    return None


def phone(value, *, region=None, field="", message_key=None):
    """{field} must be a valid phone number"""
    # Lenient E.164 shape; a host needing carrier-grade parsing wraps its own
    # check around a dedicated library in its checks file.
    if value is None: return None
    digits = re.sub(r"[ \-().]", "", str(value))
    if not re.fullmatch(r"\+?\d{7,15}", digits):
        return _v(field, "phone", "Must be a valid phone number", message_key)
    return None


def iban(value, *, field="", message_key=None):
    """{field} must be a valid IBAN"""
    if value is None: return None
    compact = re.sub(r"\s", "", str(value)).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return _v(field, "iban", "Must be a valid IBAN", message_key)
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    if int(numeric) % 97 != 1:
        return _v(field, "iban", "Must be a valid IBAN", message_key)
    return None


def uuid(value, *, field="", message_key=None):
    """{field} must be a well-formed UUID"""
    if value is None: return None
    try:
        _uuid_mod.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return _v(field, "uuid", "Must be a well-formed UUID", message_key)
    return None


def slug(value, *, field="", message_key=None):
    """{field} must contain only letters, digits, and dashes"""
    if value is None: return None
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", str(value)):
        return _v(field, "slug", "Must contain only letters, digits, and dashes", message_key)
    return None


def matches(value, pattern, *, field="", message_key=None):
    """{field} must match the required pattern"""
    if value is None or pattern is None: return None
    if not re.fullmatch(pattern, str(value)):
        return _v(field, "matches", "Must match the required format", message_key)
    return None


def no_html(value, *, field="", message_key=None):
    """{field} must contain no HTML"""
    if value is None: return None
    if re.search(r"<[^>]+>", str(value)):
        return _v(field, "no_html", "Must contain no HTML", message_key)
    return None


def one_of(value, allowed, *, field="", message_key=None):
    """{field} must be one of the allowed values"""
    if value is None or allowed is None: return None
    if value not in allowed:
        return _v(field, "one_of", "Must be one of the allowed values", message_key)
    return None


def not_in(value, forbidden, *, field="", message_key=None):
    """{field} must not be one of the forbidden values"""
    if value is None or forbidden is None: return None
    if value in forbidden:
        return _v(field, "not_in", "This value is not allowed", message_key)
    return None


# ── lists ─────────────────────────────────────────────────────────────────────

def unique_items(values, *, field="", message_key=None):
    """{field} items must contain no duplicates"""
    if values is None: return None
    seen = set()
    for item in values:
        if item in seen:
            return _v(field, "unique_items", "Must contain no duplicates", message_key)
        seen.add(item)
    return None


def subset_of(values, allowed, *, field="", message_key=None):
    """{field} every item must be in the allowed set"""
    if values is None or allowed is None: return None
    stray = [item for item in values if item not in allowed]
    if stray:
        return _v(field, "subset_of", "Contains values that are not allowed",
                  message_key, stray=stray)
    return None


def contains_none(values, terms, *, field="", message_key=None):
    """{field} must contain none of the listed terms"""
    if values is None or terms is None: return None
    haystack = values if isinstance(values, str) else " ".join(str(v) for v in values)
    found = [t for t in terms if t.lower() in haystack.lower()]
    if found:
        return _v(field, "contains_none", "Contains terms that are not allowed",
                  message_key, found=found)
    return None
