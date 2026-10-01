# Security Hardening — Making the Trust Boundary Real — Design

- **Date:** 2026-09-17
- **Status:** Approved design (brainstormed).
- **Motivation:** The threat model adopted 2026-06-15 treats **the model itself, and any
  content it pulls from the web or documents, as potentially adversarial** — prompt-injection
  in fetched content escalating to code execution or SSRF is in scope. The code does not yet
  enforce that. Isolation is off by default, the parent trusts metadata written by sandboxed
  code, egress is unfiltered, and live MCP connections are shared across conversations. The
  README advertises a boundary the implementation does not provide. This phase closes that
  gap. It adds no user-facing capability; it makes the existing posture true.

## What is already sound (scope limiter)

Establishing this first keeps the work narrow:

- `safe_path` (`paths.py`) resolves symlinks before checking and rejects absolute paths, `..`,
  and symlink escapes. It remains the single chokepoint and needs no change.
- `HandleStore._register_record` already runs a handle record's `path` through `safe_path`
  (`handles.py:142`), so a record pointing outside the root is **rejected at registration**,
  not read later. The path-escape class is closed; this phase does not revisit it.
- `_conv_root` already rejects separators and `..` in an untrusted conversation id
  (`manager.py:54`) before it is used as a directory that gets `rmtree`'d.
- The container backend is already hardened — network off, read-only root filesystem,
  `--cap-drop ALL`, non-root, memory/cpu/pid limits. The problem is that it is not the
  default, not that it is weak.

## Decisions (resolved in brainstorm)

| Decision | Choice |
|---|---|
| Control plane | **Zero-trust in place.** The parent re-derives all handle metadata from the bytes on disk; it does not relocate the control files. Relocation is rejected for now: the child still writes handle *files* into the mount, so re-derivation is required regardless, and relocation would additionally churn the container mount contract for no further security |
| Handle mutability | **Immutable.** Adopting an id that already exists is rejected, so sandboxed code cannot repoint an existing handle at different bytes |
| Default sandbox backend | **`container`.** A missing runtime **fails loudly** — no silent fallback, since a silent downgrade to a no-isolation backend is exactly the gap being closed. `local` stays available as an explicit opt-out and is documented as providing *no* isolation |
| Egress policy | **Deny private by default, allowlist to opt back in.** Loopback / private / link-local / reserved / multicast / unspecified are denied, with `169.254.169.254` denied explicitly; a `FetchConfig` allowlist re-enables named hosts or CIDRs, because internal data sources are a primary use case for this project |
| Redirects | **Every hop re-validated.** Redirects are followed manually rather than by httpx, so a 302 into an internal address is caught at the hop |
| DNS rebinding | **Accepted residual risk.** Validate-then-connect leaves a small TOCTOU window. Full IP pinning (connect to the validated address with a `Host` override) is deferred — it needs a custom httpx transport and complicates TLS SNI and virtual hosting |
| Per-conversation tools | **Explicit `ToolFactory` wrapper.** A bare zero-arg callable is ambiguous (a plain Python tool *is* a callable), so factories are marked rather than sniffed |

## Architecture

Four independent workstreams, one goal. They touch disjoint files and can be built and
reviewed separately; only the conftest change in workstream 2 is shared.

```
1. handles.py, runtime/tether_sandbox.py, sandbox.py   untrusted control plane
2. config.py, session.py, tests/conftest.py            isolation by default
3. egress.py (new), tools/fetch.py, tools/documents.py filtered egress
4. api.py, manager.py                                  per-conversation resources
```

---

## Workstream 1 — Zero-trust handle metadata

### The problem

`runtime/tether_sandbox.save()` computes a handle's `preview`, `bytes`, `schema`, `n_rows`
and `n_cols` **inside the sandbox** (`runtime/tether_sandbox.py:51-66`) and appends them to
the new-handles file. `_OrchestratedSandbox._ingest_new_handles` passes that record to
`HandleStore.register()` verbatim (`sandbox.py:154`), and the values flow into model context
through `Handle.summary()`. Three concrete consequences:

1. **Metadata can lie.** A record can claim `n_rows: 12` and a benign `preview` for a file
   holding something else entirely. The model's view of the data, and the audit trail, are
   both falsifiable by the code the model wrote.
2. **Preview is uncapped on this path.** `_write_text`/`_write_json` bound previews to
   `_PREVIEW_CHARS` (800), but an adopted record's `preview` is whatever the child wrote — a
   multi-megabyte preview floods the context window.
3. **Records are mutable.** `_register_record` does `self._handles[handle.id] = handle`
   (`handles.py:145`), so sandboxed code can overwrite the record for a handle id it did not
   create, repointing `h1` at different bytes.

### The fix

Invert the trust: the child reports only what it alone knows (which file it wrote, and why);
the parent derives everything that reaches model context.

**Child side** (`runtime/tether_sandbox.py`). `save()` keeps choosing the kind by type and
writing the file, and appends a minimal record:

```json
{"id": "h7", "kind": "dataframe", "path": "handles/h7.parquet", "source": "run_python"}
```

All metadata computation is deleted from the child. The comment at
`runtime/tether_sandbox.py:46` warning that the preview "must match
`HandleStore._write_dataframe` exactly (kept in sync by hand)" is deleted with it.

That comment says the duplication is guarded — "See tests for parity check". **No such test
exists.** Two copies of the preview format have been drifting unguarded, which strengthens
the case for deleting one copy rather than adding the missing test.

**Parent side** (`handles.py`). Today's `_write_dataframe` / `_write_json` / `_write_text` /
`_write_binary` each do two jobs: write bytes, then describe them. Split the describing half
into `_describe_dataframe(path)` / `_describe_json(path)` / `_describe_text(path)` /
`_describe_binary(path)`, each returning the metadata fields for a file that already exists.
`put()` keeps writing and then calls the describer; a new `adopt()` only describes:

```python
def adopt(self, *, id: str, kind: str, path: str, source: str) -> Handle:
    """Register a file written by sandboxed code. Metadata is derived here, never trusted."""
```

`adopt()` enforces, in order:

| Check | Rejection reason |
|---|---|
| `id` not already in `self._handles` | immutability — no repointing an existing handle |
| `safe_path(self.root, path)` | containment (existing behavior, retained) |
| the resolved path `is_file()` and is not a symlink | a directory, FIFO, or device would hang or mislead the describer |
| `kind` in `{json, text, dataframe, binary}` | an unknown kind is a contract violation, not a new format |

then derives `preview`, `bytes`, `schema`, `n_rows`, `n_cols` by reading the file. Because
the describers are the same code `put()` uses, an adopted handle and a parent-created handle
are indistinguishable in shape, and the preview bound applies to both by construction.

`_register_record` stays for manifest rehydration (`_load_manifest`), and the public
`register()` stays with it, since rehydrating a prior session's manifest is a legitimate
parent-side use; what changes is that **nothing on the sandbox ingestion path calls it any
more**. The immutability rule lives on `adopt()`, the child path, only.

> **Corrected during implementation.** This section originally said `_register_record` could
> stay *unchanged* because its records "are parent-authored". That is false. The manifest
> lives at `<root>/handles/_manifest.json` — inside the session root the container tier
> bind-mounts **rw** into the sandbox — so sandboxed code can rewrite it and the next
> `HandleStore(root)` would read it back. The rehydration path therefore re-derives: only
> `id`, `kind`, `path` and `source` are taken from the record, every described field comes
> from `_describe`, and a record whose file is missing or is not a regular file is dropped.
> See "Amended during implementation" at the end of this document.

**Orchestration side** (`sandbox.py`). `_ingest_new_handles` calls `adopt()` instead of
`register()`, and gains two bounds a hostile child would otherwise ignore:

- the new-handles file is read only up to `max_control_bytes`; beyond that the run reports an
  error rather than streaming an unbounded file into the parent;
- at most `max_new_handles` records are adopted per run.

The emit path gets the same treatment: `run_script` checks the emit file's size against a new
`max_emit_bytes` *before* `json.loads` (`sandbox.py:119-123`), so a child cannot flood context
through the result channel either. Over-limit is reported as an error on `ExecResult`, in
keeping with the existing "malformed emit payload" handling.

Ingestion stays tolerant of a single corrupt record (skip, don't abort) — that behavior is
deliberate and unchanged. What changes is that a *well-formed* record is no longer believed.

### Accepted trade-off, and how `_describe_dataframe` limits it

Deriving a dataframe's metadata means parsing child-written parquet **in the parent process**,
unconditionally, at ingestion time — untrusted-input parsing inside the trust boundary. This
is accepted, for two reasons. It is not a new exposure: `HandleStore.get()` already reads
child-written parquet on every `load` (`handles.py:193`), so the parser already runs parent-
side; what changes is that it now runs always, and earlier. And the alternative is worse —
trusting the metadata means every handle summary the model sees may be false and the audit
trail is forgeable by design, which is a certain weakness traded for a possible one.

`_describe_dataframe` is nevertheless written to read as little as it can, because the three
fields have very different costs:

| Field | Source | Cost |
|---|---|---|
| `schema`, `n_cols` | the parquet **footer** (`ParquetFile.schema_arrow`) | metadata only — no table data read |
| `n_rows` | the footer (`ParquetFile.metadata.num_rows`) | metadata only |
| `preview` | the **first row group** only (`read_row_group(0)`, then `head(5)`) | one row group, not the whole file |
| `bytes` | `stat().st_size` | no parse at all |

So a 500 MB parquet handle is described without ever materializing 500 MB in the parent. Row
groups are ordered, so the first five rows of row group 0 are the first five rows of the
table — preview output stays identical to `put()`'s `df.head(5).to_csv(index=False)`, which
the parity test asserts. A file with zero row groups (an empty frame) yields an empty preview
rather than an error.

If this ever needs tightening further, the describers remain a single seam: they can move to
sandbox-side describe-and-verify, or defer until first use.

---

## Workstream 2 — Isolation by default

`SandboxConfig.backend` changes from `"local"` to `"container"`. `_build_sandbox` (`session.py:169`) is the single seam where the
backend is chosen, so the guard goes there: it raises a new `SandboxRuntimeUnavailable`
when `container_runtime.detect_runtime()` finds neither podman nor docker. The message must name the opt-out explicitly, e.g.:

> no container runtime found (looked for podman, docker). Install one, or set
> `TetherConfig.sandbox.backend = "local"` to run sandboxed code with **no isolation**.

There is no fallback path. A machine without a runtime either gets a real boundary or an
explicit, informed decision to go without one.

### Test and eval impact

248 tests currently rely on the default backend and must stay runtime-free and fast. Rather
than edit each one, a **new** `tests/conftest.py` (there is none today) adds an autouse fixture pinning
`SandboxConfig.backend = "local"` for the suite; the existing container tests opt in
explicitly and stay gated on a runtime being present (`test_container_live.py`,
`test_sandbox_container.py`). `evals/run_evals.py` gets the same explicit pin. The *default*
is what changes; the suite's behavior does not.

One test must assert the new default directly (that `TetherConfig().sandbox.backend ==
"container"`), because the autouse fixture would otherwise hide a regression in it.

### Documentation

`local` is currently described as "best-effort isolation" in the README and as
"best-effort" in `sandbox.py`'s docstring. Both become "**no isolation** — the code runs as
the host user." The Sandbox tiers and Security sections swap which tier is presented as the
default, and `max_file_size_mb` gets a note that it is a local-tier-only field (it is already
documented as unenforced on the container tier — now the default tier).

---

## Workstream 3 — Egress guard

### The two egress points

`fetch_url` and `read_document` both perform **parent-side, model-controlled** network
requests with no address filtering:

- `fetch_url` (`tools/fetch.py`) validates the URL *scheme* only, then `client.get(url)` with
  `follow_redirects=True`.
- `read_document` passes an http(s) source **straight to Docling, which fetches it**
  (`tools/documents.py:52`). A guard placed only in `fetch.py` would miss this path entirely.

`web_search` and `web_extract` are **not** vectors and get no guard: our egress target is
`api.tavily.com`, fixed, and the model-supplied URL is fetched by Tavily on its own
infrastructure. This is worth stating in the spec so a later reader does not "fix" it.

### `tether/egress.py` (new)

One module owns the whole policy, so there is a single place to audit.

```python
class BlockedAddressError(ValueError): ...

def validate_url(url: str, cfg: FetchConfig) -> None:
    """Raise BlockedAddressError if url's host resolves to a denied address."""

def guarded_get(url: str, cfg: FetchConfig, *, client: httpx.Client | None = None) -> httpx.Response:
    """GET url, validating the initial address and every redirect hop."""
```

`validate_url` resolves the hostname with `socket.getaddrinfo` and checks **every** returned
address (a host with both an allowed and a denied address is denied). An address is denied
when `ipaddress.ip_address(...)` reports it `is_loopback`, `is_private`, `is_link_local`,
`is_reserved`, `is_multicast`, or `is_unspecified`, **or** when it falls in an explicit extra
range list. `169.254.169.254` is denied by a literal check as well, so the cloud-metadata
endpoint cannot survive a future loosening of the range checks. IPv6 is checked the same way;
IPv4-mapped forms such as `::ffff:127.0.0.1` are already caught by `is_loopback` (verified).

The extra range list exists because the six flags have one verified blind spot:
**`100.64.0.0/10`**, RFC 6598 carrier-grade NAT space, is reported private by *no* standard
flag, yet it is routinely used inside cloud and carrier networks. Every other special-use
range checked — RFC 2544 benchmarking, the three TEST-NETs, `192.0.0.0/24`, `240.0.0.0/4`,
the broadcast address, the IPv6 documentation and translation ranges — is already covered by
`is_reserved` or `is_private`, so CGNAT is the only addition needed.

The allowlist re-enables internal sources. `FetchConfig` gains:

```python
allow_private_hosts: tuple[str, ...] = ()   # hostnames or CIDRs exempted from the address denylist
max_redirects: int = 5
```

An entry matches either the URL's hostname (exact, case-insensitive) or a CIDR containing a
resolved address. An empty tuple — the default — denies everything internal.

`guarded_get` sets `follow_redirects=False` and walks hops itself, calling `validate_url`
before each request, up to `max_redirects`. A `Location` is resolved against the current URL
before validation so a relative redirect cannot bypass the check.

### Wiring

`fetch_url` replaces `client.get(url)` with `guarded_get(...)`. Its existing contract is
preserved: a blocked address is returned as a structured `{"error", "status": None, "url"}`
dict, exactly like a network failure today, so the agent can adapt rather than dead-end — the
scheme check stays a raise, since that is an upstream bug rather than an adaptable condition.

`read_document` stops handing URLs to Docling. For an http(s) source it fetches through
`guarded_get` into a temp file under the session root and passes Docling the **local path**.
This is the only way to close the hole: validating the initial URL alone would leave
Docling's own redirect-following unvalidated. It also simplifies the tool — Docling then has
exactly one input shape, a local path.

---

## Workstream 4 — Per-conversation tool isolation

`SessionManager.aopen` passes `self._tether._tools` — the same live list, including connected
MCP server objects — into every `Conversation.acreate` (`manager.py:92`). Each conversation's
`Session` then owns and closes servers it shares with others, which is a cross-tenant leak
vector, not merely a lifecycle wart.

The fix is an explicit marker, because a factory cannot be distinguished from a tool by
inspection — a plain Python tool is itself a zero-arg-callable-compatible object in the
general case, and sniffing would silently misclassify a user's tool.

```python
from tether import tool_factory
from agent_framework import MCPStdioTool

h = Tether(tools=[tool_factory(lambda: MCPStdioTool(name="msgraph", command="uv",
                                                    args=["run", "msgraph-mcp"]))])
```

`ToolFactory` is a small frozen dataclass wrapping a zero-arg callable; `tool_factory()` is
the public constructor. `SessionManager.aopen` and `Tether.asolve` expand each `ToolFactory`
by calling it **once per conversation**, so each conversation holds its own connections,
owned and closed by its own session. Non-factory entries pass through untouched, so existing
code keeps working; the README gains an explicit warning that sharing one live MCP instance
across conversations is unsupported, and points at `tool_factory` for multi-conversation
hosts such as an AG-UI backend.

---

## Config summary

```python
@dataclass
class SandboxConfig:
    backend: Literal["local", "container"] = "container"   # was "local"
    # + unchanged fields

@dataclass
class FetchConfig:
    allow_private_hosts: tuple[str, ...] = ()   # new — hostnames/CIDRs exempt from the denylist
    max_redirects: int = 5                       # new
    # + unchanged fields

@dataclass
class TetherConfig:
    max_emit_bytes: int = 1024 * 1024        # new — emit payload cap, checked before parse
    max_control_bytes: int = 8 * 1024 * 1024 # new — new-handles file read cap
    max_new_handles: int = 256               # new — records adopted per run
```

Placing the three new caps on `TetherConfig` rather than `SandboxConfig` is deliberate: they
bound the control plane between parent and child, which is orchestration, not a property of
either backend.

## Error handling

| Condition | Behavior |
|---|---|
| No container runtime, `backend="container"` | `SandboxRuntimeUnavailable` at `Session.create` — loud, names the `local` opt-out |
| Adopted record reuses an existing id | `ValueError`; the record is skipped, the run continues (consistent with today's tolerant ingestion) |
| Adopted path escapes root / is not a regular file | `ValueError`; record skipped |
| New-handles file over `max_control_bytes` | Run reports an error on `ExecResult`; handles adopted up to the bound are kept |
| Emit payload over `max_emit_bytes` | `ExecResult.error` set, `result` left `None` — mirrors the existing malformed-emit path |
| Blocked address in `fetch_url` | Structured `{"error", "status": None, "url"}` — adaptable, like a network failure |
| Blocked address in a redirect hop | Same structured error, naming the hop that was blocked |
| Blocked address in `read_document` | Structured `{"error", "source"}` — the tool's existing convention |

## Testing

TDD per repo convention: tests precede implementation for every unit. These are
security-critical and held to the `safe_path` bar. All offline — no model, network, or
container required.

**Workstream 1.** A child record whose `preview`, `bytes`, `n_rows` and `schema` are all
falsified is adopted with the **true derived values**, not the claimed ones — the central
test. A record reusing an existing id is rejected and the original handle is unchanged. A
record whose path is a directory, a symlink, or a missing file is rejected. An adopted
text/json handle's preview is bounded by `_PREVIEW_CHARS` even when the file is megabytes.
An adopted handle and an equivalent `put()` handle produce identical summaries — the parity
property that `runtime/tether_sandbox.py:46` claims is tested today but is not. A multi-row-
group parquet file is described **without reading beyond the first row group** (asserted by
spying on the reader), and an empty frame describes to an empty preview rather than raising. Over-limit emit, over-limit
new-handles file, and over-count records are each bounded. A single corrupt record still does
not abort ingestion.

**Workstream 2.** `TetherConfig().sandbox.backend == "container"`. `Session.create` with a
stubbed "no runtime" detector raises `SandboxRuntimeUnavailable` and the message names
`local`. With `backend="local"` explicitly, everything behaves as before.

**Workstream 3.** `validate_url` rejects each denied class — loopback (`127.0.0.1`,
`::1`, `::ffff:127.0.0.1`), private (`10/8`, `172.16/12`, `192.168/16`, `fd00::/8`),
link-local, the metadata IP specifically, unspecified, multicast, and CGNAT
(`100.64.0.0/10`, the flag blind spot) — and accepts ordinary public addresses in both
IPv4 and IPv6. A hostname
resolving to *both* a public and a private address is denied. The allowlist admits a named
host and a CIDR, and an empty allowlist admits neither. A redirect chain whose second hop
points at `127.0.0.1` is blocked at that hop, not followed. A relative `Location` is resolved
before validation. `max_redirects` is enforced. `read_document` with an http(s) source calls
the guard and hands Docling a local path, never the URL (asserted with an injected converter,
as the existing document tests do). Resolution is injected/stubbed so no test performs DNS.

**Workstream 4.** Two conversations opened from one `tool_factory` hold **distinct** tool
instances; closing one does not close the other's. A non-factory tool passes through
unchanged. `tool_factory` round-trips a plain callable without being mistaken for a tool.

## Scope / phasing

Workstreams are independent and land in this order, each its own reviewable unit
(landing order is not the workstream numbering above):

1. **Workstream 1, zero-trust metadata** — the largest change and the one that makes the audit trail
   trustworthy; everything else is easier to verify once handle data cannot lie.
2. **Workstream 3, egress guard** — self-contained new module plus two call sites.
3. **Workstream 4, per-conversation tools** — small, additive API change.
4. **Workstream 2, container default** — last deliberately: it is a one-line default flip plus a fixture and
   a docs pass, and doing it last means the preceding work is validated on the tier most
   contributors can actually run.

## Out of scope (noted for later)

- **IP pinning / full DNS-rebinding closure.** Needs a custom httpx transport and complicates
  TLS SNI and virtual hosting. The residual TOCTOU window is accepted and recorded here.
- **Relocating the control plane** out of the child-visible tree. Superseded for security
  purposes by re-derivation; retained in `docs/ROADMAP.md` as optional defense-in-depth.
- **Micro-VM tier** (gVisor / Firecracker) — the next isolation step, behind the same
  `SandboxExecutor` interface, once the default tier is real.
- **Egress filtering for MCP servers.** A user-supplied MCP server makes its own network
  calls; the harness cannot filter them. This is a documented property of trusting a server
  you install, not a gap this phase can close.
- **Rate limiting or quota on egress.** A separate concern from address filtering.

## Amended during implementation

The egress section above describes the original design. As shipped, the cloud-metadata check
differs in two ways:

- **Six endpoints are covered, not one:** `169.254.169.254`, `169.254.170.2` (AWS ECS),
  `168.63.129.16` (Azure wireserver, a *public* address that no range check would catch),
  `100.100.100.200` (Alibaba), `192.0.0.192` (Oracle), and `fd00:ec2::254` (AWS IMDS over IPv6).
- **They are non-allowlistable.** The check runs ahead of `allow_private_hosts` and nothing in
  `FetchConfig` can open them. Allowlisting a hostname vouches for the name, not for whatever
  it resolves to later.

### The manifest and the bytes are inside the child-writable root

The threat model above names the *control files* (registry, emit, new-handles) as living in
the child-visible, bind-mounted session root. Two more things live there and were not named:

- **The handle manifest** (`handles/_manifest.json`). The design above assumed its records
  were parent-authored; they are not. A child can rewrite the manifest, and the next
  `HandleStore` on that root would rehydrate the forged `preview`/`bytes`/`schema`/`n_rows`
  verbatim. Reachable whenever a root outlives the process that created it: a pinned
  `root_dir`, `asolve(keep=True)`, the eval harness, an AG-UI thread root. **Closed by
  re-derivation** in `_register_record` — `id`, `kind`, `path`, `source` from the record,
  everything else from `_describe`, and a record whose file is missing or is not a regular
  file is dropped. Rehydration stays tolerant: a bad record is skipped, not fatal.
- **The handle files themselves, after creation.** Metadata is derived once. Nothing stopped
  a later `run_python` from overwriting the bytes under an existing handle without touching
  the control channel, leaving the model holding a summary of data no longer on disk.
  **Closed by a digest** recorded alongside the derived metadata and verified on the parent
  side of every `HandleStore.get`; a mismatch raises `HandleTamperedError`. The digest is
  sha256 over the file's length plus its first 64 KiB, not the whole file — the describers
  deliberately read only a bounded window, and a whole-file hash would make every `get()`
  re-read a multi-gigabyte parquet. Any length change, and any edit inside the window every
  preview is drawn from, is caught; a length-preserving edit to the tail of a file larger
  than 64 KiB is not. That bound is documented on `_digest_file` rather than implied.
