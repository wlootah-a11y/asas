"""asas-cv-profile: a CV read into a host's profile model, every value citing its lines.

* **Profile model.** The host's sections and fields in one YAML file
  (:func:`load_model`), in the host's own words; a section names its kind (work,
  education, skills...) and a field its role, which is all the code needs to know.
* **Lines.** A CV becomes numbered lines with no model (:func:`read_cv`): Document
  Intelligence for PDFs (:class:`LayoutReader`), asas-cv for Word and text, refusals
  with a stable code (:class:`Refused`).
* **The model.** An asas-agent release fills the sections, every entry citing line
  ranges (:class:`Asker`, :func:`default_release`); the provider and the model are the
  host's.
* **Code settles it.** :class:`ProfileBuilder` checks every value against its lines,
  asks again only for what came back empty or cited lines the text lacks, folds a
  bilingual CV's twins, keeps skills in the CV's words, works out durations and totals
  with overlaps counted once, and keeps every line no field took.
* **Upload page.** :func:`upload_router` (the ``serve`` extra), and the
  ``asas-cv-profile`` command.

Table-less and router-optional variant of the Asas host contract: no session, no
``migrate``, no ``seed``, no ``configure``; the package reads no settings (the command
line does). ``import asas_cv_profile`` loads neither the Agents SDK nor the matching
libraries: the modules that need them load on first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .lines import CODES, LayoutReader, Lines, Refused, read_cv
from .model import (
    BUDGET,
    COMPUTED,
    KINDS,
    ROLES,
    TYPES,
    ModelError,
    ProfileModel,
    Section,
    Spec,
    load_model,
    parse_model,
)

if TYPE_CHECKING:  # pragma: no cover
    from .answers import personal
    from .build import ProfileBuilder
    from .calls import Asker, default_release
    from .report import review, summary, write_outputs
    from .seams import Ask, CallResult, Finish, PdfReader
    from .shapes import Built, Cell, Entry
    from .web import upload_router

__version__ = "0.1.0"

_LAZY: dict[str, str] = {
    "personal": "answers",
    "ProfileBuilder": "build",
    "Ask": "seams",
    "Finish": "seams",
    "PdfReader": "seams",
    "Built": "shapes",
    "Cell": "shapes",
    "Entry": "shapes",
    "Asker": "calls",
    "CallResult": "seams",
    "default_release": "calls",
    "review": "report",
    "summary": "report",
    "write_outputs": "report",
    "upload_router": "web",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is not None:
        from importlib import import_module

        return getattr(import_module(f".{module_name}", __name__), name)
    raise AttributeError(f"module 'asas_cv_profile' has no attribute {name!r}")


__all__ = [
    "BUDGET",
    "CODES",
    "COMPUTED",
    "KINDS",
    "ROLES",
    "TYPES",
    "Ask",
    "Asker",
    "Built",
    "CallResult",
    "Cell",
    "Entry",
    "Finish",
    "LayoutReader",
    "Lines",
    "ModelError",
    "PdfReader",
    "ProfileBuilder",
    "ProfileModel",
    "Refused",
    "Section",
    "Spec",
    "__version__",
    "default_release",
    "load_model",
    "parse_model",
    "personal",
    "read_cv",
    "review",
    "summary",
    "upload_router",
    "write_outputs",
]
