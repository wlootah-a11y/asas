"""Per-call knobs that change without a deploy: which model a call runs on,
how many tokens it may write, and which models may be named at all.

All three are read from one string a host keeps in its settings, in the shape
``prefix=value,prefix=value``, matched against the CALL NAME (the name the
runner traces the call under) by longest prefix first. A value may be given
as the string itself or as a zero-argument callable that returns it, so a host
whose settings object is live (reloaded, or patched in a test) is read every
time without the package knowing how.

* :class:`ModelOverrides` (``llm.model_overrides``): ``screening-=gpt-4.1``
  moves every call whose name starts with ``screening-`` onto ``gpt-4.1``, so
  a faster model can take a call the moment the gateway grants one.
* :class:`OutputCaps` (``llm.max_tokens`` over ``llm.max_tokens_default``):
  how many tokens a call may generate. Nothing capped a call before; a
  generation that ran away held its gateway slot until the request timeout
  and was then retried. Caps are set well above any answer seen, so only a
  runaway reaches one, and a call that stops at its cap is logged as
  ``llm_output_truncated`` by :class:`~asas_llm.callbacks.TruncatedOutputLogger`.
* :class:`ModelAllowlist` (``llm.allowed_models``): a request naming any other
  model is refused before a client is even built, so it never reaches the
  gateway. Checked after the overrides have had their say, so neither a code
  change nor a settings change can route a call to an unapproved model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from .errors import ModelNotAllowed

log = logging.getLogger(__name__)

Setting = str | int | Callable[[], Any]


def _read(value: Setting) -> Any:
    return value() if callable(value) else value


def _pairs(raw: str, parse: Callable[[str], Any], what: str) -> list[tuple[str, Any]]:
    """``prefix=value`` pairs, longest prefix first. A bad pair is skipped
    and said so, never a crash at the first call."""
    pairs: list[tuple[str, Any]] = []
    for item in (raw or "").split(","):
        prefix, _, value = item.partition("=")
        prefix, value = prefix.strip(), value.strip()
        if not prefix or not value:
            continue
        try:
            pairs.append((prefix, parse(value)))
        except (ValueError, TypeError):
            log.warning("%s: bad value %r for %r, skipped", what, value, prefix)
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


class _Parsed:
    """One parse per distinct setting value: the string is read every call
    (it may be live), the parse is not repeated while it is unchanged."""

    def __init__(self, parse: Callable[[str], Any], what: str) -> None:
        self._parse, self._what = parse, what
        self._raw: str | None = None
        self._pairs: list[tuple[str, Any]] = []

    def pairs(self, raw: str) -> list[tuple[str, Any]]:
        if raw != self._raw:
            self._pairs, self._raw = _pairs(raw, self._parse, self._what), raw
        return self._pairs


class ModelOverrides:
    """``model_for(call_name, model)``: the model a call runs on, its own
    unless the setting names another for its prefix.

    ``known`` validates an override's target (``lambda m: LLMModel(m).value``
    for a host with an enum of models); an unknown target is skipped with a
    warning. Without it any string is accepted.
    """

    def __init__(self, raw: Setting = "", *, known: Callable[[str], str] | None = None) -> None:
        self._raw = raw
        self._parsed = _Parsed(known or (lambda m: m), "model_overrides")

    def model_for(self, name: str | None, model: str) -> str:
        for prefix, override in self._parsed.pairs(str(_read(self._raw) or "")):
            if name and name.startswith(prefix):
                return override
        return model


class OutputCaps:
    """``max_tokens_for(call_name)``: the output cap for a call, or ``None``
    for no cap. ``default`` (``0`` is no cap) applies to every call the
    setting does not name."""

    def __init__(self, raw: Setting = "", default: Setting = 0) -> None:
        self._raw, self._default = raw, default
        self._parsed = _Parsed(int, "max_tokens")

    def max_tokens_for(self, name: str | None) -> int | None:
        cap = int(_read(self._default) or 0)
        for prefix, tokens in self._parsed.pairs(str(_read(self._raw) or "")):
            if name and name.startswith(prefix):
                cap = tokens
                break
        return cap if cap > 0 else None


class ModelAllowlist:
    """The approved models. ``models`` is a comma-separated string (or an
    iterable of names, or a callable returning either); case and spaces do
    not matter. An empty list approves nothing. ``refusal`` builds the error
    raised, so a host may raise its own class."""

    def __init__(
        self,
        models: Setting | Iterable[str] = "",
        *,
        refusal: Callable[[str, str], Exception] = ModelNotAllowed,
    ) -> None:
        self._models = models
        self._refusal = refusal

    def allowed_models(self) -> frozenset[str]:
        raw = _read(self._models) if callable(self._models) else self._models
        names = raw.split(",") if isinstance(raw, str) else list(raw or ())
        return frozenset(str(m).strip().lower() for m in names if str(m).strip())

    def is_allowed(self, model: str) -> bool:
        return str(model).strip().lower() in self.allowed_models()

    def ensure_allowed(self, model: str, purpose: str = "chat") -> None:
        """Raise unless ``model`` is approved."""
        if not self.is_allowed(model):
            raise self._refusal(str(model), purpose)


__all__ = ["ModelAllowlist", "ModelOverrides", "OutputCaps", "Setting"]
