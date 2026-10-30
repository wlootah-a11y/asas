"""Source grounding: each value's exact span on the lines it cites, and other information
keeping exactly the part of a line no value covers."""

from asas_cv_profile.headings import Headings
from asas_cv_profile.lines import Lines
from asas_cv_profile.model import parse_model
from asas_cv_profile.other import OtherLines
from asas_cv_profile.spans import covered, find, gaps, ground

TEXT = Lines([
    "Head of Data | Example Authority, Abu Dhabi | Apr 2021 - Present",
    "Interests: falconry and long-distance running. Holds a UAE driving licence. Open to relocating within the UAE.",
    "Built the reporting platform for twelve departments",
], "test")


def cell(kind, value, lines=None):
    c = {"kind": kind, "check": "verified", "value": value}
    if lines:
        c["lines"] = lines
    return c


def test_a_copied_value_is_found_exactly_and_a_text_field_covers_its_lines():
    profile = {"Work": {"Role": [{"lines": [1, 3], "fields": {
        "Job title": cell("copied", "Head of Data"),
        "Employer": cell("copied", "example authority"),  # case aside
        "Start date": cell("normalised", "2021-04"),
        "Responsibilities": cell("text", "Built the reporting platform for twelve departments", [3]),
    }}]}}
    assert ground(profile, TEXT) == 3
    f = profile["Work"]["Role"][0]["fields"]
    assert f["Job title"]["spans"] == [[1, 0, 12]]
    assert TEXT.line(1)[slice(*f["Employer"]["spans"][0][1:])] == "Example Authority"
    assert "spans" not in f["Start date"]  # the model's own words: no span
    assert f["Responsibilities"]["spans"] == [[3, 0, len(TEXT.line(3))]]


def test_a_value_not_found_exactly_is_aligned_by_rapidfuzz_or_not_at_all():
    assert find("Example Authorty", [1], TEXT) is not None  # one letter off: aligned
    assert find("Ministry of Something Else", [1], TEXT) is None  # not there: no span


def test_a_list_of_copied_items_gets_a_span_each():
    profile = {"Skills": {"Skill": [{"lines": [3], "fields": {"Used": cell("copied", ["reporting platform", "twelve departments"])}}]}}
    ground(profile, TEXT)
    assert len(profile["Skills"]["Skill"][0]["fields"]["Used"]["spans"]) == 2


def test_gaps_are_what_the_merged_ranges_leave():
    assert gaps(10, [(0, 2), (4, 6)]) == [(2, 4), (6, 10)]
    assert covered({"A": {"B": {"fields": {"x": {"spans": [[1, 0, 3], [1, 2, 5]]}}}}}) == {1: [(0, 5)]}


def test_other_information_keeps_exactly_what_no_value_covers():
    """The licence came from line 2 as a choice ("Light vehicle"), so the line is cited but
    no value covers its interests or "open to relocating": those sentences are kept, with
    their exact spans; the licence sentence shares a word with nothing it was read as and
    is kept too, the role line is fully covered and is not."""
    model = parse_model({"sections": [
        {"name": "Role", "group": "Work", "list": True, "fields": [
            {"name": "Job title", "type": "string"}, {"name": "Employer", "type": "string"}, {"name": "Start", "type": "date"}]},
        {"name": "Eligibility", "group": "Identity", "fields": [
            {"name": "Licence", "type": "enum", "values": ["Light vehicle", "Not stated"]}]},
        {"name": "Other information", "group": "Other", "kind": "other_information", "list": True, "fields": [
            {"name": "Topic", "type": "string", "role": "topic"}, {"name": "Details", "type": "text", "role": "details"}]},
    ]})
    profile = {
        "Work": {"Role": [{"lines": [1], "fields": {
            "Job title": cell("copied", "Head of Data"), "Employer": cell("copied", "Example Authority, Abu Dhabi"),
            "Start": cell("normalised", "2021-04")}}]},
        "Identity": {"Eligibility": {"lines": [2], "fields": {"Licence": cell("normalised", "Light vehicle")}}},
    }
    ground(profile, TEXT)
    other = OtherLines(model, Headings(), keep_all=True)
    left = other.left_over(profile, TEXT)
    assert 1 not in left  # "| Apr 2021 - Present" shares 2021 with the start date
    kept = [TEXT.line(2)[a:b] for a, b in left[2]]
    assert "Interests: falconry and long-distance running." in kept
    assert "Open to relocating within the UAE." in kept
    [block] = other.blocks(profile, TEXT)  # line 2's left-over parts, and line 3, which nothing cites, whole
    assert block["lines"] == [2, 3]
    assert block["fields"]["Details"]["spans"] == [[2, a, b] for a, b in left[2]] + [[3, 0, len(TEXT.line(3))]]
