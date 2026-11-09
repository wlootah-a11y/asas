# asas-agent

The framework that runs agents, on the OpenAI Agents SDK. One way to release an
agent, give it skills, hand runs to a durable host and evaluate it, shared by
every application that runs agents.

## Principles

- **Agents are hosted, not hosting.** The package depends on no workflow engine.
  An application that keeps runs in Temporal (or anything else) plugs into its
  seams; the workflows, Activities, payload codec and worker stay in the
  application. Every module is safe to import inside a workflow sandbox: no I/O,
  randomness or global state at import, and `import asas_agent` loads neither the
  SDK nor an OpenAI client.
- **A release is its content.** An agent's prompt, skills and budgets are captured
  whole under the SHA-256 of their content. A run pins its release and never
  rereads the files; publishing is a deployment step, never done at start-up.
- **Skills are playbooks.** One SKILL.md each, a YAML header and the words. The
  agent activates a skill; a skill that applies always joins every run's words.
- **Contracts replay.** The run payloads keep their fields and defaults; a new
  field comes with a default, and nothing is renamed without a minor release.
- **Evals count what was observed.** A scenario passes only when four attempts of
  four pass, a step the host cannot run never counts as a pass, and every report
  states the provider, the model and whether reasoning quality was measured.

## Five minutes

```python
from pathlib import Path
import asas_agent

release = asas_agent.load_release("my-model", Path("agents"), agent="assistant")
release["id"]  # pin this on the run
asas_agent.stage_tools(release, "intake")  # the tools an intake run is offered
```

A run: the host dispatches the tools, answers the agent's questions and renders its
record (`asas_agent.ContextRenderer`); the package runs the skills.

```python
result = await asas_agent.run_agent(
    {"release": release, "tools": tools, "history": history},
    dispatch,  # (call_id, name, arguments, active_skill) -> the tool's result
    ask,  # (call_id, arguments) -> the person's answer, or raise AskPending
    renderer=my_renderer,
    provider=my_provider,
)
result["answer"], result["tool_trace"], result["history"]
```

```bash
asas-agent eval evals/assistant --attempt my_app.evals:attempt --output report.json
```

Pin it like every Asas package, by tag or by commit:

```
asas-agent @ git+https://github.com/wlootah-a11y/asas.git@asas-agent/v0.3.0#subdirectory=packages/asas-agent
```

The package shape and the seams: [`DESIGN.md`](DESIGN.md). What changed:
[`CHANGELOG.md`](CHANGELOG.md).

## Running on a gateway, tracing in Langfuse

The eval models measure; a product run needs a provider with its own timeout
and retries, and its traces beside the rest of the product's in Langfuse.

```python
from agents import Runner, RunConfig, set_trace_processors
from asas_agent import gateway_provider, LangfuseTracingProcessor

provider = gateway_provider(api_key=cfg.LLM_KEY, base_url=cfg.LLM_URL,
                            timeout=120, max_retries=2,
                            use_responses=False)        # a chat-completions-only gateway
set_trace_processors([LangfuseTracingProcessor(langfuse_client, mask=mask)])

result = await Runner.run(agent, "...", run_config=RunConfig(model_provider=provider))
```

`azure_provider(api_key=, endpoint=, api_version=)` is the Azure OpenAI
counterpart, where the model a run names is the deployment. The tracing
processor writes the run as one Langfuse trace in the group id's session, a
generation per model call with model, messages and usage, a span per tool
call with its arguments and result, and never fails the run when Langfuse is
down. Both are loaded on first use, like the eval models.

Three things a host that must keep data in-country has to get right:

- **`set_trace_processors`, not `add_trace_processor`.** `add_trace_processor`
  keeps the SDK's default exporter beside Langfuse, and that exporter sends
  every trace to api.openai.com whenever an `OPENAI_API_KEY` is set.
  `set_trace_processors([...])` replaces it, so Langfuse is the only
  destination. Do not reach for `set_tracing_disabled(True)` to stop the
  exporter: it stops every processor, Langfuse included.
- **`mask` is required for residency** unless Langfuse itself runs in-country.
  `mask=None` sends every input, output and tool payload by value. A mask that
  raises sends `"[mask failed]"` for that field, never the raw value, and logs
  the failure without the content.
- **Always pass `base_url`.** `gateway_provider`'s default `base_url=None`
  means api.openai.com, so a host on a gateway must name the gateway's URL.

## Tracing attributes and the prompt link

A run can tell Langfuse who it is for, how to find it and which prompt version
served it:

```python
from asas_agent import TraceAttributes, run_agent

await run_agent(snapshot, dispatch, ask, renderer=renderer, provider=provider,
                group_id=session_id,                    # the Langfuse session
                trace=TraceAttributes(user_id="u-17", tags=("screening", "qa"),
                                      version="3", release="2026.11.0",
                                      metadata={"tenant": "t-1"}))
```

`group_id` and `trace_metadata` keep working and win over `trace` when both name
the same thing: `group_id` over `session_id`, a `trace_metadata` key over the same
key in `metadata`. A tag holds no comma.

**A host that opens its own trace.** `with agents.tracing.trace(name, trace_id=...)`
around `run_agent` makes the run join that trace, so `run_agent` cannot set its
attributes. The processor reads them from the trace's metadata under these keys,
removes them from the metadata it forwards, and sends each as the Langfuse trace
field of the same name:

| Metadata key | Langfuse trace field | Value |
| --- | --- | --- |
| `langfuse.user_id` | `userId` | string |
| `langfuse.tags` | `tags` | tags joined with commas (`"a,b"`) |
| `langfuse.version` | `version` | string |
| `langfuse.release` | `release` | string (overrides the client's own) |

The session is the SDK trace's `group_id`. `run_agent(trace=...)` writes these same
keys, so the two routes cannot differ.

**The prompt link.** `LangfuseStore.read` keeps where the release came from in
`release["source"]` (`name`, `version`, `label`), outside the content hash, so the
id is unchanged. `run_agent` links each generation of that run to the prompt version
(`generation(prompt=...)` on the 2.x client), so Langfuse shows the version and its
metrics. This works with or without a trace the host opened. A release built from
files has no `source` and no link.

**The model.** The Responses API's span exports no model, so Langfuse showed
`model=None`. A generation now gets the response's model, else the name the run
asked for. A span that names its model keeps it.

`processor.shutdown()` and `force_flush()` flush the client and never raise;
`shutdown()` does it once and leaves the client open.

## Langfuse over OpenTelemetry (SDK v3 and later)

A Langfuse v4 server in its events-only ingestion mode refuses the 2.x client's
`trace-create` and `generation-create` events (HTTP 207, which the 2.x client drops
silently), so on such a server `LangfuseTracingProcessor` sends nothing that is
kept. Install the `langfuse-otel` extra instead of `langfuse` and use
`LangfuseOtelTracingProcessor`; the mapping, the reserved keys, `mask`, the prompt
link and the model fill are the same, and `LangfuseStore` takes either client:

```python
from langfuse import Langfuse
from agents import set_trace_processors
from asas_agent import LangfuseOtelTracingProcessor, LangfuseStore

client = Langfuse(public_key=..., secret_key=..., base_url=..., environment="dev")
set_trace_processors([LangfuseOtelTracingProcessor(client, mask=mask)])
store = LangfuseStore(client, prefix="agent-")
```

What differs on the wire:

- every observation carries the trace's user (`user.id`), session (`session.id`),
  tags, version, name and metadata (`langfuse.trace.metadata.<key>`), set through
  `langfuse.propagate_attributes` in a context of the trace's own, so a span of the
  host's (a web request's) receives none of them;
- the release has no per-trace field in v3+ (the client sets it): the
  `langfuse.release` key travels as the metadata key `asas_release`;
- the root observation is the trace, named by the workflow name; agent, function
  and guardrail spans are Langfuse `agent`, `tool` and `guardrail` observations;
- observations open when the SDK span starts and close when it ends, so both
  timestamps are real; a call that never ends exports nothing;
- every metadata value is a string; Langfuse drops a value over 200 characters.

Choose one extra: the 2.x and v3+ clients cannot be installed together.

## The agent as configuration

A release is the whole agent: prompt, skills, model, budgets and `settings` in
`agent.yaml` (the SDK's model settings and tracing). It lives in a store and a
label says which one runs, so an agent changes without a deployment:

```yaml
# agents/jd-builder/agent.yaml
name: JD builder
max_turns: 40
max_tools: 30
settings:
  model: {temperature: 0.3, reasoning: {effort: low}}
  tracing: {enabled: true, sensitive: false}
```

```python
from asas_agent import FileStore, LangfuseStore, fetch_release

store = LangfuseStore(langfuse_client, prefix="agent-")   # or FileStore(Path("releases"))
release = await fetch_release(store, "jd-builder", "production", model="gpt-5")
```

```bash
asas-agent publish agents --agent jd-builder --model gpt-5 --store langfuse --label qa
asas-agent promote --agent jd-builder --id <release id> --label production --store langfuse
```

`fetch_release` verifies whatever the store returns, so an edit made in Langfuse's
editor runs under the id of what it says. Who may move `production` is the store's
to enforce. A host with neither Langfuse nor files writes its own `ReleaseStore`:
three async methods. Tracing on in a release sends traces wherever the host's
`set_trace_processors` says, and to api.openai.com if the host never set it.

## A typed answer, behind the host's guard

A run that must end in data rather than words names its type, and a host that
screens every model call wraps the model it runs on:

```python
from asas_agent import GuardedModel, run_agent

model = GuardedModel(
    provider.get_model("gpt-5"),
    inspect_prompt=check_prompt,      # async (texts, system) -> None, raise to refuse
    inspect_response=check_response,  # async (text, system) -> None, raise to refuse
)
result = await run_agent(snapshot, dispatch, ask, renderer=renderer,
                         run_model=model, output_type=Verdict)
result["output"]  # a Verdict, validated against its strict schema
```

The inspectors are the host's: the package holds no rules, and the exception an
inspector raises ends the run unchanged. `GuardedModel` does not stream.

## Developing

`pip install -e '.[dev]'`, then `pytest -q`. No database, no network, no model.
