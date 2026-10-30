"""Every value checked against the lines it cites.

Each value gets one word:

- `verified`: the value is in the cited lines (dates and numbers by what they say,
  not how they are written).
- `normalised`: an enum or a yes/no the model chose from a list; the CV's own
  words differ by design, so it is shown with its lines for a person to judge.
- `inferred`: not in the lines, and the profile model allows the field to be worked out.
- `unverified`: not in the lines, and the field should have been copied. Kept,
  never dropped, and listed first in the review.
- `bad_lines`: the entry cites no line the text has. A range past the end, or back to
  front, is dropped and the value is checked against the ranges that are real.
- `computed`: worked out in code (:mod:`asas_cv_profile.experience`, or the host).

Libraries, not hand rules: Arabic letter folding from CAMeL Tools (NYU Abu Dhabi, in
:mod:`.arabic`), fuzzy matching from RapidFuzz, dates from dateparser with CLDR's
locale data (every Arabic region's month names), and the Hijri calendar from hijridate
(Umm al-Qura), month names included.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from functools import lru_cache

from rapidfuzz import fuzz, process

from .arabic import ARABIC, ARABIC_RE, fold

MATCH = 90  # RapidFuzz partial ratio a copied value must reach in its lines
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


SHORT = 3  # a value this short after folding ("R", "AI", "SQL") is only found as a whole word
_RUN = re.compile(r"\d+")
_MARKED = re.compile(r"\w[+#]+")  # C++, C#, F#: the marks are part of the name


def norm(text: str) -> str:
    s = fold(unicodedata.normalize("NFKC", str(text)).casefold().translate(DIGITS))
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def found(value: str, haystack: str) -> bool:
    """The value is in the text: as written, or within RapidFuzz's partial or token-set
    ratio of :data:`MATCH`. Three things a ratio cannot see are checked first: its numbers
    must be there exactly ("Level 4" is not "Level 5"), a name whose marks are part of it
    must keep them ("C++" is not "C#"), and one short word, or one Latin word, is found as
    a word ("R" is not in "reports", "Java" is not "JavaScript"). A longer Arabic word keeps
    the plain rule: Arabic writes "and" and "the" onto the word ("والرياض")."""
    v, h = norm(value), norm(haystack)
    if not v:
        return not str(value).strip()  # nothing to find; a value of marks alone is not in any text
    if not set(_RUN.findall(v)) <= set(_RUN.findall(h)):
        return False
    raw = unicodedata.normalize("NFKC", str(haystack)).casefold()
    if any(m not in raw for m in _MARKED.findall(unicodedata.normalize("NFKC", str(value)).casefold())):
        return False
    if " " not in v and (len(v) <= SHORT or not ARABIC_RE.search(v)):
        return process.extractOne(v, h.split(), scorer=fuzz.ratio, score_cutoff=MATCH) is not None
    return v in h or fuzz.partial_ratio(v, h) >= MATCH or fuzz.token_set_ratio(v, h) >= MATCH


#: Dates are read by dateparser from CLDR's locale data, never from a list of ours. It
#: takes one region of a language per call, so each Arabic region is tried in turn: Arabic
#: as the Gulf and Egypt write it, the Levant and Iraq (أيلول، كانون الثاني), Algeria and
#: Tunisia (جانفي), Morocco (شتنبر). Its text search is limited (its docs say so) and takes
#: no regional locale, so the words before each year are parsed instead: on a set of Arabic
#: CVs the search over whole cited passages missed 48 of the 50 dates it flagged ("June 2025",
#: "7/6/2011", "يناير 2020", "2019م"), all of them in the lines.
LOCALES = {"latin": (["en"],), "arabic": (["ar"], ["ar-SY"], ["ar-DZ"], ["ar-MA"])}
_SETTINGS = {"PREFER_DAY_OF_MONTH": "first", "REQUIRE_PARTS": ["year", "month"]}
#: Egypt's spellings of June and July, which CLDR does not carry
_EGYPT = {"يونيه": "يونيو", "يوليه": "يوليو"}
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_TOKEN = re.compile(r"[^\W\d_]+\.?|\d+|[^\w\s]")  # words, numbers and marks, so "2010.Cyberjaya" gives up its year
_AFTER = re.compile(r"[\s,]+([^\W\d_]{3,}\.?)")
#: a month and a two-digit year with its apostrophe ("Jan '19"); dateparser picks the century
_SHORT = re.compile(r"[^\W\d_]{3,}\.?\s*['’]\s?\d{2}(?!\d)")
#: a month and two digits with no apostrophe ("Jan 19 - Mar 21"): a day or a year, and
#: the lines alone cannot say which, so both count, as 7/6/2011 does; never with a year
#: after it ("Jan 19, 2020" is a day)
_BARE = re.compile(r"(?<![\w'’])([^\W\d_]{3,}\.?)\s(\d{2})(?!\d)(?![\s,]*(?:19|20)\d{2}(?!\d))")
_ERA = re.compile(rf"(\d{{4}})\s*م(?![{ARABIC}])")  # 2019م: the Gregorian era mark
_DAYS = r"\d{1,2}(?:\s?[-–]\s?\d{1,2})?"  # a day, or a range of days
#: numbers only, the same in every language: 2019-05, 7/6/2011, 6/2011, 8-11/12 2011. A day and
#: a month that could each be the other (7/6/2011) count both ways: the lines alone cannot say
#: which the CV meant, and the order differs from country to country.
_YM = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})(?!\d)")
_DMY = re.compile(rf"(?<![\d/.\-])(?:{_DAYS}\s?[/.\-])?(\d{{1,2}})[/.\-]\s?((?:19|20)\d{{2}})(?!\d)")
_DM_Y = re.compile(rf"(?<![\d/.\-]){_DAYS}\s?[/.\-](\d{{1,2}})\s+((?:19|20)\d{{2}})(?!\d)")
#: a date in three numbers: a Hijri one when its year is 13xx or 14xx
_THREE = re.compile(r"(?<![\d/.\-])(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})(?![\d/.\-])")
_HIJRI_YEAR = re.compile(r"1[34]\d{2}")


_QUOTES = str.maketrans("", "", "'’ʻ`")


def _for_hijri(text: str) -> str:
    return fold(text).casefold().translate(_QUOTES)


@lru_cache(maxsize=1)
def _hijri_named() -> tuple[re.Pattern[str], dict[str, int]]:
    """A Hijri month by name and its year ("رمضان 1436", "1 Ramadan 1436"), a day before it
    or not: the names are hijridate's, Arabic and English, folded as the text is
    (:func:`_for_hijri`), so "Rabi al-Awwal" is "Rabi' al-Awwal"."""
    from hijridate import Hijri

    names = {_for_hijri(Hijri(1440, m, 1).month_name(lang)): m for lang in ("ar", "en") for m in range(1, 13)}
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:(\d{{1,2}})\s+)?({alts})\s*,?\s*(1[34]\d{{2}})(?!\d)"), names


@lru_cache(maxsize=16384)
def named_month(phrase: str) -> tuple[int, int] | None:
    """(year, month) of a short phrase holding a month's name, read by dateparser."""
    import dateparser

    script = "arabic" if ARABIC_RE.search(phrase) else "latin"
    for word, standard in _EGYPT.items():
        phrase = phrase.replace(word, standard)
    for locales in LOCALES[script]:
        when = dateparser.parse(phrase, locales=locales, settings=_SETTINGS)
        if when:
            return when.year, when.month
    return None


def _real(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
    except ValueError:
        return False
    return True


def _hijri_dates(text: str) -> set[tuple[int, int, int]]:
    """Every reading of each Hijri date, as (year, month, day): a month by name with its
    year ("1 رمضان 1436"; no day, its first and last), or in numbers with the year first
    (1440/3/15), or last with the day first (15/3/1440) or the month first (3-15-1440);
    both when both fit. hijridate refuses a day or month the calendar lacks."""
    from hijridate import Hijri

    out = set()
    named, months = _hijri_named()
    for day, name, year in named.findall(_for_hijri(text)):
        try:
            first = Hijri(int(year), months[name], int(day) if day else 1)
        except (ValueError, OverflowError):
            continue
        # no day: the month spans two Gregorian months, and both count
        out |= {(first.year, first.month, first.day)} if day else {(first.year, first.month, 1), (first.year, first.month, first.month_length())}
    for a, b, c in _THREE.findall(text):
        if _HIJRI_YEAR.fullmatch(c):
            readings = {(int(c), int(b), int(a)), (int(c), int(a), int(b))}
        elif _HIJRI_YEAR.fullmatch(a):
            readings = {(int(a), int(b), int(c))}
        else:
            continue
        for y, m, d in readings:
            try:
                Hijri(y, m, d)
            except (ValueError, OverflowError):
                continue
            out.add((y, m, d))
    return out


def _gregorian(y: int, m: int, d: int) -> tuple[int, int]:
    from hijridate import Hijri

    g = Hijri(y, m, d).to_gregorian()
    return g.year, g.month


def _hijri(text: str) -> set[tuple[int, int]]:
    """Hijri dates, as the Gregorian month each reading falls in."""
    return {_gregorian(*h) for h in _hijri_dates(text)}


@lru_cache(maxsize=4096)
def _months_in(text: str) -> frozenset[tuple[int, int | None]]:
    """Every (year, month) the text states; month None for a year alone."""
    text = _ERA.sub(r"\1", text.translate(DIGITS))
    out: set[tuple[int, int | None]] = {(int(y), None) for y in _YEAR.findall(text)}
    out |= {(int(y), int(m)) for y, m in _YM.findall(text) if 1 <= int(m) <= 12}
    out |= {(int(y), int(m)) for rx in (_DMY, _DM_Y) for m, y in rx.findall(text) if 1 <= int(m) <= 12}
    out |= {(int(c), int(a)) for a, b, c in _THREE.findall(text) if _YEAR.fullmatch(c) and _real(int(c), int(a), int(b))}  # month first
    out |= _hijri(text)
    out |= {got for phrase in _SHORT.findall(text) if (got := named_month(phrase))}
    out |= {got for word, yy in _BARE.findall(text) if (got := named_month(f"{word} '{yy}"))}
    rows = text.splitlines()
    for at, line in enumerate(rows):
        toks = list(_TOKEN.finditer(line))
        for i, tok in enumerate(toks):
            if not _YEAR.fullmatch(tok.group()):
                continue
            # the words before the year ("June 2025", "15th March, 2019", "كانون الثاني 2015")
            for k in range(1, 6):
                if i - k < 0:
                    break
                phrase = line[toks[i - k].start():tok.end()]
                if re.search(r"[^\W\d_]{3,}", phrase) and (got := named_month(phrase)):
                    out.add(got)
                    break
            # or the month after it, as reference lists write it ("2025 Dec;47(3)"), never across a dash
            after = _AFTER.match(line, tok.end())
            if after and (got := named_month(f"{tok.group()} {after.group(1)}")):
                out.add(got)
            elif i == len(toks) - 1 and at + 1 < len(rows) and (got := _month_wrapped(tok.group(), rows[at + 1])):
                out.add(got)
    return frozenset(out)


def _month_wrapped(year: str, below: str) -> tuple[int, int] | None:
    """A reference list's "2025 Sep;56(5)" with the line break after the year: the month
    opens the next line and has no year of its own, so "2019" over "May 2020 - Present"
    is two dates, not one."""
    toks = _TOKEN.findall(below)
    if not toks or not re.fullmatch(r"[^\W\d_]{3,}\.?", toks[0]) or (len(toks) > 1 and _YEAR.fullmatch(toks[1])):
        return None
    return named_month(f"{year} {toks[0]}")


def date_found(value: str, cited: str) -> bool:
    """YYYY, YYYY-MM or YYYY-MM-DD, found by year and month in however the CV writes it."""
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-\d{1,2})?$", str(value).strip())
    if not m:
        return False
    year, month = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    stated = _months_in(cited)
    return (year, month) in stated if month else any(y == year for y, _ in stated)


_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def _numbers_in(text: str) -> set[float]:
    """Every number the text states, "2.1 million" and "1,500,000" included."""
    out = set()
    for num, unit in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|mn|million|m|bn|billion|b)?\b", text.translate(DIGITS).lower()):
        n = float(num.replace(",", ""))
        out.add(n)
        if unit:
            out.add(round(n * _SCALE[unit], 2))
    return out


def year_only(value, cited: str) -> str | None:
    """The year alone, when the model wrote a month the lines do not give: the lines state
    the year with no month at all (on one set of CVs, 30 of the 35 dates flagged as not
    found were "2019" in the CV written "2019-03"). None when the value stands or the lines give
    another month, which is a real mismatch."""
    m = re.match(r"^(\d{4})-\d{1,2}(?:-\d{1,2})?$", str(value or "").strip())
    if not m:
        return None
    stated = _months_in(cited)
    year = int(m.group(1))
    return m.group(1) if (year, None) in stated and not any(y == year and mo for y, mo in stated) else None


def from_hijri(value, cited: str, *, month: bool) -> str | None:
    """The Gregorian date of the Hijri date the lines state, when the model copied its
    Hijri year as the value ("1436" for 12-28-1436). None unless the lines give exactly
    one such Gregorian date: a Hijri year alone spans two Gregorian years."""
    m = re.match(r"^(1[34]\d{2})(?:-(\d{1,2}))?(?:-\d{1,2})?$", str(value or "").strip())
    if not m:
        return None
    year, mo = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    found_ = {_gregorian(*h) for h in _hijri_dates(cited.translate(DIGITS)) if h[0] == year and mo in (None, h[1])}
    out = {f"{y:04d}-{gm:02d}" if month else f"{y:04d}" for y, gm in found_}
    return out.pop() if len(out) == 1 else None


def corrected_date(spec, value, cited: str) -> str | None:
    """An unverified date written as the lines give it: the year alone when they give no
    month (:func:`year_only`), or the Gregorian date of a Hijri date (:func:`from_hijri`)."""
    if spec.type not in ("date", "year"):
        return None
    return (year_only(value, cited) if spec.type == "date" else None) or from_hijri(value, cited, month=spec.type == "date")


def phone_found(value, cited: str, region: str | None = None) -> bool:
    """Each number the same number as one in the lines, compared by Google's libphonenumber
    (the phonenumbers port), so "+971501234567" is found where the CV writes "050 123 4567"
    in region AE. A number with no country code is read in ``region``; with none, it is
    found only by its digits."""
    import phonenumbers

    where = region or phonenumbers.UNKNOWN_REGION

    def national(text: str) -> str | None:
        try:
            return str(phonenumbers.parse(text, where).national_number)
        except phonenumbers.NumberParseException:
            return None

    in_lines = {str(m.number.national_number) for m in phonenumbers.PhoneNumberMatcher(cited.translate(DIGITS), where, leniency=phonenumbers.Leniency.POSSIBLE)}
    digits = re.sub(r"\D", "", cited.translate(DIGITS))
    for v in value if isinstance(value, list) else [value]:
        n = national(str(v))
        if not n or not (n in in_lines or (len(n) >= 7 and n in digits)):
            return False
    return True


def check_value(spec, value, cited: str, lines_ok: bool) -> str:
    if value in (None, "", [], "Not stated"):
        return "empty"  # nothing to check, whatever the lines: an empty field is not a bad one
    if not lines_ok:
        return "bad_lines"
    if spec.type == "phone":
        return "verified" if phone_found(value, cited, spec.region) else "unverified"
    if spec.kind == "text":
        return "verified"  # copied from the lines by code, so it is the CV's text by construction
    if spec.type in ("date", "year"):
        return "verified" if date_found(value, cited) else ("inferred" if spec.may_infer else "unverified")
    if spec.type == "number":
        return "verified" if round(float(value), 2) in _numbers_in(cited) else ("inferred" if spec.may_infer else "unverified")
    if spec.kind == "copied":
        if found(str(value), cited):
            return "verified"
        return "inferred" if spec.may_infer else "unverified"
    # enums, multi enums, booleans: chosen from a list
    values = value if isinstance(value, list) else [value]
    if all(isinstance(v, str) and found(v, cited) for v in values):
        return "verified"
    return "inferred" if spec.may_infer else "normalised"
