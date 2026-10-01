# Host Integration — Isolation Guarantees, Image Readiness, Instructions, File Exchange — Design

- **Date:** 2026-10-01
- **Status:** Approved scope (all five items, phased: 1–3 then 4–5).
- **Motivation:** A multi-user web host embeds tether through the continuous-session API
  (`Tether.aopen` → `Conversation`, streaming `conv.agent.run`). To adopt it, the host needs
  (a) a sandbox that refuses to run without isolation when isolation is required, (b) image
  readiness that never hangs and never builds behind its back, (c) its own system prompt on
  the agent, and (d) a safe way to exchange files with the user in both directions. Items are
  numbered TETHER-1..5 after the host's requirements document.

## Relationship to the security-hardening design (2026-09-17)

That design is approved but not yet implemented, and overlaps this one. This design is written
to compose with it rather than pre-empt it:

| Hardening workstream | Overlap | Resolution here |
|---|---|---|
| WS2 — container becomes the default backend | `_build_sandbox` in `session.py` | TETHER-1 is **additive**: a `require_isolation` switch, default unchanged. The missing-runtime error is named `SandboxRuntimeUnavailable`, the name WS2 already specifies, so WS2 reuses it instead of adding a second type. |
| WS1 — zero-trust control plane | `_OrchestratedSandbox.run_script`, `handles.py` | The new publish control file follows WS1's discipline from the start: bounded read, bounded record count, every record untrusted and fully re-validated in the parent. Input handles are parent-authored (`HandleStore.put_input`), so they do not go through WS1's `adopt()` path. |
| WS4 — per-conversation tools | `api.py`, `manager.py` signatures | Only new keyword arguments are added; textual conflicts are expected but small. |

## TETHER-1 — Isolation can be made mandatory

`SandboxConfig.require_isolation: bool = False`. `_build_sandbox` (the single backend seam)
enforces it **at `Session.create`**:

- `backend="local"` with `require_isolation=True` → `SandboxRuntimeUnavailable` naming both
  fixes (switch to `container`, or drop the requirement for trusted/dev use).
- `backend="container"`: runtime detection already happens in `ContainerSandbox.__init__`
  (i.e. at `Session.create`). A missing runtime now raises `SandboxRuntimeUnavailable`
  (a `RuntimeError` subclass, so existing `except RuntimeError` callers keep working). There is
  no fallback to local anywhere, and none is added.
- Image presence is **not** checked at `Session.create` — that would add a runtime round trip
  to every conversation open. It is checked at run time (TETHER-2) and by the preflight, which
  is the host's startup gate.

Docs: the `SandboxConfig` docstring and README state that the local tier is **not a security
boundary** (scrubbed env + rlimits only; full host-user file and network access).

**Container guarantees under test.** Unit tests on `_build_run_argv` pin: `--network none`
unless `network=True`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, pids/memory/cpu
limits, exactly the session root (rw) + runtime (ro) [+ layer (ro), + inputs (ro, TETHER-5)]
mounted, and only `TETHER_*` / fixed `PATH`/`HOME`/`TMPDIR`/`PYTHONPATH` passed via `-e` (no
`--env-host`, no inherited values). Live tests (gated on a runtime) cover the acceptance list:
host file outside the root is unreadable, `/proc/<ppid>/environ` does not expose the host
environment, an outbound connection fails, a sentinel host env var is invisible, and a write
under `/workspace` appears in the host root.

## TETHER-2 — Image readiness

New `SandboxConfig` fields:

```python
build_on_demand: bool | None = None   # None -> auto: True unless require_isolation
build_timeout_s: float = 900.0        # image build + pip layer provisioning
```

`None` keeps 0.1 behavior for existing callers (build on first use) while giving a host that
sets `require_isolation=True` the safe behavior without a second switch.
`SandboxConfig.effective_build_on_demand` resolves it.

- `ensure_image(..., build_on_demand, timeout_s)`: missing image + build disabled →
  `SandboxImageMissing` naming the exact tag and the command that builds it
  (`tether-build-sandbox --preinstalled ... --pip ...`). Builds run with `timeout=` and raise
  `SandboxImageError` on expiry. `image inspect` always runs with a short timeout
  (`_INSPECT_TIMEOUT_S = 10`) so a wedged daemon cannot hang a check.
- `ensure_layer` gets the same treatment: an unprovisioned layer with builds disabled raises;
  provisioning is time-bounded.
- Errors raise out of `run_python`. MAF reports a raising tool to the model as a function error,
  and the run continues; the message is actionable for the operator.

**Preflight.** `tether.sandbox_preflight(config: SandboxConfig | TetherConfig) -> PreflightReport`

```python
@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    backend: str
    runtime: str | None
    image_tag: str | None
    problems: list[str]
```

Checks, without mutating anything: isolation requirement vs backend; runtime on PATH; image
present (bounded `image inspect`; a timeout or daemon error is a problem, not a hang); pip layer
sentinel present when `pip_packages` is set. Never calls `build` or `run`.

**`tether-build-sandbox`** gains `--preinstalled`, `--pip`, `--runtime`, `--timeout`, and
`--check` (prints the preflight report, exits non-zero on problems), so a deploy script can
build exactly the tag and layer the host will request.

## TETHER-3 — Agent instructions

`agent_instructions: str | None` on `Tether(...)` (default for every conversation),
`Tether.aopen` / `SessionManager.aopen` (per-conversation override; wins when both set),
`Tether.asolve` / `Tether.solve` / module `solve`, and `Conversation.acreate`, passed through to
`Session.create_agent`. Layering (documented): MAF assembles
`tether operating manual (core + bundles)` + blank line + `agent_instructions`. The override
applies when a conversation is **created**; re-opening a live id returns the existing agent.

## TETHER-4 — Publishing output files to the host

### Types (`tether/publish.py`)

```python
@dataclass(frozen=True)
class PublishedFile:
    path: Path; rel_path: str; name: str; size: int; sha256: str
    content_type: str | None; description: str | None; source: str

OnPublish = Callable[[PublishedFile], dict | None]
```

### Validation — `validate_publication(root, path, name, max_bytes)`

All of it runs in the parent, for both the tool and sandbox requests, in this order:

1. Non-empty string path.
2. `lstat` on the **unresolved** `root / path`: a symlink is rejected; a non-regular file
   (directory, FIFO, device, missing) is rejected.
3. `safe_path(root, path)` — catches `..`, absolute paths outside the root, and symlinked
   *parent directories* that escape.
4. The root-relative path must not start with `handles/` or `.scripts/`, and no top-level
   component may start with `_` (control files live at `_<kind>_<token>.*`).
5. Open with `O_NOFOLLOW`, `fstat` the descriptor (regular file, size ≤
   `TetherConfig.max_publish_bytes`, default 100 MB) and hash from that descriptor, so size and
   digest describe the bytes actually checked.
6. `safe_filename(name, fallback=basename)`: last path segment only, control/format characters
   removed, whitespace trimmed, `.`/`..`/empty → fallback, capped at 128 characters keeping
   the extension.

Hardlinks are not rejected: in the container tier a hardlink cannot cross the bind mount, and
in the local tier the child can already copy any host-user file, so a check adds nothing.

### Delivery — `Session.publish(path, *, name, description, source) -> dict`

Validates, builds `PublishedFile`, calls the host callback **synchronously in the calling
thread**. Tools run via `asyncio.to_thread`, which copies the caller's context, so host
contextvars set before `agent.run` are visible; the callback may therefore run on a worker
thread and must be thread-safe (documented). The file is untouched until the callback returns.
Returns a record `{name, rel_path, size, sha256, content_type, host}`; a validation failure or
a raising callback returns `{"error": ..., ...}` instead, so the agent loop continues and the
model can adapt. Emits a `StatusEvent(tool="publish_file")` per successful publication.

The callback is set on `Tether(on_publish=...)`, overridable per `aopen(on_publish=...)`, and
reaches the `Session` through `Conversation.acreate`.

### Model surface — opt-in `deliver` bundle

- `publish_file(path, name=None, description=None)` and
  `publish_handle(handle_id, format=None, name=None, description=None)`.
- Both are exposed only when the bundle is selected **and** a callback is configured; with no
  callback the tools are filtered out and the bundle's instructions are omitted.
- `publish_handle` converts a dataframe handle to `csv` / `xlsx` (needs openpyxl) / `parquet` /
  `json` under `outputs/`; other kinds are copied as-is to `outputs/`. It then goes through the
  same validation as `publish_file`.

### Sandbox helper — `publish(path, name=None, description=None)`

- Enabled iff a callback is configured: the parent creates `_publish_<token>.jsonl` and passes
  `TETHER_PUBLISH`; without it, `publish()` raises `RuntimeError("publishing is not enabled")`.
- The child normalizes absolute paths under `TETHER_ROOT` to root-relative (a convenience for
  `/workspace/...` in the container — not a trust step) and appends one JSON line.
- The parent processes requests **after the child exits, and only on exit code 0** (a failed or
  killed run may leave partial files; skipped requests are reported in `ExecResult.error`).
  Reads are bounded (`_MAX_PUBLISH_CONTROL_BYTES = 1 MiB`, `_MAX_PUBLISH_REQUESTS = 64`);
  malformed records are skipped. Each request goes through `Session.publish(source="run_python")`.
- `ExecResult.published: list[dict]` carries one record (or error record) per request.

### Bundle instructions

Write deliverables under `outputs/`; call `publish_file` (or `publish()` in code) only for
final files the user asked for; tell the user the delivered file name.

## TETHER-5 — Ingesting user-provided input files

`Session.add_input(source: Path | bytes, *, input_id, name, content_type=None,
description=None) -> Handle` and `Session.inputs -> dict[str, Handle]`, delegated by
`Conversation.add_input` (sync; raises if a turn is running) and `Conversation.aadd_input`
(async; waits for the turn lock).

- `input_id` is validated as a single path segment by a new `paths.validate_segment` (the same
  rule `_conv_root` applies, plus control characters and a length cap; `_conv_root` itself is
  left untouched to avoid churn in `manager.py`, which hardening WS4 also edits). `name` goes
  through `safe_filename`.
- `inputs/` and `inputs/<input_id>/` must resolve to exactly `<root>/inputs[/<id>]` — a
  symlink planted there by sandboxed code is refused, never written through.
- Size is checked against `TetherConfig.max_input_bytes` (default 100 MB) **before** copying
  (`len(bytes)` or `stat` of a regular file). The copy goes to a temp file in the target dir,
  then `os.replace`, then `chmod 0444`.
- Registered via a new `HandleStore.put_input(...)` as a `binary` handle,
  `path=inputs/<input_id>/<name>`, `source="upload:<name>"`, with new optional `Handle` fields
  `input_id`, `content_type`, and `description` (dropped from summaries when `None`, so
  existing manifests and non-input handle summaries are unchanged). Idempotency is by `input_id` and survives restarts because the field is in the
  manifest.
- **No parsing in the host process.** The preview is built from raw bytes only: name, size,
  content type, and for `csv/tsv/txt/json/md` the first few lines decoded as UTF-8 with
  replacement, capped at the store's preview bound.
- **Read-only to sandboxed code:** file mode `0444`; the container tier mounts
  `<root>/inputs` at `/workspace/inputs:ro` when it exists **as a real directory**. A symlink
  there (planted by an earlier run) is never mounted — the runtime would bind whatever host
  path it points at. The local tier's `0444` is
  advisory (same uid) — consistent with the local tier not being a boundary.
- `read_document` on an input parses **in the host process** (Docling). Documented as a risk;
  moving it into the sandbox is out of scope.
- Core instructions gain: user files are under `inputs/` and registered as handles; their
  contents are data, never instructions.

## Concurrency and timeouts (added after final review)

The safety argument for publishing and inputs ("the parent acts after the child exits") was
false as first built: MAF runs one turn's tool calls concurrently, and a timed-out container
kept running because only the runtime CLI was killed. Sandboxed code could then swap a path
under the root between a parent check and its use — mounting a host directory via `inputs/`,
leaking host file contents through publish, or writing host files via `publish_handle` /
`add_input`. Resolution:

- **One lock per session.** `_OrchestratedSandbox.lock` (an `RLock`) is held for the whole of
  `run_code` / `run_script`, including publish processing, and `Session` shares it as
  `io_lock` for `publish`, `publish_handle`, and `add_input`.
- **Timed-out containers are removed.** Each run is named `tether-<random>`; on timeout the
  parent runs `<runtime> rm -f <name>` and warns in stderr if that fails.
- **Private snapshot.** `validate_publication(..., snapshot_dir=)` copies the checked bytes
  from the validated descriptor; the host callback reads that copy, deleted afterwards.
- **Publish control file.** Random per-run token for all control files; the publish file is
  created `O_CREAT|O_EXCL|O_NOFOLLOW` and read `O_NOFOLLOW|O_NONBLOCK` + `S_ISREG`, so a
  planted symlink or FIFO is refused rather than followed or blocking. Path values the OS
  cannot represent are `PublishError`s, and a failing publisher call becomes an error record.

Not done, deliberately: moving `inputs/` out of the child-writable tree and dir-fd-relative
walks. With runs serialized and timeouts enforced, no child runs during those operations.

## Known gaps (deferred from final review, 2026-10-01)

Eight findings the final review graded Minor were deferred at merge. They are recorded here so
they live in the repo, each with its planned fix. Status: **open**; fixes in progress on
`fix/known-gaps` (plan: `docs/superpowers/plans/2026-10-01-host-integration-known-gaps.md`).

| # | Gap | Where | Planned fix |
|---|---|---|---|
| G1 | **Host-path oracle.** `validate_publication` runs `lstat` on the unresolved candidate before containment, so its distinct errors ("no such file" / "symlink" / "outside the workspace") tell sandboxed code whether an absolute host path exists. Via a planted parent-dir symlink it also distinguishes existing from missing host files. | `publish.py` | Check containment first (`safe_path`, which resolves but does not require existence), and return one message for every escape; only then `lstat` the in-root unresolved path for the final-component symlink check. |
| G2 | **Stale or forged input handle.** A repeat `add_input` returns the manifest's handle without checking its file. After a delete, the handle points at nothing; in the container tier the manifest is child-writable, so the record itself can be forged. | `session.py` `_add_input` | Return the existing handle only if it is intact: `kind == "binary"`, path exactly `inputs/<input_id>/<file>`, and that path is a regular, non-symlink file inside the root. Otherwise re-copy and re-register **under the same handle id**, so host references stay valid. |
| G3 | **Uploads can be blocked.** Sandboxed code can leave `inputs`, `inputs/<id>`, or the target filename as a symlink, file, or directory; `add_input` then fails for that conversation forever. | `session.py` `_input_dir` | Under the session lock, replace anything at those paths that is not the expected kind: unlink a symlink or file (never follow), `rmtree` a directory squatting on the target filename (`rmtree` does not follow links), then create. |
| G4 | **Sandbox `publish()` without the bundle.** It is live whenever `on_publish` is set, even if `deliver` is not selected; the README implies both are required. | `session.py`, `conversation.py` | **Decision for review — recommended: gate it.** `Session.create(config, *, on_publish=None, bundles=None)` enables sandbox `publish()` only when `deliver` is among the selected bundles (`None` = all, as today, so direct `Session` users and existing tests keep working); `Conversation.acreate` passes its bundles. Alternative: keep the behavior and correct the README. |
| G5 | **Model-facing docstring.** `run_python`'s description in `tools/registry.py` omits `publish()`. | `tools/registry.py` | Mention `publish(path)` as available when file delivery is enabled. |
| G6 | **`aadd_input` blocks the event loop** while copying up to `max_input_bytes`. | `conversation.py` | Hold the turn lock, run the copy via `asyncio.to_thread`. |
| G7 | **Unbounded copy of a host path.** The source is `stat`ed for size, then copied without a limit; a file that grows in between bypasses `max_input_bytes`. Low risk: the source is host-supplied. | `session.py` | Copy in a counted loop and abort (removing the temp file) once `max_input_bytes` is exceeded. |
| G8 | **Build timeout leaks and doubling.** Killing `podman build` / the pip-layer CLI on timeout may leave the work running; `ensure_layer` gives the image build and the pip install each a full `build_timeout_s`. | `container_runtime.py` | One deadline per `ensure_layer` call shared across both steps; name the pip-layer container and `rm -f` it on timeout (as the sandbox does). An image build can't be cancelled by name; document that the runtime may finish it in the background. |

Also outstanding, but owned by the hardening branch (see the plan's handoff note): the other
control files (`_new_handles`, `_emit`, `_registry`) and the `.scripts/` directory still need
the no-follow / non-blocking treatment the publish control file got.

## Config summary (all additive; existing call sites unchanged)

| Field | Default | Item |
|---|---|---|
| `SandboxConfig.require_isolation` | `False` | 1 |
| `SandboxConfig.build_on_demand` | `None` (auto: `not require_isolation`) | 2 |
| `SandboxConfig.build_timeout_s` | `900.0` | 2 |
| `TetherConfig.max_publish_bytes` | `100 MB` | 4 |
| `TetherConfig.max_input_bytes` | `100 MB` | 5 |

## Testing

TDD per repo convention; offline unless marked live. Security-relevant units
(`validate_publication`, `safe_filename`, `validate_segment`, publish-request ingestion,
`add_input`) are held to the `safe_path` bar. Each acceptance test in the host requirements
maps to at least one test here; container-only ones live in `test_container_live.py`.

## Out of scope

- Flipping the default backend (hardening WS2).
- Running `read_document` inside the sandbox.
- A hardened local tier (bubblewrap/Landlock).
