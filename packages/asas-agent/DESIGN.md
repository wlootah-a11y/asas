# asas-agent design

## Shape

```
asas_agent/
  contracts.py   run payloads (pydantic): RunInput, ToolCall, ModelCall, Ask,
                 Delivery, TurnResult, Failure
  skills.py      the SKILL.md format: parse_skill, load_skills
  releases.py    build_release, load_release, read_release, stage_view, stage_tools,
                 release_from, fetch_release, FileStore
  langfuse_store.py  LangfuseStore: releases as Langfuse prompts (the langfuse extra;
                 loaded on first use)
  evaluation.py  scenario suites: parse_scenario, run_suite, summarize_repetitions,
                 assert_expected_record
  models.py      MeasuredModel, GatewayModels (imports the SDK; loaded on first use)
  providers.py   gateway_provider, azure_provider: a product run's ModelProvider
                 (imports the SDK and the OpenAI client; loaded on first use)
  guard.py       GuardedModel: the host's prompt and response inspection around
                 every model call (imports the SDK; loaded on first use)
  tracing.py     LangfuseTracingProcessor: the SDK's run traces in Langfuse 2.x
                 (imports the SDK; loaded on first use)
  seams.py       the Protocols a host fills: ToolDispatcher, AskHandler, SkillAware,
                 ContextRenderer, ReleaseStore
  loop.py        run_agent, instructions_for, described_tools, model_settings
                 (imports the SDK; loaded on first use)
  cli.py         asas-agent eval
```

Hosts import from `asas_agent` only; the submodules may move between releases.

## Hosted, not hosting

A host runs each agent run somewhere durable. The first host keeps one Temporal
child workflow per run, makes every model and tool call an Activity and replays
the payloads above. None of that is here: the package never imports a workflow
engine, and a run's moving parts reach the host through seams the host fills.

| Seam | What the host supplies | Arrives |
|---|---|---|
| model provider | the SDK `Model` or `ModelProvider` a run calls (a gateway, a model called by reference from a workflow and told the active skill through `SkillAware`, a scripted model in tests) | 0.3.0 |
| tool dispatcher | `ToolDispatcher`: the call id, tool name, arguments and active skill in, the tool's result out, under the host's authorisation and gates | 0.3.0 |
| ask handler | `AskHandler`: records the agent's question and returns when the person answers, or raises `AskPending` | 0.3.0 |
| context renderer | `ContextRenderer`: the record the agent reads first and finds in tool results, the tools every skill holds, the ask tool, and the host's words before and after the skills | 0.3.0 |
| release store | `ReleaseStore`: where releases live and which one a label points at (`FileStore`, `LangfuseStore`, or the host's database), read outside the sandbox | 0.4.0 |
| run store | the run's transcript, model-call cache, tool effects, inbox and outbox | with the store and its tables |
| clock | the time the run sees | with the store |

Rules for code here, so a host can call it from a workflow sandbox:

- No I/O, randomness, environment reads or global state at import.
- No reads of the clock or the environment inside a function a workflow calls;
  the host passes what it recorded (`RunInput` carries its deadlines for that
  reason).
- `import asas_agent` does not import the SDK or the OpenAI client.

## The loop

`run_agent` renders the instructions as the release's prompt, the renderer's
`head`, the skill catalogue, the active skill's playbook and clauses, then the
renderer's `tail`, and opens the input with the renderer's `opening` before the
history. Only `activate_skill` changes the instructions, so the provider's prompt
cache holds them between writes; the record rides in the input. The tool calls of
one model answer start in the order the model issued them. A tool whose
descriptor says `"read_only": true` runs beside the calls around it; any other
tool, `activate_skill` and the ask tool included, waits for every call before it
and holds back every call after it, so a write never races a read of the same
record. The trace and the results the model reads keep the call order, so a
replay sees the same sequence.
An `AgentsException` from a seam ends the run; any other exception from the
dispatcher is a typed refusal the model reads.

## Releases

The id is the SHA-256 of the release without its id, as
`json.dumps(release, sort_keys=True)` writes it. The keys are `name`, `model`,
`instructions`, `skills`, `max_turns`, `max_tools`, and `settings` only when the
agent names any (0.4.0), so a release without settings keeps the id it had. A new
required key changes every host's ids, so it is a breaking release, never a patch.

A store moves the bytes; `release_from` decides what they are. Content that claims
an id must hash to it. Content that claims none, an edit made in a store's own
editor, is given the id of what it says, so the run records what really ran and a
change of model or of one word is a new id.

## Langfuse SDK v5

`LangfuseTracingProcessor` writes through the Langfuse 2.x client (`trace`,
`generation`, `span`), the one the self-hosted servers hosts run today accept. The
v5 SDK is OpenTelemetry-native: a v5 processor would sit beside this one, not
replace it, as another `TracingProcessor` (`LangfuseOtelTracingProcessor`, in
its own extra) that opens OTel spans with the same mapping and the same reserved
metadata keys, writes them as the OTel span attributes v5 defines for user, tags,
version, release, prompt name and version and model (checked against v5 when it is
built, not now), and takes `trace_id` from the SDK trace
id (32 hex, as now). `TraceAttributes`, `release["source"]` and the run's context
variable are SDK-free for that reason: a host chooses one processor and everything
`run_agent` sends stays the same. It waits until the servers hosts run accept v5.

## Tables

None yet. When the run store moves here it owns its tables and its Alembic chain,
runs in the host's database, protects each tenant table with asas-tenancy's row
level security in the revision that creates it, and keeps the table names its
first host already has, so adopting it moves no data.
