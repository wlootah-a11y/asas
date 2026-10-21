"""The CV headshot extractor, against the encodings CVs are actually written in.

A NOTE ON THE FIXTURES, because a previous extractor passed 22 tests and then
found nothing in any real CV. Those fixtures were written from the same
mistaken model of PDF as the parser they were checking. Two things keep that
from happening here: the READER is pdfminer, which nobody here wrote, so a
fixture assembled wrongly is rejected rather than quietly understood; and the
hard fixture is built by PILLOW rather than by hand (a PNG's IDAT is zlib over
PNG-predicted scanlines, byte for byte what a PDF image stream with
``/FlateDecode`` and ``/Predictor 15`` holds).

Run against real CVs with ``CV_PHOTO_CORPUS=/path/to/dir pytest``. Those files
are people's faces and are deliberately not committed.
"""

from __future__ import annotations

import base64
import io
import os
import pathlib
import struct
import zipfile
import zlib

import pytest
from PIL import Image

from asas_cv import MAX_PHOTO_BYTES, ExtractedPhoto, extract_profile_photo, looks_like_a_headshot


def face(width: int = 300, height: int = 300, seed: int = 7) -> Image.Image:
    """A deterministic, PHOTOGRAPH-LIKE image: a soft blob on a gradient
    survives JPEG quality 90 within a few levels per channel, while a real
    decode fault moves whole rows."""
    image = Image.new("RGB", (width, height))
    cx, cy = width / 2, height / 2
    radius = min(width, height) / 2 or 1
    pixels = []
    for y in range(height):
        for x in range(width):
            d = (((x - cx) ** 2 + (y - cy) ** 2) ** 0.5) / radius
            blob = max(0.0, 1.0 - d)
            pixels.append((
                int(40 + 180 * blob + 30 * x / width),
                int(30 + 150 * blob + 40 * y / height),
                int((20 + 120 * blob + seed * 4) % 256),
            ))
    image.putdata(pixels)
    return image


JPEG_TOLERANCE = 24


def assert_close(got: Image.Image, want: Image.Image) -> None:
    a, b = got.convert("RGB").tobytes(), want.convert("RGB").tobytes()
    assert len(a) == len(b), f"size changed: {len(a)} against {len(b)}"
    worst = max(abs(x - y) for x, y in zip(a, b))
    assert worst <= JPEG_TOLERANCE, f"worst channel differs by {worst}"


def _pdf(objects: list[bytes], root: int = 1) -> bytes:
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, root, xref_at)
    return bytes(out)


def stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< %s /Length %d >>\nstream\n" % (dictionary, len(data)) + data + b"\nendstream"


def pages_pdf(pages: list[list[bytes]]) -> bytes:
    catalogue_and_tree = 2
    page_numbers: list[int] = []
    tail: list[bytes] = []
    next_object = catalogue_and_tree + 1
    for images in pages:
        page_number = next_object
        image_numbers = list(range(page_number + 1, page_number + 1 + len(images)))
        resources = b" ".join(b"/Im%d %d 0 R" % (index, number) for index, number in enumerate(image_numbers))
        tail.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << %s >> >> >>" % resources
        )
        tail.extend(images)
        page_numbers.append(page_number)
        next_object = page_number + 1 + len(images)
    kids = b" ".join(b"%d 0 R" % n for n in page_numbers)
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_numbers)),
        *tail,
    ])


def flate_rgb_image(image: Image.Image) -> bytes:
    return stream(
        b"/Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode" % image.size,
        zlib.compress(image.tobytes()),
    )


def a85_flate_rgb_image(image: Image.Image) -> bytes:
    """ReportLab's shape: a CHAIN of two filters, applied in order."""
    body = base64.a85encode(zlib.compress(image.tobytes())) + b"~>"
    return stream(
        b"/Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
        b"/Filter [/ASCII85Decode /FlateDecode]" % image.size,
        body,
    )


def predicted_rgb_image(image: Image.Image) -> bytes:
    """Word's shape: Flate plus a PNG predictor, built BY PILLOW."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    png = buffer.getvalue()
    idat = b""
    at = 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        kind = png[at + 4 : at + 8]
        if kind == b"IDAT":
            idat += png[at + 8 : at + 8 + length]
        at += 12 + length
    assert idat, "Pillow wrote no IDAT, the fixture is broken"
    width, height = image.size
    return stream(
        b"/Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        b"/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 "
        b"/Columns %d >>" % (width, height, width),
        idat,
    )


def jpeg_image(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return stream(
        b"/Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode" % image.size,
        buffer.getvalue(),
    )


def docx(images: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='http://x'></Types>")
        archive.writestr("word/document.xml", "<document/>")
        for name, data in images.items():
            archive.writestr(f"word/media/{name}", data)
    return buffer.getvalue()


def as_image(photo: ExtractedPhoto) -> Image.Image:
    return Image.open(io.BytesIO(photo.content))


class TestTheEncodingsThatMatter:
    def test_plain_flate_samples(self) -> None:
        photo = extract_profile_photo(pages_pdf([[flate_rgb_image(face())]]), "cv.pdf")
        assert photo is not None
        assert (photo.width, photo.height) == (300, 300)
        assert photo.mime_type == "image/jpeg"
        assert as_image(photo).size == (300, 300)

    def test_a_filter_chain_is_applied_in_order(self) -> None:
        original = face()
        photo = extract_profile_photo(pages_pdf([[a85_flate_rgb_image(original)]]), "cv.pdf")
        assert photo is not None
        assert_close(as_image(photo), original)

    def test_a_decodeparms_predictor_is_undone(self) -> None:
        original = face()
        photo = extract_profile_photo(pages_pdf([[predicted_rgb_image(original)]]), "cv.pdf")
        assert photo is not None
        assert_close(as_image(photo), original)

    def test_a_jpeg_is_passed_through_rather_than_re_encoded(self) -> None:
        image_object = jpeg_image(face())
        photo = extract_profile_photo(pages_pdf([[image_object]]), "cv.pdf")
        assert photo is not None
        assert photo.mime_type == "image/jpeg"
        assert photo.content.startswith(b"\xff\xd8\xff")
        assert photo.content in image_object

    def test_an_image_nested_in_a_form_xobject_is_found(self) -> None:
        inner = flate_rgb_image(face())
        form = stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 300 300] /Resources << /XObject << /Inner 5 0 R >> >>",
            b"q Q",
        )
        pdf = _pdf([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Fm0 4 0 R >> >> >>",
            form,
            inner,
        ])
        photo = extract_profile_photo(pdf, "cv.pdf")
        assert photo is not None and (photo.width, photo.height) == (300, 300)


class TestPickingTheFace:
    def test_a_letterhead_banner_is_not_a_headshot(self) -> None:
        assert extract_profile_photo(pages_pdf([[flate_rgb_image(face(1200, 160))]]), "cv.pdf") is None

    def test_an_icon_is_not_a_headshot(self) -> None:
        assert extract_profile_photo(pages_pdf([[flate_rgb_image(face(64, 64))]]), "cv.pdf") is None

    def test_a_full_page_scan_is_not_a_headshot(self) -> None:
        assert extract_profile_photo(pages_pdf([[flate_rgb_image(face(1240, 1754))]]), "cv.pdf") is None

    def test_the_larger_image_on_a_page_wins(self) -> None:
        photo = extract_profile_photo(
            pages_pdf([[flate_rgb_image(face(120, 120)), flate_rgb_image(face(400, 400))]]), "cv.pdf"
        )
        assert photo is not None and (photo.width, photo.height) == (400, 400)

    def test_page_one_beats_a_bigger_image_further_in(self) -> None:
        photo = extract_profile_photo(
            pages_pdf([[flate_rgb_image(face(220, 260))], [flate_rgb_image(face(600, 600))]]), "cv.pdf"
        )
        assert photo is not None and (photo.width, photo.height) == (220, 260)

    def test_a_cv_with_no_images_yields_nothing(self) -> None:
        assert extract_profile_photo(pages_pdf([[]]), "cv.pdf") is None

    def test_the_size_rule_alone(self) -> None:
        assert looks_like_a_headshot(300, 300) and looks_like_a_headshot(220, 260)
        assert not looks_like_a_headshot(64, 64)
        assert not looks_like_a_headshot(1200, 160)
        assert not looks_like_a_headshot(1240, 1754)


class TestDocx:
    def test_a_photo_in_word_media_is_found(self) -> None:
        buffer = io.BytesIO()
        face().save(buffer, format="JPEG", quality=90)
        photo = extract_profile_photo(docx({"image1.jpeg": buffer.getvalue()}), "cv.docx")
        assert photo is not None
        assert photo.mime_type == "image/jpeg" and (photo.width, photo.height) == (300, 300)

    def test_the_logo_beside_it_does_not_win(self) -> None:
        photo_bytes, logo_bytes = io.BytesIO(), io.BytesIO()
        face(400, 400).save(photo_bytes, format="PNG")
        face(1000, 120).save(logo_bytes, format="PNG")
        photo = extract_profile_photo(
            docx({"image1.png": logo_bytes.getvalue(), "image2.png": photo_bytes.getvalue()}), "cv.docx"
        )
        assert photo is not None and (photo.width, photo.height) == (400, 400)

    def test_a_document_with_no_pictures_yields_nothing(self) -> None:
        assert extract_profile_photo(docx({}), "cv.docx") is None


class TestItNeverRaises:
    @pytest.mark.parametrize(
        "raw",
        [b"", b"not a document at all", b"%PDF-1.7\ntruncated right here", b"PK\x03\x04 a zip that is not a docx"],
        ids=["empty", "garbage", "truncated-pdf", "broken-zip"],
    )
    def test_unreadable_input_yields_none(self, raw: bytes) -> None:
        assert extract_profile_photo(raw, "cv.pdf") is None
        assert extract_profile_photo(raw, "cv.docx") is None

    def test_an_unsupported_extension_is_skipped_quietly(self) -> None:
        assert extract_profile_photo(b"anything", "cv.rtf") is None

    def test_an_oversized_stream_is_refused_before_it_is_read(self) -> None:
        oversized = stream(
            b"/Type /XObject /Subtype /Image /Width 300 /Height 300 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode",
            b"\x00",
        ).replace(b"/Length 1 ", b"/Length %d " % (MAX_PHOTO_BYTES + 1))
        assert extract_profile_photo(pages_pdf([[oversized]]), "cv.pdf") is None

    def test_a_colour_space_we_cannot_place_is_skipped(self) -> None:
        image = Image.new("CMYK", (300, 300), (10, 20, 30, 40))
        cmyk = stream(
            b"/Type /XObject /Subtype /Image /Width 300 /Height 300 "
            b"/ColorSpace /DeviceCMYK /BitsPerComponent 8 /Filter /FlateDecode",
            zlib.compress(image.tobytes()),
        )
        assert extract_profile_photo(pages_pdf([[cmyk]]), "cv.pdf") is None


@pytest.mark.skipif(not os.environ.get("CV_PHOTO_CORPUS"), reason="set CV_PHOTO_CORPUS to a directory of real CVs")
def test_every_cv_in_the_corpus_gives_up_its_face() -> None:
    corpus = sorted(pathlib.Path(os.environ["CV_PHOTO_CORPUS"]).glob("*.pdf"))
    assert corpus, "CV_PHOTO_CORPUS names no PDFs"
    missed = []
    for path in corpus:
        photo = extract_profile_photo(path.read_bytes(), path.name)
        if photo is None:
            missed.append(path.name)
            continue
        assert as_image(photo).size == (photo.width, photo.height)
    assert not missed, f"no photo found in {missed}"
