# Changelog: `asas-agent`

Versions follow semver, and the git tag matches this file: `asas-agent/v0.2.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.5.0

What Langfuse needs to find a run and the prompt version behind it, on the Langfuse
2.x client the package already pins, and a release with no skill called with no
skill machinery. A minor, because what the model reads changes for a skill-less
release (a host may have measured its prompt against the old text); a release with
skills reads exactly what it read on 0.4.0, a host that passes nothing new sends
what it sent, and no release id changes. Moving to the Langfuse SDK v5
(OpenTelemetry) is not part of this release (DESIGN.md).

- **No `activate_skill` without a skill.** `described_tools` offers it only when the
  release (or the stage's view of it) holds at least one skill, so a one-shot
  release calls the model with its host tools alone, often none. On a gateway's
  chat completions a model that refuses a reasoning effort beside a function tool
  (gpt-5.5) refused every such call, though the tool could never be used; every
  call is also the tool's description shorter.
- **No empty skill sections.** `instructions_for` renders `## Skills` and
  `## Active skill` (with the renderer's `no_skill`) only when there is a skill;
  the prompt, the host's head and tail sections are unchanged.
- **`TraceAttributes`** (`user_id`, `session_id`, `tags`, `version`, `release`,
  `metadata`; frozen, all optional) and `run_agent(..., trace=)`. The processor sends
  user, tags, version and release on the Langfuse trace. `group_id` and
  `trace_metadata` keep working and win over `trace` where both name the same thing.
  They travel as reserved `langfuse.*` keys in the SDK trace's metadata, stripped
  from what is forwarded, so a host that opens its own `trace(...)` writes the same
  keys (README, "Tracing attributes and the prompt link").
- **The prompt link.** `LangfuseStore.read` returns `source` (the Langfuse prompt's
  `name`, `version` and the `label` read) in the release. It is provenance: it never
  enters the content hash, so ids are unchanged, and `build_release` has none. Given
  such a release, `run_agent` links every generation of the run to that prompt
  version (`generation(prompt=...)`), also inside a trace the host opened.
- **Fix:** a generation from the Responses API reached Langfuse with `model=None`
  (the span exports none). It now gets the response's model, else the run's.
- **`LangfuseTracingProcessor.shutdown()`** flushes once, later calls do nothing, and
  neither it nor `force_flush()` raises. The client stays open: it is the host's.

## 0.4.0

The agent as configuration: a release carries its settings and lives in a store the
host chooses, read through a label, so an agent changes without a deployment. A
minor, as 0.3.0 was, because the stores and their commands are new public API;
nothing a host on 0.3.1 calls changes, and a release whose agent.yaml names no
settings keeps its id.

- **`settings` in a release**, from `agent.yaml`: `model`, the SDK's model settings
  (temperature, reasoning, max_tokens...), laid over the loop's own by
  `model_settings(conversation_id, release)`; and `tracing`, `enabled` and
  `sensitive`. Present only when named, so every existing id holds.
- **Tracing per agent.** `run_agent` traces a run only when its release's
  `settings.tracing.enabled` says so (it was always off), named after the release,
  with `release_id` in the metadata beside the host's new `trace_metadata`, in the
  host's new `group_id` session; `sensitive` decides whether the trace keeps the
  run's content. Where it goes is the host's `set_trace_processors`: a host that
  enables tracing without replacing the SDK's processors sends traces to
  api.openai.com.
- **Release stores.** `ReleaseStore` (a seam: `read(agent, label)`,
  `publish(agent, release)`, `promote(agent, release_id, label)`), `FileStore`
  (`<id>.json` and `<agent>/<label>.json`) and `LangfuseStore` (one Langfuse text
  prompt per agent, the instructions as its text and the rest of the release as its
  config; the `langfuse` extra). `fetch_release(store, agent, label, model=)` reads a
  label and verifies what came back with `release_from`, which checks the shape,
  refuses content that does not hash to the id it claims and gives content that
  claims none (an edit in Langfuse's editor) the id of what it says. `read_release`
  and `load_release` are unchanged.
- **`asas-agent publish` and `asas-agent promote`**, for a release pipeline:
  `--store file:<directory>` or `--store langfuse`.
- **Fix:** `LangfuseTracingProcessor` sends a generation's model parameters
  flat, a nested one (the SDK's `reasoning`) as its JSON; the 2.x client
  refused the object and the generation was lost.

- **A typed answer.** `run_agent(..., output_type=)` hands the type to the SDK's
  `Agent` (a pydantic model, a dataclass or a TypedDict), so the final answer is
  validated against its strict schema. The result gains `output`: the typed
  answer, or None without `output_type` and for a run that ended at a question.
  `answer` keeps the text, empty for a typed answer.
- **`GuardedModel(inner, inspect_prompt=, inspect_response=)`** (loaded on first
  use): the host's inspection around every model call of a run, passed as
  `run_model`. The prompt inspector gets the texts the call sends (messages and
  tool results, not the system instructions) and the system instructions; the
  response inspector gets the answer's text and every tool call's arguments. An
  inspector that raises refuses the call, and its exception ends the run as it
  is. The package holds no rules. It does not stream: a streamed answer would
  reach the run before it was inspected.
  A by-reference model behind it still learns the active skill (`bind` passes
  through), and the inner model's retry advice reaches the SDK.

## 0.3.1

- **Read-only tools of one model answer run together.** A tool whose descriptor
  in the snapshot's `tools` says `"read_only": true` no longer waits for the
  calls beside it; any other tool, `activate_skill` and the ask tool included,
  waits for every call before it and holds back every call after it, as every
  tool did in 0.3.0. The trace, its indexes and the results the model reads keep
  the order the model issued the calls in. A host that declares no tool
  read-only sees no change.

## 0.3.0

The SDK loop, moved from the first host's agent runtime behind seams the host
fills. Additive: nothing in 0.2.1 changes, and a host on 0.2.1 needs no code
change to take it. A minor all the same, because the loop is the package's new
public API.

- **`run_agent(snapshot, dispatch, ask, *, renderer, model=, provider=,
  run_model=, trace_sink=, conversation_id=)`** runs a release's skills on the
  OpenAI Agents SDK until the agent answers, the ask handler raises `AskPending`
  or an `AgentsException` ends the run. `activate_skill` swaps the playbook and
  the tools on the next step; a tool outside the active skill is refused with the
  skill to activate; a failed tool is a typed refusal the model reads; the
  release's `max_tools` raises `ToolBudgetExceeded`. The result keeps its fields:
  `answer`, `history`, `active_skill`, `tool_trace`, `record`, `usage`, `ended`.
- **The seams** (`asas_agent.seams`, named at import without the SDK):
  `ToolDispatcher` and `AskHandler`, the callables the loop awaits; `SkillAware`,
  a run model told the active skill (a model called by reference); and
  `ContextRenderer`, the host's record and words: the tools every skill holds
  (`always`), the ask tool's name, `activate_skill`'s description, the words while
  no skill is active, the sections before the skill catalogue (`head`) and after
  the active skill (`tail`), the record a tool result carries (`record`) and the
  input items that open the run (`opening`).
- **Pure helpers a host's model by reference rebuilds from the run's snapshot**:
  `instructions_for(snapshot, active_skill, renderer)`,
  `described_tools(snapshot, renderer)`, `model_settings(conversation_id)`; and
  `failure_tool_trace`, `run_usage`, `empty_usage`, `SkillState`.
- Loaded on first use like the eval models: `import asas_agent` still loads
  neither the SDK nor the OpenAI client. The loop reads no clock, environment or
  global, so a host may run it inside a workflow sandbox; the first host proves
  it in its Temporal lane.
- Tracing stays off in the loop's `RunConfig` (`tracing_disabled=True`), as in
  the host it came from, so a `LangfuseTracingProcessor` sees no run that
  `run_agent` starts yet. A switch for it is a later, additive change.

## 0.2.1 (unreleased)

Additive: a host that is on 0.2.0 changes nothing.

- **Production providers** (`asas_agent.providers`, loaded on first use):
  `gateway_provider(api_key, base_url, timeout, max_retries, use_responses,
  default_headers)` for OpenAI or any gateway that speaks its API, and
  `azure_provider(api_key, endpoint, api_version, ...)` for Azure OpenAI.
  The counterpart of the eval-only `GatewayModels`: an explicit timeout and
  retry count on the client (the SDK's defaults are ten minutes and two
  retries), and `use_responses=False` for a gateway that serves chat
  completions only, where every run then uses `OpenAIChatCompletionsModel`.
- **Langfuse tracing** (`asas_agent.tracing.LangfuseTracingProcessor`, loaded
  on first use): the SDK's run traces written one to one to a Langfuse 2.x
  client the host already has. The run is a trace with the same id (less the
  `trace_` prefix) in the group id's session; a generation or response span
  is a Langfuse generation with model, messages and usage; every other span
  is a Langfuse span named by kind and name, a tool call keeping its
  arguments and result; an error is the `ERROR` level. Spans are written when
  they end, a `mask(data=...)` is applied to every payload (a mask that
  raises sends `"[mask failed]"` for that field, never the raw value), and a
  failing backend never fails the run. Install it with
  `set_trace_processors([...])`: `add_trace_processor` keeps the SDK's
  default exporter, which sends traces to api.openai.com.
- `gateway_provider`, `azure_provider` and `LangfuseTracingProcessor` are
  exported from `asas_agent` lazily, like the eval models.

## 0.2.0

A new package with an old name. `asas-agent` 0.1.0 lived in its own repository
(`ad-govtech/asas-agent`): a Postgres registry of agent definitions, Langfuse
prompts and a stateless `AgentRuntime.run` on SDK 0.8. This release shares
nothing with it. **There is no upgrade path from 0.1.0**: a host on 0.1.0 stays
on that tag or moves to this API.

The runtime is extracted from a procurement application's agent runtime in
steps; this release carries the pieces that are pure.

- **Releases** (`build_release`, `load_release`, `read_release`, `release_id`):
  an agent's prompt, extra prompt files, enabled skills and budgets captured
  under the SHA-256 of `json.dumps(release, sort_keys=True)`. The agent's folder,
  the extra files and the environments that build from files are arguments, so
  the source application's release ids are byte-identical after the move.
  Elsewhere the published release under `releases/active-<environment>.json` is
  read and refused unless it still hashes to its id and names the model.
- **Skills** (`parse_skill`, `load_skills`): the SKILL.md format, a YAML header
  (`name`, `enabled`, `applies: always`, `stage`, `roles`, `tools`, and any
  field the host adds) and the playbook. `stage_view` and `stage_tools` give a
  stage's run only its skills; the default stage is an argument.
- **Run contracts** (`RunInput`, `ToolCall`, `ModelCall`, `Ask`, `Delivery`,
  `TurnResult`, `Failure`): the payloads a durable host replays, with their
  fields and defaults pinned by tests. `RunInput.stage` is a string; a host
  narrows it by subclassing. `RunInput.need_id` keeps its first host's name so
  running workflows replay; a rename is a later, breaking release.
- **Evals** (`parse_scenario`, `run_suite`, `summarize_repetitions`,
  `assert_expected_record`, `ProcedureFailure`): scenario suites with hashed
  contracts, four attempts each, pass^4 and prefix pass^4 counted apart.
  `MeasuredModel` and `GatewayModels` (in `asas_agent.models`, loaded on first
  use) measure every gateway call, wait out throttling and count the retries;
  the settings a gateway is read from are an argument.
- **`asas-agent eval <suite> --attempt module:function`** runs a suite through
  the host's attempt and writes a report naming the provider, the model and
  `reasoning_quality_measured`. It never overwrites a report.
- No Temporal dependency, not even an extra. Table-less and router-less in this
  release; the run store and its tables arrive in a later one.
- Depends on `openai-agents>=0.22,<0.23`, `pydantic>=2.11,<3` and `pyyaml>=6.0`.
