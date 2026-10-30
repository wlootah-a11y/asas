"""Values checked against their lines: dates, numbers, names, phones, as the CV writes them."""

from conftest import lines

from asas_cv_profile import checks
from asas_cv_profile.answers import cited_text
from asas_cv_profile.model import Spec


def spec(type, **kw) -> Spec:
    return Spec(section="S", name="F", type=type, **kw)


def test_dates_numbers_and_wrapped_lines_are_found_as_the_cv_writes_them():
    assert checks.check_value(spec("date"), "2023-12-31", "Lecturer (24/9/2019 > 31/12/2023).", True) == "verified"
    assert checks.check_value(spec("date"), "2021-03", "Mar 2021 - Present", True) == "verified"
    assert checks.check_value(spec("date"), "2021-04", "Mar 2021 - Present", True) == "unverified"
    assert checks.check_value(spec("number"), 2100000.0, "managed a budget of AED 2.1 million", True) == "verified"
    assert checks.check_value(spec("number"), 1500000.0, "budget AED 1,500,000", True) == "verified"
    text = lines("led strategic sourcing and procurement", "operations for 40 entities")
    assert checks.found("procurement operations", cited_text([1], text))


def test_a_value_the_profile_model_lets_be_worked_out_is_inferred_not_unverified():
    assert checks.check_value(spec("enum", values=["Senior"], infer=True), "Senior", "Analyst, Example Co", True) == "inferred"
    assert checks.check_value(spec("enum", values=["Senior"]), "Senior", "Analyst, Example Co", True) == "normalised"
    assert checks.check_value(spec("string"), "Example Co", "Analyst at Another Co", True) == "unverified"


def test_arabic_matching_ignores_hamza_and_diacritics():
    assert checks.found("أحمد الحمادي", "الاسم: احمد الحَمادي")
    assert checks.found("جامعة الإمارات", "خريج جامعة الامارات العربية المتحدة")
    assert not checks.found("جامعة زايد", "خريج جامعة الامارات")


def test_dates_as_arabic_and_english_cvs_write_them():
    assert checks.date_found("2025-06", "Analyst\nJune 2025 - present")
    assert checks.date_found("2011-06", "Lecturer 7/6/2011 - 31/5/2012")
    assert checks.date_found("2004-06", "مدير 9/3/2002 - 31/6/2004")  # a day the month does not have
    assert checks.date_found("2019-09", "سبتمبر 2019م")
    assert checks.date_found("2015-01", "كانون الثاني 2015") and checks.date_found("2018-09", "أيلول 2018")
    assert checks.date_found("2011-12", "Workshop, 8-11/12 2011") and checks.date_found("2023-03", "Seoul, Mar. 2023")
    assert checks.date_found("2021-08", "مؤتمر 25-26 /8/2021، دبي")
    assert not checks.date_found("2020-03", "يناير 2020 - مارس 2021")  # March is 2021's, not 2020's
    assert not checks.date_found("2019-12", "Room 12 2019")


def test_dates_in_every_arabic_region_and_the_hijri_calendar():
    assert checks.date_found("2018-09", "أيلول 2018") and checks.date_found("2017-03", "اذار 2017")  # Levant, no hamza
    assert checks.date_found("2016-07", "جويلية 2016") and checks.date_found("2016-09", "شتنبر 2016")  # Algeria, Morocco
    assert checks.date_found("2015-06", "يونيه 2015")  # Egypt, which CLDR lacks
    assert checks.date_found("2016-10", "اكتوبر 2016") and checks.date_found("2019-03", "15th March 2019")
    assert checks.date_found("2018-11", "تخرج 15/3/1440 هـ")  # a full Hijri date, as the Gregorian month it falls in
    assert not checks.date_found("2019-10", "رمضان 1440")  # Ramadan 1440 is May and June 2019
    assert not checks.date_found("2015-10", "Graduated 2015")  # a year alone never stands for a month


def test_a_hijri_month_by_name():
    # Ramadan 1436 runs from 18 June to 16 July 2015: with no day, both months count
    assert checks.date_found("2015-06", "رمضان 1436") and checks.date_found("2015-07", "رمضان 1436هـ")
    assert checks.date_found("2015-06", "1 رمضان 1436") and not checks.date_found("2015-07", "1 رمضان 1436")
    assert checks.date_found("2023-08", "15 محرم 1445") and checks.date_found("2015", "رمضان ١٤٣٦")
    assert checks.date_found("2014-12", "Rabi al-Awwal 1436") and checks.date_found("2015-06", "Ramadan, 1436")
    assert not checks.date_found("2015-06", "رجب محمد 1436")  # a name, not the month: no year follows it
    assert checks.from_hijri("1436", "رمضان 1436", month=False) == "2015"
    assert checks.from_hijri("1436", "رمضان 1436", month=True) is None  # June or July


def test_a_day_and_month_that_could_be_either_count_both_ways():
    assert checks.date_found("2011-06", "Joined 7/6/2011") and checks.date_found("2011-07", "Joined 7/6/2011")
    assert checks.date_found("2019-09", "24/9/2019") and not checks.date_found("2019-12", "24/9/2019")  # 24 is no month


def test_a_two_digit_year_with_its_apostrophe():
    assert checks.date_found("2019-01", "Analyst, Jan '19 - Mar '21") and checks.date_found("2021-03", "Jan '19 - Mar ’21")
    assert checks.date_found("2005-09", "Sept. '05")


def test_a_two_digit_year_with_no_apostrophe_counts_both_ways():
    assert checks.date_found("2019-01", "Analyst, Jan 19 - Mar 21") and checks.date_found("2021-03", "Jan 19 - Mar 21")
    assert not checks.date_found("2019-01", "Award, Jan 19, 2020")  # a year follows: 19 is the day
    assert not checks.date_found("2012-01", "Room 12")  # no month


def test_dates_in_reference_lists_and_glued_text():
    assert checks.date_found("2025-12", "J Example. 2025 Dec;47(3):377-400.")
    assert checks.date_found("2010-01", "Course, January 2010.Cyberjaya")
    assert not checks.date_found("2019-03", "2019 - مارس 2021")  # the month after a dash is the next date's


def test_a_reference_date_broken_after_its_year():
    assert checks.date_found("2025-09", "J Example. Smith A, Jones B. Title of the paper. 2025\nSep;56(5):101-9.")
    assert not checks.date_found("2019-05", "Analyst, Acme 2019\nMay 2020 - Present")  # two dates: May has its own year
    text = lines("Paper one.", "Paper two. 2019", "Paper three.", "Paper four.", "May;12(4):1-9.", "Paper five.")
    assert not checks.date_found("2019-05", cited_text([1, 6], text))  # lines 3 and 4 sit between: not one date


def test_an_empty_field_is_never_bad_lines_and_phones_match_by_number():
    phone = spec("phone", note="E.164", region="AE")
    assert checks.check_value(phone, None, "", False) == "empty"
    assert checks.check_value(phone, "+971501234567", "Mobile: 050 123 4567", True) == "verified"
    assert checks.check_value(phone, ["+971501234567", "+201001234567"], "050-1234567 / 0020 100 123 4567", True) == "verified"
    assert checks.check_value(phone, "+971501234999", "Mobile: 050 123 4567", True) == "unverified"
    assert checks.check_value(phone, "0501234567", "Mobile: +971 50 123 4567", True) == "verified"
    assert checks.check_value(spec("phone"), "0501234567", "Mobile: +971 50 123 4567", True) == "unverified"  # no region: no country to read it in


def test_a_month_the_cv_does_not_give_is_cut_to_the_year():
    assert checks.year_only("2019-03", "Presented at the annual meeting, 2019.") == "2019"
    assert checks.year_only("2019-03", "Presented in June 2019.") is None  # another month: a real mismatch
    assert checks.year_only("2019", "2019.") is None  # already the year


def test_hijri_dates_in_any_order_and_a_copied_hijri_year():
    assert checks.date_found("2015-10", "Member since 12-28-1436")  # month first: 28 is no month
    assert checks.date_found("2018-01", "6-5-1439") and checks.date_found("2018-02", "6-5-1439")  # both readings fit
    assert checks.date_found("2018-11", "1440/3/15")  # year first
    assert checks.from_hijri("1436", "Member since 12-28-1436", month=True) == "2015-10"
    assert checks.from_hijri("1436", "Member since 12-28-1436", month=False) == "2015"
    assert checks.from_hijri("1439", "Member 6-5-1439", month=True) is None  # two readings, two months: left as it is
    assert checks.from_hijri("1437", "Member 1437", month=True) is None  # a Hijri year alone spans two Gregorian ones
    assert checks.from_hijri("2015", "12-28-1436", month=True) is None
    assert checks.corrected_date(spec("date"), "1436", "عضو منذ ١٢-٢٨-١٤٣٦") == "2015-10"
    assert checks.corrected_date(spec("date"), "2019-03", "Presented, 2019.") == "2019"
    assert checks.corrected_date(spec("string"), "1436", "12-28-1436") is None


def test_what_a_ratio_cannot_see_is_checked_first():
    found = checks.found
    assert not found("C++", "Skills: C#, Java") and found("C++", "Languages: C++, Python")  # the marks are the name
    assert not found("R", "production reports") and found("R", "Python, R and SQL")  # a short value is a whole word
    assert not found("AI", "maintained the archive") and not found(".NET", "network admin")
    assert not found("Java", "JavaScript and TypeScript") and found("JavaScript", "JavaScript and TypeScript")
    assert not found("Level 4 diploma", "Level 5 diploma in care")  # numbers must be there exactly
    assert not found("***", "anything at all") and found("", "anything at all")
    assert found("Data Analyst", "Senior Data Analyst, Example Bank") and found("Senior Analyst Data", "Senior Data Analyst")
    assert found("محمد", "محـمد علي")  # the tatweel is a stretch, not a letter
    assert found("الرياض", "عمل في والرياض")  # a longer Arabic word keeps the plain rule: "and" is written onto it


def test_arabic_folding_is_camel_tools():
    """CAMeL Tools' four foldings, as character maps: every diacritic it names, every alef
    form, alef maksura and teh marbuta."""
    from asas_cv_profile.arabic import ARABIC_RE, fold

    assert fold("مُحَمَّد") == "محمد" and fold("أحمد إبراهيم آمنة") == "احمد ابراهيم امنه" and fold("مستشفى") == "مستشفي"
    assert fold("ٱلْقُرْآن هٰذا كِتَابٌ جَمِيلًا مُعَلِّمٍ") == "القران هذا كتاب جميلا معلم"
    assert ARABIC_RE.search("ﻣﺤﻤﺪ") and not ARABIC_RE.search("Mohammed")  # presentation forms count as Arabic
