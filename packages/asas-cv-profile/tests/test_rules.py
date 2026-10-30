"""The rules code applies after the answers: twins, merged entries, skills, the computed
fields, other information, and the profile model file itself."""

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import Scripted, lines, run

from asas_cv_profile import ModelError, ProfileBuilder, parse_model
from asas_cv_profile.answers import spans
from asas_cv_profile.calls import spans_label
from asas_cv_profile.experience import add_experience, skill_key
from asas_cv_profile.headings import Headings
from asas_cv_profile.merged import flag_merged
from asas_cv_profile.model import BUDGET, one_group, pack
from asas_cv_profile.other import OtherLines, marks
from asas_cv_profile.skills import _skill_line_items, copy_and_restore
from asas_cv_profile.twins import merge_twins


def test_ranges_expand_and_a_bad_range_is_caught():
    assert spans([[3, 5], [9, 9], [4, 4]], 10) == ([3, 4, 5, 9], True)
    assert spans([[8, 12]], 10) == ([8, 9, 10], False)
    assert spans([[5, 3]], 10)[1] is False
    assert spans([[1, 2, 3]], 10) == ([], False)


def test_packed_calls_stay_under_the_property_budget(model):
    assert one_group(model.asked).properties <= BUDGET and len(pack(model.asked)) == 1
    packed = pack(model.asked, budget=20)
    assert len(packed) > 1 and all(g.properties <= 20 or len(g.subs) == 1 for g in packed)  # a section is never split
    assert [s.name for g in packed for s in g.subs] == [s.name for s in model.asked]


def test_a_bilingual_cv_keeps_each_role_once_in_english(model):
    def job(title, company, start, end, at):
        return {"lines": at, "fields": {"Job title": {"kind": "copied", "value": title}, "Company": {"kind": "copied", "value": company},
                                        "Start date": {"kind": "normalised", "value": start}, "End date": {"kind": "normalised", "value": end}}}

    p = {"Work experience": {"Professional": [
        job("مدير", "شركة المثال", "2015", "2023", [10, 11]), job("معلم", "مدرسة المثال", "1999", "2015", [12, 13]),
        job("Manager", "Example Co", "2015", "2023", [30, 31]), job("Teacher", "Example School", "1999", "2015", [32, 33]),
        job("Analyst", "Example Bank", "2012-09", "2019-09", []), job("Analyst", "Example Bank", "2012-09", "2019-09", [40]),
        job("Analyst", "Example Bank", None, None, [50])]}}
    assert merge_twins(p, model) == 3
    jobs = p["Work experience"]["Professional"]
    assert [j["fields"]["Job title"]["value"] for j in jobs] == ["Manager", "Teacher", "Analyst", "Analyst"]
    assert jobs[0]["lines"] == [10, 11, 30, 31] and jobs[0]["fields"]["Company"]["arabic"] == "شركة المثال"
    assert jobs[2]["lines"] == [40]  # the repeat with no lines folded into the one that has them


def test_an_item_in_both_languages_and_repeated_folds_in_one_pass(model):
    def job(title, company, at):
        return {"lines": at, "fields": {"Job title": {"kind": "copied", "value": title}, "Company": {"kind": "copied", "value": company},
                                        "Start date": {"kind": "normalised", "value": "2015"}, "End date": {"kind": "normalised", "value": "2023"}}}

    p = {"Work experience": {"Professional": [job("Manager", "Example Co", [3]), job("مدير", "شركة المثال", [10]),
                                              job("Manager", "Example Co", [20]), job("مدير", "شركة المثال", [25]),
                                              job("Teacher", "Example School", [30])]}}
    assert merge_twins(p, model) == 3
    jobs = p["Work experience"]["Professional"]
    assert [j["fields"]["Job title"]["value"] for j in jobs] == ["Manager", "Teacher"]  # same years, but no Arabic copy left for it
    assert jobs[0]["lines"] == [3, 10, 20, 25] and jobs[0]["fields"]["Company"]["arabic"] == "شركة المثال"
    assert merge_twins(p, model) == 0  # nothing left for a second pass


def test_a_pairing_chosen_by_cv_order_alone_is_flagged(model):
    def job(title, at, start="2015"):
        return {"lines": at, "fields": {"Job title": {"kind": "copied", "value": title},
                                        "Start date": {"kind": "normalised", "value": start}, "End date": {"kind": "normalised", "value": "2023"}}}

    p = {"Work experience": {"Professional": [job("مدير", [1]), job("مستشار", [2]), job("Manager", [10]), job("Teacher", [11], "1999")]}}
    assert merge_twins(p, model) == 1
    jobs = p["Work experience"]["Professional"]
    assert [j["fields"]["Job title"]["value"] for j in jobs] == ["Manager", "مستشار", "Teacher"]  # the pair takes the first one's place
    assert jobs[0]["flags"][0]["code"] == "twin" and "flags" not in jobs[1]  # two Arabic roles share its dates: CV order chose
    flag_merged(p, model, lines(*["Role, 2015 - 2023"] * 12))
    assert [f["code"] for f in jobs[0]["flags"]] == ["twin"]  # the merged check keeps other checks' flags
    clean = {"Work experience": {"Professional": [job("مدير", [1]), job("Manager", [10])]}}
    assert merge_twins(clean, model) == 1 and "flags" not in clean["Work experience"]["Professional"][0]


def test_skills_and_languages_are_never_twins(model):
    p = {"Technical skills": {"Languages": [{"lines": [1], "fields": {"Language": {"kind": "copied", "value": "Arabic"}}},
                                            {"lines": [2], "fields": {"Language": {"kind": "copied", "value": "Arabic"}}}]}}
    assert merge_twins(p, model) == 0


def test_an_entry_holding_several_date_ranges_is_flagged_as_merged(model):
    text = lines("Lecturer, Example University", "Sep 2019 - Present", "Taught statistics; in 2021 led the new course.",
                 "· Lab assistant, Example Lab", "2003 - 2005", "· Analyst, Example Co", "2010 – 2014", "· Intern", "2019-2024")

    def role(at):
        return {"lines": at, "fields": {"Job title": {"value": "x", "check": "verified"}}}

    p = {"Work experience": {"Professional": [role([1, 2, 3]), role([4, 5, 6, 7, 8, 9])]}, "Education": {"Course": [{"lines": [5], "fields": {}}]}}
    assert flag_merged(p, model, text) == 1
    one, many = p["Work experience"]["Professional"]
    assert "flags" not in one  # one range plus a single year in the text is one role
    assert many["flags"][0]["code"] == "merged" and "3 separate date ranges" in many["flags"][0]["message"]


def test_bilingual_dates_nested_tenure_and_shared_lines_are_not_merges(model):
    text = lines("مدير الميزانية (٢٠٢٤ حتى الآن)", "Budget manager (2024 - Present)", "Example Bank, Dubai 06/2003 - 03/2020",
                 "AVP, Budgeting | 01/2018 - 03/2020", "· CMA (2019)", "· Oracle Hyperion Planning (2017)", "· ISO/IEC 17025:2017 course", "2025")

    def e(at):
        return {"lines": at, "fields": {}}

    p = {"Work experience": {"Professional": [e([1, 2]), e([3, 4])]}, "Education": {"Course": [e([5, 6]), e([5, 6]), e([7, 8])]}}
    assert flag_merged(p, model, text) == 0


def test_skill_list_lines_split_as_written(model):
    assert _skill_line_items("Technical Skills: Python, SQL, and Power BI", set()) == ["Python", "SQL", "Power BI"]
    assert _skill_line_items("Built services with Python, Go and Rust for the bank", set()) == []
    p = {"Technical skills": {"Job skills": [{"lines": [2], "fields": {"Skill": {"value": "budgeting", "check": "verified"}}},
                                             {"lines": [2], "fields": {"Skill": {"value": "tendering", "check": "verified"}}}]}}
    out = copy_and_restore(p, ["Skills", "Budgeting, Tendering, Contract management, SLA governance"], model, Headings())
    assert out == {"copied": 0, "restored": 2, "listed": 4}
    assert [e["fields"]["Skill"]["value"] for e in p["Technical skills"]["Job skills"]][2:] == ["Contract management", "SLA governance"]


def test_every_item_of_the_skills_section_is_taken_and_marked(model):
    text = ["EXPERIENCE", "Analyst, built dashboards in Tableau, 2019-2023", "SKILLS", "- Power BI", "- Stakeholder management",
            "- SQL", "Led the reporting team with care and patience for years", "LANGUAGES", "Arabic"]
    p = {"Technical skills": {"Job skills": [{"lines": [n], "fields": {"Skill": {"value": v, "check": "verified"}}}
                                             for n, v in ((2, "Tableau"), (4, "Power BI"), (6, "SQL"))]}}
    out = copy_and_restore(p, text, model, Headings())
    got = {e["fields"]["Skill"]["value"]: e["source"] for e in p["Technical skills"]["Job skills"]}
    assert got == {"Tableau": "elsewhere", "Power BI": "skills list", "SQL": "skills list", "Stakeholder management": "skills list"}
    assert out["listed"] == 3
    again = copy_and_restore(p, text, model, Headings())  # run again, as recheck does: nothing added twice
    assert again["restored"] == 0 and len(p["Technical skills"]["Job skills"]) == 4


def test_a_skills_section_of_fragments_is_not_trusted(model):
    p = {"Technical skills": {"Job skills": [{"lines": [5], "fields": {"Skill": {"value": "Excel", "check": "verified"}}}]}}
    out = copy_and_restore(p, ["SKILLS", "ال", "مه", "ارات", "Excel"], model, Headings())
    assert out["listed"] == 0 and len(p["Technical skills"]["Job skills"]) == 1


def test_total_experience_counts_overlapping_roles_once(model):
    def role(a, b, skills=()):
        return {"lines": [1], "fields": {"Start date": {"value": a}, "End date": {"value": b}, "Skills used in this role": {"value": list(skills)}}}

    p = {"Work experience": {"Professional": [role("2020-01", "2022-12", ["SQL"]), role("2021-01", "2021-12", ["SQL"]), role("2023-06", "2023-12")]},
         "Technical skills": {"Job skills": [{"lines": [2], "fields": {"Skill": {"value": "sql"}}}]}}
    add_experience(p, model, date(2026, 10, 27))
    exp = p["Work experience"]["Experience"]["fields"]
    assert exp["Total experience (months)"]["value"] == 36 + 7 and exp["Gaps between roles (months)"]["value"] == 5
    assert p["Work experience"]["Professional"][0]["fields"]["Duration (months)"]["value"] == 36
    assert p["Work experience"]["Professional"][0]["fields"]["Date precision"]["value"] == "Month"
    skill = p["Technical skills"]["Job skills"][0]["fields"]
    assert skill["Months used"]["value"] == 36 and skill["Last used"]["value"] == "2022-12"  # the two roles overlap: a year of SQL in both is a year


def test_skill_names_keep_their_symbols():
    assert {skill_key(s) for s in ("C", "C++", "C#", ".NET", "c++ ")} == {"c", "c++", "c#", ".net"} and skill_key("Python.") == "python"


def test_lines_no_field_takes_are_kept_as_other_information(model):
    text = ["Sara Example", "sara@example.com | +971 50 123 4567", "EXPERIENCE", "Lecturer, Example University, 2019-2023",
            "COURSES TAUGHT", "Statistics for engineers, 2019 to 2023", "Research methods", "GRANTS AND FUNDING:",
            "Seed grant, 2021", "REFERENCES", "Dr Referee, Example University", "LANGUAGES", "Arabic", "Marital status: married",
            "x" * 120]

    def answer(call, s):
        if s.name == "Professional":
            return [{"job_title": "Lecturer", **{k: None for k in ("company", "start_date", "end_date", "i_currently_work_here")},
                     "responsibilities": [], "skills_used_in_this_role": [], "lines": [[4, 4]]}]
        if s.name == "Languages":
            return [{"language": "Arabic", "lines": [[13, 13]]}]
        if s.name == "Name":
            return {"full_name": "Sara Example", "lines": [[1, 1]]}
        return None

    builder = ProfileBuilder(model, Scripted(model, answer))
    built = run(builder.build(b"", "cv.txt", as_of=date(2026, 10, 28), text=lines(*text)))
    other = built["profile"]["Identity"]["Other information"]
    assert [e["fields"]["Topic"]["value"] for e in other] == ["COURSES TAUGHT", "LANGUAGES"]  # the second: the filler line
    assert other[0]["lines"] == [6, 7, 8, 9] and other[0]["fields"]["Details"]["check"] == "unplaced"
    assert other[1]["lines"] == [15] and built["other_information"] == {"lines": 5, "topics": 2}
    assert not {n for e in other for n in e["lines"]} & {2, 11, 14}  # contact details, a referee, a personal fact
    assert "Other information" not in [s.name for s in model.asked]  # never asked of the model
    again = builder.recheck(json.loads(json.dumps(built)))
    assert again["profile"]["Identity"]["Other information"] == other


def test_recheck_of_a_build_changes_nothing(model):
    text = ("Sara Example", "EXPERIENCE", "Analyst, Example Bank, 2019 - 2023", "SKILLS", "- Power BI", "- SQL", "- Stakeholder management")

    def answer(call, s):
        if s.name == "Professional":
            return [{"job_title": "Analyst", "company": "Example Bank", "start_date": "2019", "end_date": "2023",
                     "responsibilities": [], "skills_used_in_this_role": ["SQL", "Stakeholder management"], "lines": [[3, 3]]}]
        if s.name == "Job skills":
            return [{"skill": "Power BI", "lines": [[5, 5]]}, {"skill": "SQL", "lines": [[6, 6]]}]
        if s.name == "Name":
            return {"full_name": "Sara Example", "lines": [[1, 1]]}
        return None

    builder = ProfileBuilder(model, Scripted(model, answer))
    built = run(builder.build(b"", "cv.txt", as_of=date(2026, 10, 28), text=lines(*text)))
    added = next(e for e in built["profile"]["Technical skills"]["Job skills"] if e["fields"]["Skill"].get("restored"))
    role = built["profile"]["Work experience"]["Professional"][0]
    assert added["fields"]["Skill"]["value"] == "Stakeholder management"
    assert added["fields"]["Months used"]["value"] == role["fields"]["Duration (months)"]["value"] > 0
    once = builder.recheck(json.loads(json.dumps(built)))
    twice = builder.recheck(json.loads(json.dumps(once)))
    for k in ("profile", "checks", "fixes", "other_information"):
        assert once[k] == built[k] and twice[k] == built[k], k  # the skill code added again keeps its months


def test_a_markdown_heading_the_word_lists_do_not_know_starts_a_new_topic(model):
    text = lines("# Sara Example", "## Languages", "Arabic", "## Grants won", "Seed grant from the example fund", "Second grant from the fund")
    prof = {"Identity": {"Name": {"lines": [1], "fields": {}}}, "Technical skills": {"Languages": [{"lines": [3], "fields": {}}]}}
    other = OtherLines(model, Headings()).blocks(prof, text)
    assert [(e["fields"]["Topic"]["value"], e["lines"]) for e in other] == [("Grants won", [5, 6])]


def test_a_block_that_repeats_what_the_profile_holds_is_left_out_of_other_information(model):
    text = lines("# Sara Example", "## Summary", "Finance expert, 25 years, IPSAS and RPA projects", "## الملخص",
                 "خبيرة مالية، ٢٥ عاما، مشاريع IPSAS و RPA", "## Grants won", "Seed grant from the example fund",
                 "## المنح", "منحة من صندوق آخر 2019")
    prof = {"Identity": {"Name": {"lines": [1], "fields": {}}, "Summary": {"lines": [3], "fields": {}}}}
    other = OtherLines(model, Headings())
    other.put(prof, text)
    assert [e["lines"] for e in prof["Identity"]["Other information"]] == [[5], [7], [9]]

    def kinds(message):
        assert "L005 " in message and "Block 1: L005, under the heading" in message
        return [{"block": 1, "kind": "Summary", "repeats": [3]},  # the Arabic summary: a repeat
                {"block": 2, "kind": "Research and grants", "repeats": []},
                {"block": 3, "kind": "Research and grants", "repeats": [7]}]  # says 2019, line 7 does not

    assert spans_label([5, 6, 7, 9], 3) == "L005-L007, L009"
    assert marks("KPIs and RPA, 2019") == {"kpi", "and", "rpa", "2019"}
    _, dropped = run(other.name(prof, text, Scripted(model, kinds=kinds)))
    kept = prof["Identity"]["Other information"]
    assert [e["lines"] for e in kept] == [[7], [9]] and kept[0]["fields"]["Kind"]["value"] == "Research and grants"
    assert dropped == [{"lines": [5], "repeats": [3], "topic": "الملخص"}]
    assert 5 not in {n for e in other.blocks(prof, text, frozenset([5])) for n in e["lines"]}


def test_the_profile_model_is_checked_where_it_is_wrong():
    with pytest.raises(ModelError, match="unknown type 'money'"):
        parse_model({"sections": [{"name": "A", "fields": [{"name": "B", "type": "money"}]}]})
    with pytest.raises(ModelError, match="role 'start' is not one a plain section has"):
        parse_model({"sections": [{"name": "A", "fields": [{"name": "B", "type": "date", "role": "start"}]}]})
    with pytest.raises(ModelError, match="used twice"):
        parse_model({"sections": [{"name": "A", "fields": [{"name": "B"}]}, {"name": "A", "fields": [{"name": "C"}]}]})
    with pytest.raises(ModelError, match="belongs in a work section"):
        parse_model({"sections": [{"name": "A", "fields": [{"name": "B", "type": "number", "computed": "duration_months"}]}]})
    with pytest.raises(ModelError, match="headings need a kind"):
        parse_model({"sections": [{"name": "A", "headings": ["portfolio"], "fields": [{"name": "B"}]}]})
    with pytest.raises(ModelError, match="unknown region 'UAE'"):
        parse_model({"region": "UAE", "sections": [{"name": "A", "fields": [{"name": "B"}]}]})


def test_the_phone_region_is_the_models():
    m = parse_model({"region": "ae", "sections": [{"name": "A", "fields": [{"name": "Mobile", "type": "phone"}, {"name": "B"}]}]})
    assert m.region == "AE" and [f.region for f in m.sections[0].specs] == ["AE", None]
    assert parse_model({"sections": [{"name": "A", "fields": [{"name": "Mobile", "type": "phone"}]}]}).sections[0].specs[0].region is None


def test_the_model_reads_the_fields_in_the_hosts_words(model):
    prof = next(s for s in model.sections if s.name == "Professional")
    assert [f.name for f in prof.asked][:2] == ["Job title", "Company"] and prof.role("start").name == "Start date"
    assert prof.computed("duration_months").name == "Duration (months)" and prof.trait("dates") == "range"
    assert next(s for s in model.sections if s.name == "Languages").trait("twins") is False
    assert Path(model.source).name == "profile.yaml" and len(model.sha256) == 64


def test_a_group_counts_its_sections_once_each(model):
    from asas_cv_profile.model import (
        count_properties,
        group,
        group_model,
        strict_schema,
    )

    solo = [count_properties(strict_schema(group_model("One", [s]))) for s in model.asked]
    assert group("All", model.asked).properties == sum(solo)  # what pack() relies on


def test_a_total_needs_a_section_that_is_not_a_list():
    from asas_cv_profile import ModelError, parse_model

    doc = {"sections": [{"name": "Roles", "list": True, "kind": "work", "fields": [
        {"name": "Start", "type": "date", "role": "start"}, {"name": "Total", "type": "number", "computed": "total_months"}]}]}
    with pytest.raises(ModelError, match="not a list"):
        parse_model(doc)


def test_an_arabic_skill_list_splits_as_an_english_one():
    assert _skill_line_items("المهارات: إدارة المشاريع، التخطيط الاستراتيجي، تحليل البيانات", set()) == [
        "إدارة المشاريع", "التخطيط الاستراتيجي", "تحليل البيانات"]


def test_a_date_range_needs_a_word_between_and_a_month_by_its_name():
    from asas_cv_profile.merged import date_ranges

    assert date_ranges("2019 Tokyo 2020 conference") == set()
    assert date_ranges("June 2018 to March 2020") == {((2018, 6), (2020, 3))}
    assert date_ranges("Junior 2018 - 2020") == {((2018, 0), (2020, 0))}  # "Junior" is not June


def test_a_bilingual_skills_list_is_not_held_twice(model):
    def skill(n, v):
        return {"lines": [n], "fields": {"Skill": {"value": v, "check": "verified"}}}

    text = ["SKILLS", "Budgeting, Tendering, Contract management", "المهارات", "الميزانية، المناقصات، إدارة العقود"]
    p = {"Technical skills": {"Job skills": [skill(2, "Budgeting"), skill(2, "Tendering")]}}
    copy_and_restore(p, text, model, Headings())
    assert [e["fields"]["Skill"]["value"] for e in p["Technical skills"]["Job skills"]] == ["Budgeting", "Tendering", "Contract management"]
    arabic = {"Technical skills": {"Job skills": [skill(4, "الميزانية"), skill(4, "المناقصات")]}}
    copy_and_restore(arabic, ["SKILLS", "", "المهارات", "الميزانية، المناقصات، إدارة العقود"], model, Headings())
    assert [e["fields"]["Skill"]["value"] for e in arabic["Technical skills"]["Job skills"]][2:] == ["إدارة العقود"]  # an Arabic CV's own list


def test_a_fields_note_reaches_the_model_whatever_its_type():
    """The module's promise: a note is shown to the model beside the field, for a choice
    from listed values, a date, a number, a yes/no and copied text as much as for a string."""
    from asas_cv_profile.model import parse_model, entry_model

    note = "Never guess this from the employer's name."
    doc = {"sections": [{"name": "Things", "fields": [
        {"name": "Choice", "type": "enum", "values": ["A", "B"], "note": note},
        {"name": "When", "type": "date", "note": note},
        {"name": "How many", "type": "number", "note": note},
        {"name": "Yes or no", "type": "boolean", "note": note},
        {"name": "Words", "type": "text", "note": note},
        {"name": "Name", "type": "string", "note": note},
    ]}]}
    section = parse_model(doc).sections[0]
    props = entry_model(section).model_json_schema()["properties"]
    for spec in section.specs:
        assert note in props[spec.slug]["description"], spec.name


def test_a_required_choice_is_never_null_in_the_schema():
    """required: true makes an enum with values non-nullable, so strict structured outputs
    always give one; anything else marked required is refused when the model is read."""
    import pytest
    from asas_cv_profile.model import ModelError, entry_model, parse_model

    doc = {"sections": [{"name": "Skills", "list": True, "fields": [
        {"name": "Skill", "type": "string"},
        {"name": "Type", "type": "enum", "values": ["Job skill", "Digital and AI"], "required": True},
        {"name": "Level", "type": "enum", "values": ["1", "2"]},
    ]}]}
    section = parse_model(doc).sections[0]
    props = entry_model(section).model_json_schema()["properties"]
    assert props["type"].get("enum") == ["Job skill", "Digital and AI"] and "anyOf" not in props["type"]
    assert any(x.get("type") == "null" for x in props["level"]["anyOf"])
    with pytest.raises(ModelError, match="required needs an enum"):
        parse_model({"sections": [{"name": "X", "fields": [{"name": "A", "type": "string", "required": True}]}]})


def test_a_separator_inside_brackets_does_not_split_a_skill():
    from asas_cv_profile.skills import _skill_line_items

    items = _skill_line_items("Job skills: data strategy (expert, 5/5), data governance (4/5), budgeting (4/5)", set())
    assert items == ["data strategy (expert, 5/5)", "data governance (4/5)", "budgeting (4/5)"]


def test_a_restored_skill_takes_the_fields_its_lines_skills_agree_on():
    """One Skill list told apart by a Type field: an item of a CV's skill line the model left
    out takes the Type the line's other skills share, and "Python (5/5)" is not added beside
    the model's "Python"."""
    from asas_cv_profile.headings import Headings
    from asas_cv_profile.model import parse_model
    from asas_cv_profile.skills import copy_and_restore

    model = parse_model({"sections": [{"name": "Skill", "group": "Skills", "kind": "skills", "list": True, "fields": [
        {"name": "Skill", "type": "string", "role": "name"},
        {"name": "Type", "type": "enum", "values": ["Job skill", "Digital and AI"], "required": True},
    ]}]})
    lines = ["SKILLS", "Digital and AI: Python (5/5), SQL (5/5), Azure (5/5)"]

    def skill(name):
        return {"lines": [2], "fields": {"Skill": {"kind": "copied", "check": "verified", "value": name},
                                          "Type": {"kind": "normalised", "check": "verified", "value": "Digital and AI"}}}

    profile = {"Skills": {"Skill": [skill("Python"), skill("SQL")]}}
    copy_and_restore(profile, lines, model, Headings())
    got = {e["fields"]["Skill"]["value"]: e["fields"]["Type"]["value"] for e in profile["Skills"]["Skill"]}
    assert got == {"Python": "Digital and AI", "SQL": "Digital and AI", "Azure (5/5)": "Digital and AI"}
