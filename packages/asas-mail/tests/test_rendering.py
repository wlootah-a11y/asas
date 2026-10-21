"""The host's template inside the shell, escaped unless the template says
otherwise, with every variable arriving as a string."""

from __future__ import annotations

from pathlib import Path

import pytest

from asas_mail import Renderer, template_strings


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    (tmp_path / "welcome.html").write_text(
        "<p>Hello {{ first_name }}, your role is {{ role }}.</p>"
        "{% if candidate_facing %}<p>candidate</p>{% endif %}"
        "{{ notification_body | safe }}",
        encoding="utf-8",
    )
    return tmp_path


def test_the_body_lands_inside_the_shipped_shell(templates: Path) -> None:
    html = Renderer(templates).render(
        "welcome.html", subject="Welcome", variables={"first_name": "Noura", "role": "Admin"}
    )

    assert "<title>Welcome</title>" in html
    assert "<h1>Welcome</h1>" in html
    assert "Hello Noura, your role is Admin." in html


def test_variables_are_escaped_unless_the_template_marks_one_safe(templates: Path) -> None:
    html = Renderer(templates).render(
        "welcome.html",
        subject="s",
        variables={"first_name": "<b>x</b>", "role": "r", "notification_body": "<em>ok</em>"},
    )

    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<em>ok</em>" in html


def test_a_candidate_facing_message_drops_the_title_band(templates: Path) -> None:
    staff = Renderer(templates).render("welcome.html", subject="s", variables={"first_name": "a", "role": "r"})
    candidate = Renderer(templates).render(
        "welcome.html", subject="s", variables={"first_name": "a", "role": "r", "candidate_facing": True}
    )

    assert "<h1>s</h1>" in staff
    assert "<h1>s</h1>" not in candidate
    assert "<p>candidate</p>" in candidate


def test_a_hosts_own_shell_shadows_the_shipped_one(templates: Path) -> None:
    (templates / "base.html").write_text("<main>{{ email_title }}|{{ body | safe }}</main>")

    html = Renderer(templates).render("welcome.html", subject="T", variables={"first_name": "a", "role": "r"})

    assert html.startswith("<main>T|<p>Hello a")


def test_a_shell_can_be_named_per_message(templates: Path) -> None:
    (templates / "letter.html").write_text("<letter>{{ body | safe }}</letter>")

    html = Renderer(templates).render(
        "welcome.html", subject="T", variables={"first_name": "a", "role": "r"}, shell="letter.html"
    )

    assert html.startswith("<letter>")


def test_the_recipient_reaches_the_shell(templates: Path) -> None:
    html = Renderer(templates).render(
        "welcome.html", subject="s", variables={"first_name": "a", "role": "r", "recipient": "x@y.co"}
    )

    assert "sent to x@y.co" in html


def test_render_body_has_no_shell(templates: Path) -> None:
    body = Renderer(templates).render_body("welcome.html", {"first_name": "a", "role": "r"})

    assert body.startswith("<p>Hello a")
    assert "<html" not in body


def test_template_strings_are_the_shape_templates_were_written_for() -> None:
    assert template_strings({"flag": True, "off": False, "n": 3, "gone": None, "s": "x"}) == {
        "flag": "true",
        "off": "",
        "n": "3",
        "s": "x",
    }
