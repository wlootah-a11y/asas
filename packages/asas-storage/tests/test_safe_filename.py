"""safe_filename's opt-in Unicode rule (ascii_only=False, 0.15.1): keep letters and digits in any
script, flatten what object stores reject or render deceptively, cap the
result in UTF-8 bytes. The backend rules behind each case are cited in
asas_storage/base.py."""

import unicodedata
import uuid

import pytest

from asas_storage import LocalStorage, safe_filename, valid_key
from asas_storage.base import MAX_FILENAME_BYTES


def unicode_filename(name, **kw):
    """``safe_filename`` with the opt-in Unicode charset."""
    return safe_filename(name, ascii_only=False, **kw)


ARABIC_CV = "السيرة الذاتية.pdf"


def test_arabic_is_kept():
    assert unicode_filename(ARABIC_CV) == ARABIC_CV
    assert (
        unicode_filename("عقد العمل ٢٠٢٦.docx") == "عقد العمل ٢٠٢٦.docx"
    )  # Arabic-Indic digits


def test_arabic_with_diacritics_keeps_the_marks():
    """Harakat are combining marks (Mn), not isalnum(); flattening them would
    turn a vocalised name into underscores between every letter."""
    assert unicode_filename("مُحَمَّد.pdf") == "مُحَمَّد.pdf"


def test_arabic_mixed_with_latin():
    assert unicode_filename("CV - أحمد Ahmed (2026).pdf") == "CV - أحمد Ahmed (2026).pdf"
    assert unicode_filename("données_2025.csv") == "données_2025.csv"


def test_nfc_and_nfd_give_one_key():
    nfd = unicodedata.normalize("NFD", "données é.csv")
    nfc = unicodedata.normalize("NFC", "données é.csv")
    assert nfd != nfc
    assert unicode_filename(nfd) == unicode_filename(nfc) == nfc
    # the same holds in ASCII mode: one visual name, one key
    assert safe_filename(nfd, ascii_only=True) == safe_filename(nfc, ascii_only=True)


def test_narrow_no_break_space_and_other_whitespace_flatten():
    """The TEAMY-248 case (U+202F), plus every non-ASCII-space whitespace."""
    assert (
        unicode_filename("Screenshot 2025-08-11 at 4.26.18\u202fPM.png")
        == "Screenshot 2025-08-11 at 4.26.18_PM.png"
    )
    for ws in (
        "\u00a0",
        "\u2009",
        "\u3000",
        "\t",
        "\n",
        "\r",
        "\u2028",
        "\u2029",
        "\u0085",
    ):
        assert unicode_filename(f"a{ws}b.txt") == "a_b.txt", repr(ws)


@pytest.mark.parametrize(
    "control",
    [
        "\u200f",
        "\u200e",
        "\u202e",
        "\u202d",
        "\u2066",
        "\u2069",
        "\u061c",
        "\u200b",
        "\u200d",
        "\ufeff",
    ],
)
def test_bidi_and_format_characters_flatten(control):
    out = unicode_filename(f"cv{control}عقد.pdf")
    assert out == "cv_عقد.pdf"
    assert not any(unicodedata.category(ch) == "Cf" for ch in out)


def test_bidi_override_attack_does_not_survive():
    """U+202E would make this display as 'invoiceexe.pdf'. With no override
    left, logical order is display order: the key visibly ends in .exe."""
    out = unicode_filename("invoice\u202efdp.exe")
    assert out == "invoice_fdp.exe"
    assert out.endswith(".exe")
    assert not out.endswith(".pdf")
    # the same attack wrapped in Arabic, where an RTL run is natural
    out = unicode_filename("فاتورة\u202efdp.exe")
    assert out == "فاتورة_fdp.exe"


def test_emoji_and_symbols_flatten():
    """Decision: emoji are symbols (So), not letters. They flatten, and so
    do the ZWJ (Cf) and the variation selector (a default-ignorable mark)
    that build emoji sequences; Supabase rejects them outright anyway."""
    assert unicode_filename("party🎉.png") == "party_.png"
    assert unicode_filename("family👨\u200d👩\u200d👧.jpg") == "family_____.jpg"
    assert unicode_filename("heart❤\ufe0f.txt") == "heart__.txt"
    assert unicode_filename("a#b%c<d>e|f.txt") == "a_b_c_d_e_f.txt"


def test_invisible_letters_and_store_rejected_code_points_flatten():
    # Hangul fillers are Lo (isalnum() is True) but render as nothing.
    assert unicode_filename("cv\u3164\u3164.exe") == "cv__.exe"
    # Azure's "not recommended" table: C1 controls, noncharacters, specials.
    for cp in (
        0x80,
        0x85,
        0x9F,
        0xFDD1,
        0xFDEF,
        0xFFF9,
        0xFFFD,
        0xFFFE,
        0xFFFF,
        0x1FFFE,
        0x10FFFF,
    ):
        assert unicode_filename(f"a{chr(cp)}b") == "a_b", hex(cp)
    assert unicode_filename("a\ue000b") == "a_b"  # private use: "cannot be used"
    assert unicode_filename("a\ud800b") == "a_b"  # a lone surrogate from a bad decode
    assert unicode_filename("a\x00b\x7fc") == "a_b_c"


def test_path_separators_flatten_to_one_segment():
    for name in ("../../etc/passwd", "a/b.pdf", "a\\b.pdf", "/abs.pdf"):
        out = unicode_filename(name)
        assert "/" not in out and "\\" not in out
        assert valid_key(f"orgs/1/documents/{uuid.uuid4()}-{out}")


def test_trailing_dots_and_spaces_trim():
    """Azure: no path segment should end with a dot."""
    assert unicode_filename("report. . ") == "report"
    assert unicode_filename("  cv.pdf") == "cv.pdf"


def test_never_empty_dot_or_dotdot():
    for name in ("", ".", "..", " .. ", "...", "   ", None):
        assert unicode_filename(name) == "file"
    assert unicode_filename("\u202f") == "_"
    assert unicode_filename("🎉") == "_"


def test_long_arabic_name_is_capped_in_bytes_and_keeps_its_extension():
    name = "ملف " * 200 + ".pdf"  # about 1,400 UTF-8 bytes
    out = unicode_filename(name)
    assert len(out.encode("utf-8")) <= MAX_FILENAME_BYTES
    assert out.endswith(".pdf")
    assert out.startswith("ملف ملف")
    assert not out[:-4].endswith((" ", "."))
    out.encode("utf-8").decode("utf-8")  # no code point was split


def test_cap_never_splits_a_code_point():
    # 4-byte letters (U+10400 DESERET) make an odd boundary likely
    for pad in range(4):
        out = unicode_filename("a" * pad + "\U00010400" * 100 + ".txt")
        assert len(out.encode("utf-8")) <= MAX_FILENAME_BYTES
        assert out.endswith(".txt")


def test_cap_with_no_usable_extension():
    out = unicode_filename("ب" * 500)
    assert out == "ب" * (MAX_FILENAME_BYTES // 2)
    # an "extension" too long to be one is truncated like the stem
    out = unicode_filename("x." + "y" * 500)
    assert len(out.encode("utf-8")) == MAX_FILENAME_BYTES
    assert unicode_filename("." + "z" * 500) == "." + "z" * (MAX_FILENAME_BYTES - 1)


def test_max_bytes_is_configurable_and_bounded():
    out = unicode_filename("ملف" * 100 + ".pdf", max_bytes=64)
    assert len(out.encode("utf-8")) <= 64 and out.endswith(".pdf")
    with pytest.raises(ValueError):
        unicode_filename("a.pdf", max_bytes=10)


def test_short_names_fit_under_the_cap_unchanged():
    assert unicode_filename("Deep Research (v2).docx") == "Deep Research (v2).docx"


def test_ascii_only_keeps_the_old_charset():
    """For Supabase Storage, whose key validator rejects any non-ASCII."""
    assert safe_filename(ARABIC_CV, ascii_only=True) == "______ _______.pdf"
    assert safe_filename("données_2025.csv", ascii_only=True) == "donn_es_2025.csv"
    assert (
        safe_filename("Screenshot 2025-08-11 at 4.26.18\u202fPM.png", ascii_only=True)
        == "Screenshot 2025-08-11 at 4.26.18_PM.png"
    )
    assert safe_filename("مُحَمَّد", ascii_only=True) == "________"
    out = safe_filename("é" * 300 + ".pdf", ascii_only=True)
    assert len(out) <= MAX_FILENAME_BYTES and out.endswith(".pdf")


def test_arabic_key_round_trips_on_local_storage(tmp_path):
    store = LocalStorage(tmp_path)
    key = f"orgs/1/documents/{uuid.uuid4()}-{unicode_filename(ARABIC_CV)}"
    store.put(key, b"%PDF-1.7", content_type="application/pdf")
    assert store.get(key) == b"%PDF-1.7"
    assert store.stat(key).content_type == "application/pdf"
    assert list(store.list("orgs/1/documents")) == [key]
    store.delete(key)
    assert not store.exists(key)


def test_capped_name_fits_the_local_file_name_limit(tmp_path):
    """NAME_MAX is 255 bytes; put() also writes a temp name 38 bytes longer
    than the segment, and the segment carries a 37-byte uuid prefix."""
    store = LocalStorage(tmp_path)
    key = f"orgs/1/documents/{uuid.uuid4()}-{unicode_filename('ملف ' * 200 + '.pdf')}"
    store.put(key, b"x")
    assert store.get(key) == b"x"
    assert list(store.list("orgs/1")) == [key]


def test_ascii_is_the_default_so_every_backend_accepts_the_key():
    """Supabase Storage 400s on any non-ASCII key, so the default must be the
    charset every backend accepts; readable Unicode keys are an opt-in."""
    assert safe_filename(ARABIC_CV) == "______ _______.pdf"
    assert unicode_filename(ARABIC_CV) == ARABIC_CV

