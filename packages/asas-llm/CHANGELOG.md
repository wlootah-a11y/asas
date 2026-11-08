# Changelog — `asas-llm`

Versions follow semver, and the git tag matches this file: `asas-llm/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — unreleased

First release. Extracted from an AI engine's working LLM layer (about 400 lines:
a LangChain runner, a Langfuse prompt store, a strict-JSON-schema walker and
the Langfuse-under-LangChain-1.x shim), with the two pieces that engine did not
have and the roadmap named as the reason it was not yet a library.

- **Prompts as data, registry-first with two fallbacks** (`Prompt`, `PromptStore`,
  `LayeredPromptStore`, `LocalPromptStore`, `InMemoryPromptStore`,
  `asas_llm.langfuse.LangfusePromptStore`). Every read goes registry, then the
  last copy successfully fetched, then a file shipped with the image, and raises
  `PromptUnavailable` naming all three only when all three have nothing. In the
  source engine the registry was a hard runtime dependency: a replica that could
  not reach Langfuse had no prompts. Here it is where prompts are edited, not
  what the process needs in order to exist. `Prompt.source` says which layer
  answered; the outage is logged once per window, not once per call.
- **`python -m asas_llm.snapshot`** pulls the registry into the local directory
  in the format `LocalPromptStore` reads, leaving unchanged files untouched so
  a commit of the directory is a real diff. Run it in the image build.
- **Per-request tracing with the id handed back** (`bind_request`, `traced`,
  `trace_ids`, `last_trace_id`, `TraceIdMiddleware`, `Tracer`, `NullTracer`,
  `asas_llm.langfuse.LangfuseTracer`). The trace id is minted before the backend
  is asked, so it is on every `LLMResult` and every `LLMError` whether or not
  Langfuse answered. A fan-out (`asyncio.gather`) records every sub-call on the
  request that will report them. A call made inside an active trace joins it
  rather than opening an orphan. The source engine set the id into a context
  variable and read it nowhere; the middleware and the register are the
  hand-back.
- **Structured output** (`strict_json_schema`, `response_format`,
  `parse_structured`). Ask the provider for strict JSON, strip code fences and
  repair before validating, and on failure raise `StructuredOutputError` with
  the raw text and pydantic's error list attached. The walker is the OpenAI SDK's
  private one, ported so this package does not import `openai` for a schema
  transform.
- **`LLMRunner.ainvoke`** (`asas_llm.runners`): prompt name in, `LLMResult` out,
  typed errors instead of `None`. Model is a string chosen by the prompt's
  config or the call, built by a host-supplied factory (`openai_compatible`,
  `azure_openai`, or the host's own). `LLMRunner.trace` gives host-owned agents
  and graphs a traced `RunnableConfig` with the same nesting.
- **Host contract**: table-less, router-less. `configure(...)` + lazy `runner()`
  + `set_runner()` for tests, mirroring `asas_storage`. `import asas_llm` loads
  neither Langfuse nor LangChain; both are optional extras (`[langfuse]`,
  `[langchain]`, `[openai]`) imported on use, and the module that needs one says
  which extra to install when it is missing.
- Langfuse is pinned `>=2.50,<3`. The 3.x SDK is a different API; it gets its own
  `Tracer`/`PromptStore` adapter when a host needs it, which is why those are
  protocols.
- **The gateway layers** (added 19 Oct 2026, from the same engine's
  `base/llm/gateway`, `admission`, `provenance` and `output_caps`, about
  1,600 lines of code and 1,400 of tests, with their reasoning):
  - `asas_llm.guard` / `rules` / `content_safety`: every chat completion
    inspected before it is sent (prompt-injection rules plus a
    `ContentScanner`, Azure AI Content Safety shipped, on the untrusted text
    the runner marks) and before its answer is used (unsafe-answer rules plus
    harm categories). `GuardPolicy` settles a finding per direction as
    `off | monitor | enforce`; `guarded(ChatModelClass, policy=...)` builds
    the client class with the guard, the admission slot, a timer and outbound
    headers in its four completion methods. A finding is logged by rule name,
    never by text, counted in `GUARD_EVENTS`, and refused as `UnsafeContent`
    (or the host's class).
  - `asas_llm.admission` / `slots`: `Admission` caps model requests in flight
    across every process on a Redis sorted set of leases (`WideSemaphore`),
    with an interactive lane bulk work gives way to, a per-process fallback
    share when Redis is unreachable, and `shares_quota` for clients off the
    chat gateway. The Redis client is injected; the package imports none.
  - `asas_llm.provenance`: `PromptRef`, `CallProvenance`, `OutputProvenance`,
    `recording()`, `identity()`, `of_calls()`. Every `LLMResult` now carries
    its `CallProvenance`; only a registry-served prompt names a version.
  - `asas_llm.overrides`: `ModelOverrides`, `OutputCaps`, `ModelAllowlist`,
    `prefix=value` strings matched on the call name by longest prefix, read
    live from a value or a callable. The runner applies them (model, cap,
    allowlist before any client exists); `asas_llm.callbacks.TruncatedOutputLogger`
    logs a call that stopped at its cap.
  - `asas_llm.masking.mask_prefixed(prefixes)`: a Langfuse mask by key prefix
    that never raises.
  - `asas_llm.evals`: `EvalCase`, `run_cases` (pass^N, expectations, a host
    check, a model `Judge`), `cases_from_langfuse`.
  - `LLMRunner.ainvoke` gains `reasoning_effort`, `request_timeout`,
    `untrusted_keys` and `log_detail`; `ModelOptions` gains `max_tokens`,
    `reasoning_effort`, `request_timeout`, `max_retries` and `callbacks`; the
    two factories take `client_class`. A busy gateway is raised as
    `GatewayBusy` and a guard refusal as itself, never as `GenerationFailed`
    (`busy_cause`, `refusal_cause` in `errors`). One `llm_call` log line per
    answered call, never the answer's text.
  - New extra `[guard]` (`httpx`), and `pytest-asyncio` in `[dev]`.
- Licensed **Apache 2.0**, with `LICENSE` and `NOTICE` in the wheel.
- `tests/test_host_contract.py` per TEAMY-798, plus a test that `import asas_llm`
  loads no optional SDK, run in a fresh interpreter.
