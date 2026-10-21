"""A body template inside a shell, with autoescaping on.

The host owns its templates: a directory of Jinja files, one per message,
plus a shell (the chrome around every message: the title band, the footer)
the body is dropped into. The package ships ONE shell, ``base.html``, for a
host that has none yet; a host with its own letterhead names its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

DEFAULT_SHELL = "base.html"
PACKAGE_TEMPLATES = Path(__file__).resolve().parent / "templates"


def template_strings(variables: dict[str, Any]) -> dict[str, str]:
    """Every variable as a string, the shape a template is written for.

    A flag becomes ``"true"`` or ``""`` so ``{% if candidate_facing %}`` reads
    it either way, ``None`` is left out (a missing key is already "unset"),
    and anything else is its ``str()``.
    """
    out: dict[str, str] = {}
    for key, value in variables.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else ""
        else:
            out[key] = value if isinstance(value, str) else str(value)
    return out


class Renderer:
    """Renders ``template`` with ``variables`` and drops the result into the
    shell as ``body``, with ``email_title`` set to the subject.

    ``templates_dir`` is searched first and the package's own directory
    second, so a host's ``base.html`` shadows the shipped one. Autoescaping is
    on for ``.html``; a template that must render markup from a variable
    marks it ``| safe`` itself, deliberately, at that one place.
    """

    def __init__(
        self,
        templates_dir: str | Path | None = None,
        *,
        shell: str = DEFAULT_SHELL,
    ) -> None:
        loaders = []
        if templates_dir is not None:
            loaders.append(FileSystemLoader(str(templates_dir)))
        loaders.append(FileSystemLoader(str(PACKAGE_TEMPLATES)))
        self._env = Environment(
            loader=ChoiceLoader(loaders),
            autoescape=select_autoescape(["html", "htm", "xml"]),
        )
        self.shell = shell

    def render(
        self,
        template: str,
        *,
        subject: str,
        variables: dict[str, Any] | None = None,
        shell: str | None = None,
    ) -> str:
        """The full HTML of one message."""
        context = template_strings(variables or {})
        body = self._env.get_template(template).render(**context)
        return self._env.get_template(shell or self.shell).render(
            email_title=subject,
            email_subtitle=context.get("email_subtitle", ""),
            body=body,
            show_header=not context.get("candidate_facing"),
            **context,
        )

    def render_body(self, template: str, variables: dict[str, Any] | None = None) -> str:
        """The body alone, with no shell around it."""
        return self._env.get_template(template).render(**template_strings(variables or {}))
