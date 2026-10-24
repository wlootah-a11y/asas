"""The mask redacts by key prefix, anywhere in a payload, and never raises."""

from asas_llm.masking import REDACTED, mask_prefixed

mask = mask_prefixed(("screening_",))


def test_a_prefixed_key_is_redacted():
    masked = mask(data={"screening_transcript": "candidate: my salary is 40000"})
    assert masked["screening_transcript"] == REDACTED
    assert "40000" not in str(masked)


def test_an_unprefixed_key_is_left_alone():
    # Two other agents send a bare `transcript`; the prefix keeps the blast
    # radius inside the one that asked for masking.
    assert mask(data={"transcript": "interviewer notes here"})["transcript"] == "interviewer notes here"


def test_the_audit_fields_survive():
    data = {"model": "gpt-5.1", "prompt_version": 3, "question_id": "B01"}
    assert mask(data=data) == data


def test_nested_and_listed_keys_are_redacted():
    assert mask(data={"payload": {"screening_segment": "x"}})["payload"]["screening_segment"] == REDACTED
    assert mask(data=[{"screening_answer_text": "x"}])[0]["screening_answer_text"] == REDACTED


def test_a_plain_string_is_returned_unchanged():
    assert mask(data="ok") == "ok"


def test_it_never_raises_on_an_odd_shape():
    class Odd:
        pass

    odd = Odd()
    assert mask(data=odd) is odd


def test_several_prefixes_and_a_custom_marker():
    m = mask_prefixed(["a_", "b_"], redacted="[gone]")
    assert m(data={"a_x": 1, "b_y": 2, "c_z": 3}) == {"a_x": "[gone]", "b_y": "[gone]", "c_z": 3}
    assert m.prefixes == ("a_", "b_")
