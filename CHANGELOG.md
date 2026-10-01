# Changelog

All changes are additive: existing call sites keep working with the defaults.

## Unreleased

### Isolation is on by default
- **Behavior change:** `SandboxConfig.backend` now defaults to `"container"`, not `"local"`.
  `run_python` executes model-authored code, so the shipped default is a real boundary.
  `TETHER_SANDBOX_BACKEND` overrides the default and selects the *tier* (`container` or
  `local`), not the container runtime; an explicit `SandboxConfig(backend=...)` still wins.
  Any other value raises `ValueError` rather than falling back.
- Selecting `local` emits `NoSandboxIsolationWarning`.
- Selecting `container` probes the runtime for *liveness* (`<runtime> info`), not just
  presence on `PATH`, and raises `SandboxRuntimeUnavailable` when it is absent or
  unresponsive. Successful probes are cached per process.

### Zero-trust handle metadata
- Sandboxed code reports only `{id, kind, path, source}` for a file it writes; the parent
  derives every described field (`preview`, `bytes`, `schema`, `n_rows`, `n_cols`) from the
  bytes on disk, including on manifest rehydration.
- Handle records are immutable: reusing an id is refused (`HandleIdReuseError`) and reported
  back to the model.
- A content digest is recorded when metadata is derived and checked on every read; a
  mismatch raises `HandleTamperedError`. The digest covers the file's length plus its first
  64 KiB.
- New `TetherConfig.max_emit_bytes` (1 MiB), `max_control_bytes` (8 MiB) and
  `max_new_handles` (256) bound the parent↔sandbox control channel.

### Egress policy
- New `tether/egress.py`. `fetch_url` and `read_document` deny loopback, private,
  link-local, reserved, multicast, unspecified and CGNAT (`100.64.0.0/10`) addresses by
  default, and re-validate every redirect hop (`follow_redirects=False` is forced).
- Cloud metadata endpoints are denied unconditionally and cannot be allowlisted.
- New `FetchConfig.allow_private_hosts` (hostnames or CIDRs) and `FetchConfig.max_redirects`.

### Per-conversation tools
- New `tool_factory(build)` marker: each conversation builds its own instance of a stateful
  tool (such as an MCP connection) instead of sharing one.

### Isolation can be made mandatory
- New `SandboxConfig.require_isolation: bool = False`. With `container` as the default this
  is the stricter assertion on top of it: when true, a non-container backend is refused
  *however it was selected* -- an explicit `backend="local"` or `TETHER_SANDBOX_BACKEND=local`
  -- raising `SandboxRuntimeUnavailable` at `Session.create`; a missing container runtime or
  image raises and never falls back to the local tier.
- A missing container runtime now raises `SandboxRuntimeUnavailable` (a `RuntimeError`
  subclass) at `Session.create`.
- Documented that the local tier is **not a security boundary**.

### Sandbox image readiness
- New `SandboxConfig.build_on_demand: bool | None = None` (`None` → build unless
  `require_isolation`). With builds disabled, a missing image or pip layer raises
  `SandboxImageMissing` immediately with the command that builds it.
- New `SandboxConfig.build_timeout_s: float = 900.0` bounds image builds and pip-layer
  provisioning (`SandboxImageError` on expiry). `image inspect` is bounded to 10 s.
- New `tether.sandbox_preflight(config) -> PreflightReport(ok, backend, runtime, image_tag,
  problems)`: a non-mutating readiness check that never builds.
- `tether-build-sandbox` accepts `--preinstalled`, `--pip`, `--runtime`, `--timeout`, and
  `--check`.

### Agent instructions
- `agent_instructions` on `Tether(...)` (default), `Tether.aopen` / `SessionManager.aopen`
  (per-conversation override), `asolve` / `solve`, and `Conversation.acreate`.

### Publishing files to the host
- New opt-in `deliver` bundle: `publish_file(path, name=None, description=None)` and
  `publish_handle(handle_id, format=None, name=None, description=None)`; exposed only when a
  host callback is configured.
- New `publish(path, name=None, description=None)` helper inside `run_python`; requests are
  processed after a clean exit and reported in `ExecResult.published`.
- New `Tether(on_publish=...)` / `aopen(on_publish=...)` receiving a `PublishedFile`.
- New `TetherConfig.max_publish_bytes: int = 100 MB`.
- The callback receives a private snapshot of the validated bytes (outside the workspace),
  deleted when it returns.

### Sandbox run safety
- Sandbox runs are serialized per session, and publishing / input ingestion take the same
  lock, so no sandboxed code runs while the parent touches paths in the workspace.
- A container that exceeds `timeout_s` is now force-removed by name; previously only the
  runtime CLI was killed and the container kept running.
- Control files use random per-run names.

### Known-gap fixes
- Publishing never follows links, so its answers no longer reveal whether a host path exists.
  **Behavior change:** publishing through a symlinked directory inside the workspace is
  refused; use the real path.
- `add_input` re-copies an input whose recorded file is missing, replaced, or forged (same
  handle id), repairs entries planted under `inputs/`, and bounds host-path copies. Sandboxed
  code can no longer create input records.
- **Behavior change:** a repeat `add_input` with the same `input_id` but different bytes now
  stores the host's new bytes (same handle id); with the same bytes it is still a no-copy
  no-op.
- `aadd_input` copies in a worker thread.
- Sandbox `publish()` requires the `deliver` bundle (`Session.create(..., bundles=None)` keeps
  the previous behavior for direct callers).
- `build_timeout_s` is one budget for the image build and pip layer; a timed-out pip-layer
  container is removed.

### Ingesting user files
- New `Conversation.add_input` / `aadd_input` / `inputs` (and `Session.add_input` /
  `Session.inputs`): uploads become read-only `binary` handles under `inputs/<input_id>/`,
  idempotent by `input_id` across restarts, never parsed in the host process.
- New `Handle` fields `input_id`, `content_type`, `description` (omitted when unset).
  On rehydration only `input_id` is carried from the (sandbox-writable) manifest;
  `content_type` is re-derived from the filename and `description` is dropped.
- New `TetherConfig.max_input_bytes: int = 100 MB`.
- The container tier mounts `inputs/` read-only.
