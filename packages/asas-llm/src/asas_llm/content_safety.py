"""The content scanner seam, and Azure AI Content Safety behind it.

A :class:`ContentScanner` answers two questions the built-in rules
(:mod:`asas_llm.rules`) cannot: is there a prompt-injection attack hidden in
this data that no regular expression knows, and does this answer carry harm
(hate, sexual, violence, self-harm)? :mod:`asas_llm.guard` asks it on every
call when one is configured, and never when none is.

**Which text is scanned.** The data a prompt carries (a CV, a transcript, a
posting, a note) is what an attacker controls; the host's own prompt text is
not. The runner marks the template values it fills in as the untrusted text
(:data:`UNTRUSTED`, set with :func:`untrusted`), and the scanner is handed
those; a call made outside the runner is scanned whole. A host may name which
values are untrusted (``untrusted_keys``) when it passes its own briefs as
template values beside the document.

:class:`AzureContentSafety` is the one scanner shipped here. Prompt Shields
(``text:shieldPrompt``) is Microsoft's classifier for direct and indirect
attacks; the data is sent as documents, the indirect-attack check. Harm
categories (``text:analyze``) are read on the answer at or above a severity
threshold. Signs in with Entra ID by default (the process's managed identity,
or a developer's ``az login``; the resource needs the *Cognitive Services
User* role) or with a key when one is given.

Cost and time: texts are split at the service's 10,000-character limit and
the parts are checked a few at a time; a verdict is remembered per part for an
hour, so a document checked once is not sent again by the next five calls
that carry it; the requests share one pooled client per event loop. When the
service cannot answer, ``fail_mode="open"`` (default) logs it and lets the
call through on the built-in rules alone; ``"closed"`` reports
``azure_unavailable``, which an enforcing guard turns into a refusal.

Needs ``httpx`` (the ``[guard]`` extra). Entra sign-in needs ``azure-identity``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: The untrusted text of the current model call, set by the runner. ``None``
#: means "not stated": a scanner then reads every message.
UNTRUSTED: ContextVar[list[str] | None] = ContextVar("asas_llm_untrusted", default=None)


@contextmanager
def untrusted(texts: list[str] | None) -> Iterator[None]:
    """Mark ``texts`` as the untrusted data of the model calls in the block."""
    token = UNTRUSTED.set(texts)
    try:
        yield
    finally:
        UNTRUSTED.reset(token)


def as_texts(values: Iterable[Any]) -> list[str]:
    """The untrusted values of a prompt, as text: strings as they are,
    anything structured as JSON (a profile, a list of answers)."""
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, str):
            text = v
        elif hasattr(v, "content"):  # a message
            text = v.content if isinstance(v.content, str) else json.dumps(v.content, default=str)
        elif hasattr(v, "model_dump"):
            text = json.dumps(v.model_dump(mode="json"), ensure_ascii=False)
        else:
            text = json.dumps(v, ensure_ascii=False, default=str)
        if text.strip():
            out.append(text)
    return out


@runtime_checkable
class ContentScanner(Protocol):
    """What the guard asks of a scanner. Both methods return rule names
    (``azure_prompt_shield``, ``azure_hate``), never text."""

    def enabled(self) -> bool: ...

    async def prompt_findings(self, texts: list[str]) -> list[str]: ...

    async def response_findings(self, text: str) -> list[str]: ...


_API = "2024-09-01"
_SCOPE = "https://cognitiveservices.azure.com/.default"
#: The service's per-request limit, in characters.
CHUNK = 10_000
#: Requests in flight per check.
_AT_ONCE = 4
_VERDICT_TTL = 3600.0
_VERDICTS_MAX = 4096
_MAX_CONNECTIONS = 32
#: How long an idle pooled connection is kept: long enough to carry the
#: analyze after a model call into the next call's shield, below the ~4 minute
#: idle limit of Azure's front end.
_KEEPALIVE_SECONDS = 120.0

Setting = str | float | int | Callable[[], Any]


def _read(value: Setting) -> Any:
    return value() if callable(value) else value


@dataclass
class _Pool:
    """What one event loop keeps: the HTTP client, the credential (its aio
    transport is bound to the loop it first ran on, like the client) and the
    lock that makes a token refresh single-flight."""

    loop: asyncio.AbstractEventLoop
    client: Any
    token_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    credential: Any = None


def default_credential() -> Any:
    from azure.identity.aio import DefaultAzureCredential

    return DefaultAzureCredential()


class AzureContentSafety:
    """Azure AI Content Safety as a :class:`ContentScanner`.

    Every setting may be a value or a zero-argument callable returning one,
    so a host whose settings are live is read on each call. An empty
    ``endpoint`` means the scanner is off.
    """

    def __init__(
        self,
        endpoint: Setting = "",
        key: Setting = "",
        *,
        fail_mode: Setting = "open",
        severity_threshold: Setting = 4,
        timeout_seconds: Setting = 5.0,
        credential_factory: Callable[[], Any] = default_credential,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._key = key
        self._fail_mode = fail_mode
        self._severity = severity_threshold
        self._timeout = timeout_seconds
        self._credential_factory = credential_factory
        self._client_factory = client_factory
        self._verdicts: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        #: The Entra ID token, shared by every loop (it is only a string).
        self._token: dict[str, Any] = {"value": None, "expires": 0.0}
        self._pools: dict[int, _Pool] = {}

    # -- settings ------------------------------------------------------------

    def enabled(self) -> bool:
        return bool(_read(self._endpoint))

    def _url(self, op: str) -> str:
        return f"{str(_read(self._endpoint)).rstrip('/')}/contentsafety/text:{op}?api-version={_API}"

    # -- the pooled client ---------------------------------------------------

    def _new_client(self) -> Any:
        import httpx

        kwargs = {
            "timeout": float(_read(self._timeout)),
            "limits": httpx.Limits(
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_CONNECTIONS,
                keepalive_expiry=_KEEPALIVE_SECONDS,
            ),
        }
        return (self._client_factory or httpx.AsyncClient)(**kwargs)

    def _pool(self) -> _Pool:
        """One pooled client per event loop. A worker pool may run a loop per
        thread, and an httpx client is bound to the loop it first ran on, so
        the pool is keyed by the loop; one whose loop has closed is dropped."""
        loop = asyncio.get_running_loop()
        for key in [k for k, p in self._pools.items() if p.loop.is_closed() or p.client.is_closed]:
            self._pools.pop(key, None)
        held = self._pools.get(id(loop))
        if held is not None and held.loop is loop:
            return held
        pool = _Pool(loop=loop, client=self._new_client())
        self._pools[id(loop)] = pool
        return pool

    async def close(self) -> None:
        """Close the pooled clients (process shutdown). Only the running
        loop's can be closed from here; the others' loops are ending with the
        process."""
        loop = asyncio.get_running_loop()
        for key, pool in list(self._pools.items()):
            self._pools.pop(key, None)
            if pool.loop is not loop:
                continue
            try:
                await pool.client.aclose()
                if pool.credential is not None:
                    await pool.credential.close()
            except Exception as exc:  # noqa: BLE001 - shutdown goes on regardless
                log.info("content_safety: close failed (%s)", type(exc).__name__)

    # -- sign-in -------------------------------------------------------------

    def _token_fresh(self) -> bool:
        return self._token["value"] is not None and self._token["expires"] - 300 >= time.time()

    async def _headers(self) -> dict[str, str]:
        key = _read(self._key)
        if key:
            return {"Ocp-Apim-Subscription-Key": str(key)}
        if not self._token_fresh():
            pool = self._pool()
            # Single-flight: a check sends its parts concurrently and a loop
            # runs several checks at once, so an expired token used to be
            # fetched by every one of them.
            async with pool.token_lock:
                if not self._token_fresh():
                    if pool.credential is None:
                        pool.credential = self._credential_factory()
                    token = await pool.credential.get_token(_SCOPE)
                    self._token["value"], self._token["expires"] = token.token, float(token.expires_on)
        return {"Authorization": f"Bearer {self._token['value']}"}

    # -- verdict memory ------------------------------------------------------

    def _remember(self, key: str, attacked: bool) -> None:
        self._verdicts[key] = (time.monotonic(), attacked)
        self._verdicts.move_to_end(key)
        while len(self._verdicts) > _VERDICTS_MAX:
            self._verdicts.popitem(last=False)

    def _recall(self, key: str) -> bool | None:
        held = self._verdicts.get(key)
        if held is None or time.monotonic() - held[0] > _VERDICT_TTL:
            return None
        self._verdicts.move_to_end(key)
        return held[1]

    # -- requests ------------------------------------------------------------

    async def _post(self, client: Any, op: str, body: dict) -> dict:
        """One request, asked once more after a short wait on 429 or 5xx, or
        at once when a pooled connection turns out to be stale."""
        import httpx

        stale = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)
        for attempt in (0, 1):
            try:
                response = await client.post(self._url(op), headers=await self._headers(), json=body)
            except stale:
                if attempt == 0:
                    continue
                raise
            if response.status_code == 200:
                return response.json()
            if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
                await asyncio.sleep(0.5)
                continue
            raise RuntimeError(f"content safety {op} answered {response.status_code}")
        raise RuntimeError("unreachable")  # pragma: no cover

    def _unavailable(self, op: str, exc: Exception) -> list[str]:
        log.warning("ai_guard.content_safety_unavailable op=%s error=%s", op, type(exc).__name__)
        return ["azure_unavailable"] if str(_read(self._fail_mode)).lower() == "closed" else []

    @staticmethod
    def _chunks(texts: list[str]) -> list[str]:
        return [t[i : i + CHUNK] for t in texts for i in range(0, len(t), CHUNK)]

    async def prompt_findings(self, texts: list[str]) -> list[str]:
        """``["azure_prompt_shield"]`` when Prompt Shields finds an attack in
        any of these texts (sent as documents), else ``[]``."""
        if not self.enabled():
            return []
        parts = self._chunks([t for t in texts if t])
        keys = [hashlib.sha256(p.encode()).hexdigest() for p in parts]
        todo = [(k, p) for k, p in zip(keys, parts, strict=True) if self._recall(k) is None]
        try:
            if todo:
                gate = asyncio.Semaphore(_AT_ONCE)
                client = self._pool().client

                async def shield(key: str, part: str) -> None:
                    async with gate:
                        body = await self._post(client, "shieldPrompt", {"userPrompt": "", "documents": [part]})
                    attacked = bool((body.get("userPromptAnalysis") or {}).get("attackDetected")) or any(
                        d.get("attackDetected") for d in body.get("documentsAnalysis") or []
                    )
                    self._remember(key, attacked)

                await asyncio.gather(*(shield(k, p) for k, p in todo))
        except Exception as exc:  # noqa: BLE001 - an outage must not fail the call by itself
            return self._unavailable("shieldPrompt", exc)
        return ["azure_prompt_shield"] if any(self._recall(k) for k in keys) else []

    async def response_findings(self, text: str) -> list[str]:
        """``azure_<category>`` for each harm category at or above the threshold."""
        if not self.enabled() or not text:
            return []
        threshold = int(_read(self._severity))
        try:
            gate = asyncio.Semaphore(_AT_ONCE)
            client = self._pool().client

            async def analyze(part: str) -> dict:
                async with gate:
                    return await self._post(client, "analyze", {"text": part})

            answers = await asyncio.gather(*(analyze(p) for p in self._chunks([text])))
        except Exception as exc:  # noqa: BLE001
            return self._unavailable("analyze", exc)
        found = {
            f"azure_{str(item.get('category', 'harm')).lower()}"
            for body in answers
            for item in body.get("categoriesAnalysis") or []
            if int(item.get("severity") or 0) >= threshold
        }
        return sorted(found)


class NoScanner:
    """The scanner for a host without one: off, finds nothing."""

    def enabled(self) -> bool:
        return False

    async def prompt_findings(self, texts: list[str]) -> list[str]:
        return []

    async def response_findings(self, text: str) -> list[str]:
        return []


__all__ = [
    "CHUNK",
    "UNTRUSTED",
    "AzureContentSafety",
    "ContentScanner",
    "NoScanner",
    "as_texts",
    "default_credential",
    "untrusted",
]
