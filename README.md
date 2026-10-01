# tether

> A reusable Python substrate for building autonomous data-gathering and data-integration agents — a single-agent-loop ("coding agent") harness on the [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (MAF).

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![Built on Microsoft Agent Framework](https://img.shields.io/badge/Built%20on-Microsoft%20Agent%20Framework-512BD4)

You give it a task and a set of tools (or MCP servers). It fetches data from links and tools, keeps large datasets **out of the model's context** as typed *handles*, and uses a **sandboxed Python environment** to write and run code that analyzes that data — returning a trustworthy answer plus a re-openable audit trail of every script and artifact.

This is **v1: the general substrate**. Specific problems plug in later as user-supplied tools / MCP servers.

## Key features

- **References, not payloads.** Large or structured returns from tools, MCP servers, and URLs spill to typed **handles** (JSON / text / Parquet) *before* they reach the model — ~200 tokens stand in for megabytes.
- **A computer for the agent.** A sandboxed `run_python` speaks the same handle store (`load(id)` / `save(id, obj)`), so the agent writes and runs code to do the real work; only what it deliberately returns flows back into context.
- **MAF-native.** Builds a standard MAF agent (`create_harness_agent`) you can run, stream, compose into workflows, or use as a sub-agent — with any MAF chat client (OpenAI by default; Azure, Foundry, or your own).
- **One-shot and continuous.** Ephemeral, self-cleaning `solve()` for one-shots; a persistent multi-turn `Conversation` (workspace + history) with an optional idle TTL for continuous use.
- **Tools and MCP, auto-handled.** Plain Python callables and MCP servers (stdio / HTTP / WebSocket) drop into one `tools=[…]` list; spill handling and server lifecycle are managed for you.
- **Live status.** Built-in, developer (`report_progress`), and MCP-server progress all stream through one `on_status` feed, `--verbose`, or AG-UI events.
- **AG-UI / CopilotKit.** Stream answer text, tool-call visibility, and status as AG-UI events; frontend tools, shared state, human-in-the-loop, and multi-turn history all work.
- **Confined by construction.** One session root holds everything; every model-supplied path passes through a single `safe_path` chokepoint, and a hardened container sandbox tier is available for real isolation.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
  - [Installation](#installation)
  - [API keys](#api-keys)
  - [Quickstart: CLI](#quickstart-cli)
  - [Quickstart: library](#quickstart-library)
- [Examples and samples](#examples-and-samples)
- [Guides](#guides)
  - [Use it as a MAF agent](#use-it-as-a-maf-agent)
  - [Bring your own model client](#bring-your-own-model-client)
  - [Sessions: one-shot vs continuous](#sessions-one-shot-vs-continuous)
  - [Exchanging files with the user](#exchanging-files-with-the-user)
  - [MCP servers](#mcp-servers)
  - [Live status updates](#live-status-updates)
  - [AG-UI and CopilotKit](#ag-ui-and-copilotkit)
- [Reference](#reference)
  - [Tool surface](#tool-surface)
  - [Security and confinement](#security-and-confinement)
  - [Sandbox tiers](#sandbox-tiers)
  - [Configuration](#configuration)
- [Development](#development)
- [Project layout](#project-layout)
- [Roadmap](#roadmap)

## How it works

The central design move is keeping large data out of context. When a tool, MCP server, or URL returns something big or structured, a **"spill" middleware** intercepts it *before it reaches the model* and writes it to disk as a typed **handle** (JSON, text, or a dataframe stored as Parquet). The model only ever sees a compact summary:

```json
{ "handle": "h1", "kind": "dataframe", "source": "query_sales(region='EU')",
  "schema": {"date": "date", "units": "int", "revenue": "float"},
  "n_rows": 480000, "n_cols": 3, "bytes": 18402211,
  "path": "handles/h1.parquet",
  "preview": "date,units,revenue\n2025-01-01,12,840.0\n... (5 of 480000 rows)" }
```

~200 tokens stand in for 18 MB. To actually work with the data, the agent is given a **computer**: it writes `.py` scripts and runs them in a sandbox that speaks the same handle store (`load(id)` / `save(id, obj)`). Only the small value it deliberately prints/returns flows back into context; derived data becomes new handles. The loop follows a **search → read → analyze** triad: `search` to locate, `read_file` to load the right slice, `run_python` to analyze.

## Getting started

### Installation

Requires **Python 3.12** and [`uv`](https://docs.astral.sh/uv/). `agent-framework-core` is a prerelease, so allow prereleases when syncing:

```bash
uv sync --prerelease=allow
```

Optional extras:

```bash
uv sync --prerelease=allow --extra docling   # document ingestion (read_document)
uv sync --prerelease=allow --extra agui       # AG-UI / CopilotKit streaming
```

Document ingestion uses [Docling](https://github.com/DS4SD/docling), which downloads its models on first use. To pay that cost up front (e.g. at deploy time) instead of on the first `read_document` call, prefetch them:

```bash
tether-prefetch-docling          # add --ocr to also fetch OCR models (scanned documents)
```

OCR is **off by default** — born-digital PDFs/Office files get their tables and structure without it. Set `TetherConfig.documents.ocr = True` for scanned/image documents.

### API keys

Create a `.env` in the project root:

```bash
OPENAI_API_KEY=sk-...        # required by the default OpenAI client (see "Bring your own model client" to use another provider)
TAVILY_API_KEY=tvly-...      # optional — enables web_search / web_extract
```

### Quickstart: CLI

```bash
uv run tether "What is the total EU revenue in 2025, excluding invalid rows?"
```

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `gpt-5-mini` | Model for the built-in OpenAI client (reads `OPENAI_API_KEY`, or `AZURE_OPENAI_*` for Azure routing). The CLI uses that client; for other providers drive the tether from Python with your own client — see [Bring your own model client](#bring-your-own-model-client) |
| `--root` | a fresh session dir | Workspace root (the confinement boundary) |
| `-v`, `--verbose` | off | Print live tool status to stderr as the task runs (see [Live status updates](#live-status-updates)) |

The CLI prints the answer and leaves `[session: …]` — the directory holding every script, handle, and artifact for inspection.

### Quickstart: library

```python
from tether import Tether, TetherConfig, solve

# One-shot convenience
result = solve("Summarize the latest figures from https://example.com/report.pdf")
print(result.final_text)

# Reusable tether with your own tools
def query_sales(region: str) -> list[dict]:
    """Return sales rows for a region."""
    ...

# With no client passed, the tether builds the default OpenAI client (reads OPENAI_API_KEY).
# `model` configures ONLY that default client — to use Azure / Foundry / any other provider,
# pass your own MAF chat client (see "Bring your own model client"); `model` is then ignored.
h = Tether(TetherConfig(model="gpt-5-mini"))
result = h.solve(
    "Total EU revenue in 2025, excluding invalid rows?",
    tools=[query_sales],          # plain Python callables; auto-wrapped + spill-handled
)

result.final_text   # the answer
result.handles      # dict of handle summaries produced during the run
result.files        # user-meaningful files written under the session root
result.session_dir  # full audit trail (scripts, handles, transcript)
result.error        # None, or a string if the run failed (e.g. context overflow)
```

Your tools are wrapped as MAF agent tools automatically, and their returns pass through the same spill middleware — so tool-produced and code-produced data are identical kinds of handle.

## Examples and samples

The quickstarts above and the guides below are each worked examples. Two runnable samples ship in the repo:

| Sample | Shows |
|---|---|
| [`examples/copilotkit/`](examples/copilotkit/) | **Full end-to-end app:** a CopilotKit chat UI + a FastAPI AG-UI backend with an **MCP server** wired in — ask a data question and watch the agent call the MCP tool, spill the result to a handle, and run sandboxed Python to answer |
| [`examples/agui_server.py`](examples/agui_server.py) | The minimal AG-UI backend: a FastAPI endpoint that streams a tether run as SSE |
| [Use it as a MAF agent](#use-it-as-a-maf-agent) | Building and driving the agent with the ordinary MAF agent surface (`run` / streaming / threads / workflows) |
| [Bring your own model client](#bring-your-own-model-client) | Injecting an Azure / Foundry / custom-auth chat client |
| [Sessions: one-shot vs continuous](#sessions-one-shot-vs-continuous) | Persistent multi-turn conversations with a shared workspace |
| [Exchanging files with the user](#exchanging-files-with-the-user) | Delivering output files to a host and ingesting uploads |
| [MCP servers](#mcp-servers) | Plugging in stdio / remote MCP servers |
| [Live status updates](#live-status-updates) | Streaming tool progress to a callback or the CLI |

## Guides

### Use it as a MAF agent

`solve()` and `Conversation` are conveniences over a plain Microsoft Agent Framework agent. When you want the agent **itself** — to drive it directly, stream it, give it a multi-turn thread, or drop it into a MAF workflow as a node/sub-agent — build it with `Session.create_agent()` and then use the ordinary MAF agent surface:

```python
import asyncio
from agent_framework.openai import OpenAIChatClient
from tether import Session, TetherConfig

async def main():
    # The two-step (open a workspace, then build the agent over it) is the only tether-specific
    # part — it binds the agent to one session root + sandbox. The build call itself is standard MAF.
    async with Session.create(TetherConfig()) as session:
        agent = await session.create_agent(
            OpenAIChatClient(model="gpt-5-mini", env_file_path=".env"),
            agent_instructions="Reconcile EU sales and flag anomalies.",  # your task prompt
            tools=[query_sales, mcp],          # plain callables + MCPTools, spill-handled for you
            bundles=("code", "files", "web"),  # which built-in tool families to include
        )

        # `agent` is a MAF `Agent` — the usual interface, nothing tether-specific from here:
        reply = await agent.run("Which regions look anomalous?")
        print(reply.text)

        # Multi-turn over a MAF AgentSession (history threads across calls):
        thread = agent.create_session()
        await agent.run("Now compare against last quarter.", session=thread)

        # Streaming:
        async for update in agent.run("Summarize the findings.", stream=True):
            print(update.text, end="")

asyncio.run(main())
```

Because the returned object is a standard MAF `Agent`, it composes anywhere a MAF agent is expected — as a workflow node, a sub-agent, or behind the AG-UI wrapper (`agui_stream` does exactly this with `conv.agent`). Construction goes through MAF's own `create_harness_agent(client, …)` factory, so apart from the workspace two-step and the spill-wrapping of your tools, you are building and running an ordinary MAF agent.

### Bring your own model client

The convenience entry points (`solve`, `Tether(...)` with no `client`) use MAF's OpenAI client by default — following [MAF's own Python convention](https://github.com/microsoft/agent-framework/blob/main/python/README.md) (`OPENAI_API_KEY` present → OpenAI). The tether is **not** OpenAI-only: it needs only an object implementing MAF's chat-client protocol (`SupportsChatGetResponse`), and never authenticates one for you when you supply it. The same client is reused across one-shots and every continuous conversation. Two injection points:

```python
# High-level facade — your client flows to solve / aopen / agui_stream
h = Tether(TetherConfig(), client=my_client)

# Composable path — pass it straight to create_agent
agent = await session.create_agent(my_client, agent_instructions="…", tools=[…])
```

MAF's `OpenAIChatClient` already does Azure routing. For Azure with a **refreshing token provider**:

```python
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from agent_framework.openai import OpenAIChatClient
from tether import Tether, TetherConfig

token_provider = get_bearer_token_provider(      # fresh bearer token per call → handles refresh
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")

client = OpenAIChatClient(
    azure_endpoint="https://my-resource.openai.azure.com",
    api_version="2024-10-21",
    credential=token_provider,
    model="my-gpt-5-deployment",                 # Azure deployment name
)
h = Tether(TetherConfig(), client=client)      # config.model is ignored; the client carries it
```

If you already hold a fully-built MAF chat client (your own custom-auth Azure / Foundry / OpenAI-compatible client), skip all of the above and just pass it: `Tether(TetherConfig(), client=that)`.

### Sessions: one-shot vs continuous

The tether serves both one-shot and continuous (multi-turn) interactions from any frontend.

- **One-shot** (`solve`) is ephemeral — it runs and **cleans up its workspace** when done. Pass `keep=True` to retain the audit trail (`result.session_dir`).
- **Continuous** keeps a persistent workspace (handles + sandbox files) and conversation history across turns, until you close it (or an optional idle TTL expires).

```python
import asyncio
from tether import Tether, TetherConfig

async def main():
    h = Tether(TetherConfig(idle_ttl_s=1800))     # idle conversations expire after 30 min (optional)
    conv = await h.aopen("thread-42")                # open or resume by id (e.g. an AG-UI threadId)
    print((await conv.aask("load sales.csv and summarize")).final_text)
    print((await conv.aask("now filter to EU")).final_text)   # sees the prior turn's handles + history
    await conv.aclose()                              # reap this conversation's workspace
    await h.aclose_sessions()                        # (host shutdown) close any remaining
    # await h.sweep_sessions()  # reap idle conversations on your own cadence

asyncio.run(main())
```

**Agent instructions.** Give the agent your domain system prompt with `agent_instructions` — on `Tether(...)` as the default for every conversation, or per conversation on `aopen(...)` (which wins when both are set); `solve`/`asolve` accept it too. MAF layers it *after* tether's operating manual: `<core + bundle instructions>`, a blank line, then `<agent_instructions>`. Leave tool mechanics to tether and put the data model, workflow, and conventions in yours. The per-conversation value applies when a conversation is created; re-opening a live id returns the existing agent.

```python
h = Tether(cfg, agent_instructions=SYSTEM_PROMPT)
conv = await h.aopen("thread-42", agent_instructions=OTHER_PROMPT)   # optional override
```

AG-UI hosts get this automatically — `agui_stream` maps each request's `threadId` to a persistent conversation, so handles and files persist across turns (history stays in the AG-UI message replay).

### Exchanging files with the user

**Delivering files.** Pass `on_publish` (on `Tether(...)`, or per conversation on `aopen`) and
select the `deliver` bundle. The model writes a file under `outputs/` and calls `publish_file`
(or `publish()` inside `run_python`, or `publish_handle` for a handle). Tether validates every
request in the parent — inside the root, a regular file and not a symlink, not under
`handles/` or `.scripts/` or a control file, at most `max_publish_bytes` — then calls your
callback with a `PublishedFile(path, rel_path, name, size, sha256, content_type, description,
source)`. `path` is a private snapshot outside the workspace, copied from the validated bytes;
copy it during the callback — it is deleted when the callback returns. Its return
value reaches the model under `"host"`; if it raises, the tool returns an error and the run
continues. The callback runs synchronously in the tool call's context (so your contextvars are
visible) but possibly on a worker thread — make it thread-safe. Sandbox `publish()` requests
are processed after the script exits cleanly.

```python
from tether import PublishedFile, Tether

def host_publish(pf: PublishedFile) -> dict:
    file_id = storage.copy_in(pf.path, name=pf.name, run=CURRENT_RUN.get())
    return {"file_id": file_id, "label": pf.name}

h = Tether(cfg, bundles=("code", "files", "deliver"), on_publish=host_publish)
```

**Receiving files.** Between turns, hand uploads to the conversation:

```python
handle = conv.add_input(upload_bytes, input_id=upload.id, name=upload.filename)
# or: await conv.aadd_input(...)   # waits for a running turn instead of raising
```

The file lands at `inputs/<input_id>/<name>` (read-only; mounted `:ro` in the container tier)
and becomes a `binary` handle — `load(id)` in `run_python` returns its path. It is idempotent by
`input_id`, survives a restart on the same root, is capped by `max_input_bytes`, and is
**never parsed in the host process** (the preview is name/size/type plus raw first lines for
text files). `conv.inputs` lists them. Note: `read_document` on an input parses it with Docling
**in the host process**; prefer reading uploads inside `run_python` when isolation matters.

### MCP servers

Pass a MAF `MCPTool` — stdio, Streamable-HTTP, or WebSocket — in the same `tools=[…]` list. The tether connects each server, **owns its lifecycle** (closing it when the run ends, even on error), and attaches the same spill handling to every tool the server exposes — so a large MCP return (a long message list, a big query result) lands as a clean typed handle instead of flooding context.

```python
from agent_framework import MCPStdioTool
from tether import Tether, TetherConfig

# A stdio MCP server, launched however your other MCP clients launch it.
# (If the server resolves its config/credentials relative to a directory, launch it
#  from there, e.g. command="sh", args=["-c", "cd /path/to/server && exec uv run its-cmd"].)
mcp = MCPStdioTool(name="msgraph", command="uv", args=["run", "msgraph-mcp"])

h = Tether(TetherConfig(model="gpt-5-mini"))
result = h.solve("List the subjects of my 5 most recent emails.", tools=[mcp])
print(result.final_text)
```

A remote server works the same way:

```python
from agent_framework import MCPStreamableHTTPTool

mcp = MCPStreamableHTTPTool(
    name="my-service",
    url="https://example.com/mcp",
    header_provider=lambda _: {"Authorization": f"Bearer {token}"},  # if it needs auth
)
result = h.solve("…", tools=[mcp])
```

MCP support needs the `mcp` SDK, which is a declared dependency, so `uv sync` already installs it.

**On result size.** Spilling is lossless — a large page is preserved whole as a handle, so an MCP server's pagination cursor survives and the agent can fetch the next page. The two ends of the spill-over zone are configurable (`spill_threshold_bytes` … `max_spill_bytes`): a well-behaved server that paginates returns bounded pages that flow through cleanly, while an unbounded dump past `max_spill_bytes` raises `SpillLimitExceeded` rather than silently filling disk — nudging the source toward server-side pagination/filtering.

### Live status updates

Tools report progress while they run, and you receive it through an `on_status` callback — from the built-in tools, from your own tools (via `report_progress`), and from MCP servers (their logging and progress notifications are captured automatically).

```python
from tether import Tether, report_progress

def crunch(n: int) -> str:
    """Your tool can report progress."""
    for i in range(n):
        report_progress(f"processed {i + 1}/{n}", current=i + 1, total=n, tool="crunch")
    return "done"

def show(event):
    bar = f" [{event.current}/{event.total}]" if event.current is not None else ""
    print(f"→ {event.tool}: {event.message}{bar}")

Tether(tools=[crunch], on_status=show).solve("crunch 5 items")
```

The CLI exposes the same feed with `-v`/`--verbose` (printed to stderr). MCP-server status needs no extra wiring — a server's `notifications/message` arrive tagged `mcp:<server>`, and its `notifications/progress` arrive tagged with the calling tool's name plus `current`/`total`.

### AG-UI and CopilotKit

A tether run can drive an [AG-UI](https://docs.ag-ui.com/) client such as [CopilotKit](https://docs.copilotkit.ai/) — streamed answer text, live tool-call visibility, and the status feed above — all as AG-UI events. Install the optional extra:

```bash
uv sync --prerelease=allow --extra agui
```

`Tether.agui_stream(input_data)` yields AG-UI events for one request; encode them as SSE from your endpoint:

```python
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from tether import Tether

app, tether = FastAPI(), Tether()

@app.post("/agent")
async def agent(request: Request):
    input_data = await request.json()                 # AG-UI RunAgentInput
    encoder = EventEncoder()
    async def sse():
        async for event in tether.agui_stream(input_data):
            yield encoder.encode(event)
    return StreamingResponse(sse(), media_type="text/event-stream")
```

Point your AG-UI client at this endpoint. The tether reuses the official `agent-framework-ag-ui` converter, so **frontend/generative-UI tools** (defined in the request), **shared state** (`state_schema`/`predict_state_config`, forwarded as keyword args to `agui_stream`), **human-in-the-loop**, and **multi-turn history** all work — with the tether's own progress feed overlaid as `tether.status` `CUSTOM` events. A runnable version is in [`examples/agui_server.py`](examples/agui_server.py).

## Reference

### Tool surface

The agent gets a small, fixed set of root-confined tools (listed below; the web and document tools activate when their key/extra is present). Domain data sources are *your* tools/MCP servers, auto-handled by spill.

| Tool | Purpose |
|---|---|
| `write_file(path, content)` | Write a file (root-jailed) |
| `read_file(path, offset=0, limit=2000)` | Bounded, paginated text |
| `list_files(path=".")` | Directory listing |
| `search(pattern, …)` | ripgrep over files/handles, with a Python-regex fallback |
| `run_python(code=None, path=None, args=[])` | Run a script in the sandbox → `{stdout, result, error, exit_code, new_handles}` |
| `fetch_url(url)` | Fetch a URL → typed handle; HTML is cleaned to Markdown (trafilatura) |
| `inspect_handle(id, …)` | Deeper on-demand look at a handle (fuller schema, more preview, optional stats) |
| `web_search(query, max_results=5)` | Tavily web search (needs `TAVILY_API_KEY`) |
| `web_extract(url)` | Tavily clean-content extraction (needs `TAVILY_API_KEY`) |
| `read_document(source)` | A workspace path or URL → clean markdown handle (tables preserved) via Docling; needs the `docling` extra |
| `publish_file(path, name=None, description=None)` | Deliver a workspace file to the host (`deliver` bundle; only with `on_publish`) |
| `publish_handle(handle_id, format=None, name=None)` | Deliver a handle as csv/xlsx/parquet/json (`deliver` bundle; only with `on_publish`) |

### Security and confinement

Every session has one **root directory**; everything — handles, agent-written scripts, reads/writes, the sandbox `cwd` — lives under it.

- **Layer 1 — Tool path-jail (guaranteed).** All model-supplied paths route through one chokepoint, `safe_path(root, p)`, which resolves symlinks *before* checking and rejects any path outside the root (blocks `..`, absolute paths, symlink escapes). It's the most heavily tested code in the project.
- **Layer 2 — Executed code.** On the default `local` tier, `run_python` runs in a subprocess with `cwd=root`, a scrubbed environment, `resource` rlimits (CPU, memory, file size) and a wall-clock timeout. **The local tier is not a security boundary**: the code runs as the host user and can read host files outside the root and reach the network. Use it for development or fully trusted input only. For real isolation, switch to the `container` tier — see [Sandbox tiers](#sandbox-tiers).

> The **container tier** provides real isolation: set `TetherConfig.sandbox.backend = "container"` to run `run_python` in a hardened Podman/Docker container — network off by default, read-only root filesystem, dropped capabilities, non-root, and memory/cpu/pid limits — behind the same `SandboxExecutor` interface (no other tether code changes).

### Sandbox tiers

`run_python` executes model-authored code; two backends are chosen by `TetherConfig.sandbox.backend`.

- **`local`** (default) — a scrubbed-env subprocess with `resource` rlimits, a wall-clock timeout, and the path-jail. Fast, no dependencies; **not a security boundary** (host-user file and network access). Development / trusted use only.
- **`container`** — runs the code in a hardened OCI container (Podman preferred, Docker supported; auto-detected). Network **off by default**, read-only root filesystem, `--cap-drop ALL`, non-root, and memory/cpu/pid limits. The session root is bind-mounted to `/workspace`, so handles round-trip exactly as in the local tier.

```python
from tether import Tether, TetherConfig
from tether.config import SandboxConfig

cfg = TetherConfig(sandbox=SandboxConfig(
    backend="container",
    network=False,                 # opt-in with True if sandbox code must reach the network
    pip_packages=("rich",),        # provisioned into a mounted layer (network only during provisioning)
))
Tether(cfg).solve("…")
```

**Requiring isolation.** Set `require_isolation=True` to make the boundary mandatory: `backend="local"` is refused with `SandboxRuntimeUnavailable` at `Session.create` (not on first use), and a missing runtime or image raises — tether never falls back to the local tier.

**Image readiness.** By default the image (Python + `preinstalled` libraries) and any `pip_packages` layer are **built on first use** and cached. `build_on_demand` controls this; its default (`None`) means "build unless `require_isolation`". With builds disabled, a missing image makes `run_python` fail immediately with `SandboxImageMissing`, naming the exact command to run. Builds that do run are bounded by `build_timeout_s` (`SandboxImageError` on expiry). Pre-build exactly what a host will ask for, and gate startup on a non-mutating preflight:

```bash
tether-build-sandbox --preinstalled pandas pyarrow numpy httpx --pip rich   # build image + layer
tether-build-sandbox --pip rich --check                                    # report only; exit 1 on problems
```

```python
from tether import sandbox_preflight
from tether.config import SandboxConfig

cfg = SandboxConfig(backend="container", require_isolation=True)   # build_on_demand -> False
report = sandbox_preflight(cfg)        # never builds; `image inspect` is time-bounded
if not report.ok:
    raise RuntimeError("; ".join(report.problems))
```

Notes: on macOS the runtime runs in a Linux VM, so the session root must sit under a VM-shared path (the default `~/.tether/...` is); the container tier does not enforce `max_file_size_mb` (memory/pid/cpu/network are enforced instead).

### Configuration

`TetherConfig` (see `tether/config.py`) is a plain dataclass:

| Field | Default | Notes |
|---|---|---|
| `model` | `"gpt-5-mini"` | Used only by the built-in OpenAI client; ignored when you inject a `client` |
| `spill_threshold_bytes` | `8192` | Lower edge of the spill-over zone: a tool return over this becomes a handle |
| `max_spill_bytes` | `100 MiB` | Upper edge: a return larger than this is **rejected loudly** (`SpillLimitExceeded`), never silently stored |
| `max_context_window_tokens` | `128_000` | Fed to MAF compaction |
| `max_output_tokens` | `4096` | |
| `root_dir` | `None` | `None` → a session dir under `./.tether/sessions/` |
| `idle_ttl_s` | `None` | Continuous-session idle TTL; `None` → never expire |
| `max_publish_bytes` | `100 MiB` | Largest file `publish_file` / `publish()` will deliver |
| `max_input_bytes` | `100 MiB` | Largest upload `add_input` will accept |
| `sandbox` | `SandboxConfig()` | `backend` (local/container), timeout, limits, network, `pip_packages`, preinstalled libs, `require_isolation` (`False`), `build_on_demand` (`None` → build unless isolation is required), `build_timeout_s` (`900`) |
| `fetch` | `FetchConfig()` | `max_bytes`, timeout, allowed URL schemes |
| `search` | `SearchConfig()` | Tavily provider, key, `max_results` |
| `documents` | `DocumentConfig()` | Docling ingestion: `ocr` (off by default) |

## Development

```bash
uv run pytest          # full suite — no network or API keys required
uv run ruff check .
```

Testing is a first-class principle: **tests are written before implementation for every unit** (TDD). The fast suite runs deterministically against a stub chat client (no API cost); live tests are gated behind `TETHER_LIVE=1`. `safe_path` and the spill logic are held to the highest bar.

Design specs and phased implementation plans for each feature live under [`docs/superpowers/`](docs/superpowers/).

## Project layout

```
tether/
  config.py      TetherConfig / SandboxConfig / FetchConfig / SearchConfig
  paths.py       safe_path() — the single security chokepoint
  handles.py     Handle + HandleStore (json / text / dataframe persistence)
  sandbox.py     SandboxExecutor protocol + LocalSubprocessSandbox
  sandbox_container.py  hardened OCI-container backend (podman/docker)
  container_runtime.py  runtime detection, image build, package layer
  session.py     Session — root dir + store + sandbox for one run
  conversation.py  persistent multi-turn Conversation (workspace + MAF AgentSession)
  manager.py       SessionManager: continuous-session registry + lazy TTL
  spill.py       large/structured tool & MCP returns → handles (MAF result_parser)
  agui.py        AG-UI event stream (status overlay over agent-framework-ag-ui)
  bundles.py     capability bundles (code / files / web) + their instructions
  tools/         the agent's tools (files, search, fetch, code, inspect, web)
  runtime/       in-sandbox helpers (load/save/emit)
  api.py         Tether / solve() / Result
  cli.py         thin streaming CLI
docs/superpowers/  design specs + phased implementation plans
examples/          runnable samples (minimal AG-UI server; full CopilotKit + MCP app)
evals/             eval harness
tests/             mirror of the package (unit + integration + security tests)
```

## Roadmap

**Implemented:** foundation (handles, sandbox, path-jail); the agent loop + tool surface; the `Tether`/`solve()` API + CLI; web research (Tavily search/extract, Markdown fetch); **document ingestion** (`read_document` via Docling — PDF/spreadsheet → Markdown with tables); **live status updates** (built-in, developer, and MCP tools → an `on_status` feed / `--verbose`); an **AG-UI / CopilotKit** integration (`Tether.agui_stream`); a **container sandbox tier** (hardened Podman/Docker isolation behind `SandboxExecutor`); and a **session lifecycle** model (ephemeral one-shot `solve`; persistent continuous `aopen`/`aask`/`aclose` with optional idle TTL).

**Planned** (documented under [`docs/superpowers/`](docs/superpowers/)):

- **Micro-VM sandbox tier** — gVisor / Firecracker, behind the same `SandboxExecutor` interface.
- **MAF skills + memory providers** (v1.1) and a MAF Workflow durability / HITL outer shell.

**Out of scope for now:** headless-browser/JS rendering, a full search-provider abstraction, and alternative document backends.
