# Host Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a multi-user web host adopt tether: mandatory isolation, image readiness that
never hangs or builds silently, host system prompts, and safe file exchange in both directions
(publish outputs to the host; ingest user uploads).

**Architecture:** Additive changes on the existing seams. `SandboxConfig` gains isolation /
readiness switches enforced in `session._build_sandbox` and `container_runtime`. Publishing
adds a parent-side validator (`tether/publish.py`), a `Session.publish` delivery method, an
opt-in `deliver` bundle, and a bounded, untrusted child→parent control file. Inputs are
parent-authored binary handles under `inputs/`, never parsed in the host, mounted read-only in
the container tier.

**Tech Stack:** Python 3.12, Microsoft Agent Framework (`agent-framework-core`), pandas/pyarrow,
podman/docker CLI, pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-01-host-integration-design.md`

**Status of this plan:** written retroactively, partway through execution. Tasks 1–6 were
implemented test-first in this worktree before the plan existed and are marked done; their
code is in the tree, so those tasks list files, interfaces, and verification only. Tasks 7–12
are pending and carry full code. Nothing is committed yet.

## Global Constraints

- Python `>=3.12`; run everything through `uv run` (`uv sync --prerelease=allow` once).
- **All changes are additive**: existing 0.1.x call sites keep working with defaults. No
  default backend flip (that is hardening WS2's job).
- Defaults, verbatim from the spec: `require_isolation=False`; `build_on_demand=None` (auto:
  `not require_isolation`); `build_timeout_s=900.0`; `max_publish_bytes=100 MB`;
  `max_input_bytes=100 MB`; `_INSPECT_TIMEOUT_S=10`; `_MAX_PUBLISH_CONTROL_BYTES=1 MiB`;
  `_MAX_PUBLISH_REQUESTS=64`.
- Error names shared with the hardening branch: `SandboxRuntimeUnavailable(RuntimeError)`
  lives in **`tether/sandbox.py`** (identical to hardening plan Task 10).
- **Naming hard rule** from the local (gitignored) agent-conventions file applies to every
  tracked file, code comment, and commit message. Before each commit, run that file's
  pre-commit grep; it must print nothing new.
- **TDD**: failing test first, watch it fail, then implement.
- **Commits only when the user authorizes them.** Each task's commit step is the command to
  run once authorized. Before the first commit, `git checkout -- uv.lock` (the lock diff is
  benign `uv sync` drift, not part of this work).
- Test commands: `uv run pytest -p no:warnings <paths>`; the suite runs with `-q` and no
  network or keys. Live container tests in `tests/test_container_live.py` auto-skip without
  podman/docker; on macOS their roots must live under `$HOME`.

## Review Focus

Inputs the spec implies but the per-requirement tests don't exercise, most likely to bite first:

1. **Container-tier inputs are writable despite `0444`** (the container user owns the file and
   can `chmod` it back) — expect writes, chmod, and deletes to fail through the `:ro` mount.
   Pinned in Task 10 (`test_inputs_are_read_only_in_container`).
2. **`publish()` called with an absolute `/workspace/...` path inside the container** — expect
   it to map to the same root-relative path the host sees. Pinned in Task 10
   (`test_publish_from_container`).
3. **`publish_handle(format="xlsx")` without openpyxl installed** (it is not a core
   dependency) — expect a structured error, not a crashed tool call. Pinned in Task 8
   (`test_publish_handle_xlsx_without_openpyxl_is_an_error`).
4. **A text upload that is one enormous line, or not UTF-8** — expect a preview bounded by
   the store's preview cap, decoded with replacement, never an exception. Pinned in Task 8
   (`test_preview_bounded_for_single_huge_line_and_bad_utf8`).
5. **Re-adding an input after the conversation was reaped (TTL/close)** — the root is gone,
   so the same `input_id` must copy again rather than return a handle to a deleted file. Pinned
   in Task 9 (`test_input_is_recopied_after_reap`). A zero-byte upload is also accepted
   (Task 8, `test_empty_upload_is_accepted`).

---

## Phase 1 — TETHER-1, 2, 3 (release 1)

### Task 1: Agent instructions through `Tether` / `aopen` / `solve` (TETHER-3) — DONE

**Files:**
- Modify: `tether/conversation.py` (`Conversation.acreate(..., agent_instructions=None)`)
- Modify: `tether/manager.py` (`SessionManager.aopen(..., agent_instructions=None)`)
- Modify: `tether/api.py` (`Tether(..., agent_instructions=None)`, `aopen`, `asolve`, `solve`,
  module `solve`)
- Test: `tests/test_agent_instructions.py`

**Interfaces:**
- Produces: `Tether._agent_instructions: str | None`; per-call value wins when not `None`.
  Instructions land at `agent.default_options["instructions"]` as
  `<core + bundles>\n\n<agent_instructions>`.

- [x] Step 1: failing tests (`acreate`, `aopen`, Tether default, override precedence, `solve`)
- [x] Step 2: watched them fail (`unexpected keyword argument 'agent_instructions'`)
- [x] Step 3: implemented the pass-through
- [x] Step 4: `uv run pytest -p no:warnings tests/test_agent_instructions.py tests/test_api.py tests/test_manager.py tests/test_conversation.py` → exit 0
- [ ] Step 5: Commit

```bash
git add tether/api.py tether/manager.py tether/conversation.py tests/test_agent_instructions.py
git commit -m "feat(api): pass agent_instructions through Tether, aopen, and solve"
```

### Task 2: Mandatory isolation (TETHER-1) — DONE

**Files:**
- Modify: `tether/config.py` (`SandboxConfig` docstring; `require_isolation`)
- Modify: `tether/sandbox.py` (`SandboxRuntimeUnavailable`; local-tier docstring)
- Modify: `tether/session.py` (`_check_isolation` before any filesystem work in
  `Session.create`; `_build_sandbox` wraps `ContainerSandbox` construction)
- Modify: `tests/test_container_live.py` (5 acceptance tests: outside-root file, parent
  environ, host env, outbound connection, `/workspace` write)
- Test: `tests/test_isolation.py`

**Interfaces:**
- Produces: `SandboxConfig.require_isolation: bool = False`;
  `tether.SandboxRuntimeUnavailable`.

- [x] Steps 1–4: tests written first, failed, implemented, pass (offline + 11 live podman tests)
- [ ] Step 5: Commit (together with Task 3's README/CHANGELOG lines that cover TETHER-1)

```bash
git add tether/config.py tether/sandbox.py tether/session.py tests/test_isolation.py \
        tests/test_container_live.py
git commit -m "feat(sandbox): require_isolation refuses a non-isolating backend at Session.create"
```

### Task 3: Image readiness, preflight, build CLI (TETHER-2) — DONE

**Files:**
- Modify: `tether/config.py` (`build_on_demand`, `build_timeout_s`,
  `effective_build_on_demand`)
- Modify: `tether/container_runtime.py` (`SandboxImageError`, `SandboxImageMissing`,
  bounded `image_exists`, `build_command_hint`, gated/bounded `ensure_image` and
  `ensure_layer`, `PreflightReport`, `sandbox_preflight`, argparse `_build_sandbox_main`)
- Modify: `tether/__init__.py` (exports)
- Modify: `README.md` (local tier not a boundary; requiring isolation; image readiness;
  config row; agent instructions), `CHANGELOG.md` (new)
- Test: `tests/test_image_readiness.py`

**Interfaces:**
- Produces: `sandbox_preflight(config: SandboxConfig | TetherConfig, *, which=shutil.which,
  run=subprocess.run, layer_base: Path | None = None) -> PreflightReport`;
  `PreflightReport(ok, backend, runtime, image_tag, problems)`;
  `_build_sandbox_main(argv: list[str] | None = None)` with `--preinstalled --pip --runtime
  --timeout --check`.

- [x] Steps 1–4: done; full suite 276 passed / 8 skipped at end of phase 1
- [ ] Step 5: Commit

```bash
git add tether/config.py tether/container_runtime.py tether/__init__.py README.md CHANGELOG.md \
        tests/test_image_readiness.py
git commit -m "feat(sandbox): build_on_demand, bounded builds, and sandbox_preflight"
```

## Phase 2 — TETHER-4, 5 (release 2)

### Task 4: Safe names and segments — DONE

**Files:**
- Modify: `tether/paths.py` (`validate_segment`, `safe_filename`)
- Test: `tests/test_filenames.py`

**Interfaces:**
- Produces: `validate_segment(value: str, *, what: str) -> str` (raises `ValueError`);
  `safe_filename(name: str | None, *, fallback: str) -> str` (never raises; last resort
  `"file"`; ≤128 chars keeping the extension).

- [x] Steps 1–4: 34 tests pass
- [ ] Step 5: Commit

```bash
git add tether/paths.py tests/test_filenames.py
git commit -m "feat(paths): validate_segment and safe_filename for untrusted ids and names"
```

### Task 5: Publication validator (TETHER-4 core) — DONE

**Files:**
- Create: `tether/publish.py` (`PublishError`, `PublishedFile`, `OnPublish`,
  `validate_publication`)
- Test: `tests/test_publish.py`

**Interfaces:**
- Produces: `validate_publication(root, path, *, name, description, source: str,
  max_bytes: int) -> PublishedFile`; raises `PublishError(ValueError)`.

- [x] Steps 1–4: 22 tests pass (traversal, absolute outside, symlink file, symlinked parent
  dir, directory, FIFO, missing, internal files, oversize, bad values)
- [ ] Step 5: Commit

```bash
git add tether/publish.py tests/test_publish.py
git commit -m "feat(publish): parent-side validation for published workspace files"
```

### Task 6: Delivery — `Session.publish`, `deliver` bundle, sandbox `publish()` — DONE

**Files:**
- Modify: `tether/config.py` (`max_publish_bytes`, `max_input_bytes`)
- Modify: `tether/bundles.py` (`deliver` bundle + instructions; inputs-as-data core line;
  `exclude=` on `tool_names_for` / `instructions_for`)
- Modify: `tether/session.py` (`on_publish` field, `Session.create(..., on_publish=None)`,
  `publish()`, deliver filtered out without a callback)
- Create: `tether/tools/deliver.py` (`publish_file`, `publish_handle`, `_write_output`)
- Modify: `tether/tools/registry.py`, `tether/tools/code.py`
- Modify: `tether/sandbox.py` (`ExecResult.published`, `_RunContext.publish_file`,
  `publisher` attribute, `_process_publications`, `TETHER_PUBLISH` env)
- Modify: `tether/sandbox_container.py` (`TETHER_PUBLISH` translated to `/workspace`)
- Modify: `tether/runtime/tether_sandbox.py` (`publish()`)
- Modify: `tether/conversation.py`, `tether/manager.py`, `tether/api.py` (`on_publish`
  plumbing; `aopen` override wins)
- Test: `tests/test_deliver.py`

**Interfaces:**
- Consumes: `validate_publication`, `safe_filename`, `safe_path`.
- Produces: `Session.publish(path, *, name=None, description=None, source: str) -> dict`
  returning `{name, rel_path, size, sha256, content_type, host}` or a dict with `error`;
  `Session.on_publish: OnPublish | None`; `ExecResult.published: list[dict]`.

- [x] Steps 1–4: 28 tests pass, including contextvar visibility for both paths and the agent
  loop continuing after a raising callback
- [ ] Step 5: Commit

```bash
git add tether/config.py tether/bundles.py tether/session.py tether/tools/deliver.py \
        tether/tools/registry.py tether/tools/code.py tether/sandbox.py \
        tether/sandbox_container.py tether/runtime/tether_sandbox.py tether/conversation.py \
        tether/manager.py tether/api.py tests/test_deliver.py
git commit -m "feat(deliver): publish_file, publish_handle, and sandbox publish() to a host callback"
```

### Task 7: Input fields on `Handle`; `HandleStore.put_input` / `inputs()` (TETHER-5)

**Files:**
- Modify: `tether/handles.py` (`Handle` fields; `put_input`; `inputs`)
- Test: `tests/test_handles.py` (append)

**Interfaces:**
- Produces: `Handle.input_id / content_type / description: str | None = None`;
  `HandleStore.put_input(*, path: str, size: int, preview: str, source: str, input_id: str,
  content_type: str | None = None, description: str | None = None) -> Handle`;
  `HandleStore.inputs() -> dict[str, Handle]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_handles.py`:

```python
def test_put_input_registers_binary_handle(tmp_path):
    from tether.handles import HandleStore
    store = HandleStore(tmp_path)
    (tmp_path / "inputs" / "f1").mkdir(parents=True)
    (tmp_path / "inputs" / "f1" / "a.csv").write_bytes(b"x")
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="upload:a.csv",
                        input_id="f1", content_type="text/csv", description="d")
    assert (h.kind, h.input_id, h.content_type, h.description) == ("binary", "f1", "text/csv", "d")
    assert store.inputs() == {"f1": h}
    assert store.get(h.id) == str((tmp_path / "inputs/f1/a.csv").resolve())


def test_put_input_is_idempotent_and_survives_reload(tmp_path):
    from tether.handles import HandleStore
    store = HandleStore(tmp_path)
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="s", input_id="f1")
    assert store.put_input(path="inputs/f1/b.csv", size=9, preview="q", source="s",
                           input_id="f1") == h
    assert HandleStore(tmp_path).inputs() == {"f1": h}


def test_put_input_rejects_escaping_path(tmp_path):
    from tether.handles import HandleStore
    import pytest
    with pytest.raises(ValueError):
        HandleStore(tmp_path).put_input(path="../x", size=1, preview="p", source="s",
                                        input_id="f1")


def test_non_input_summary_has_no_input_fields(tmp_path):
    from tether.handles import HandleStore
    s = HandleStore(tmp_path).put("hello", source="t").summary()
    assert not {"input_id", "content_type", "description"} & s.keys()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_handles.py -k "input"`
Expected: FAIL — `AttributeError: 'HandleStore' object has no attribute 'put_input'`

- [ ] **Step 3: Implement** — in `tether/handles.py`, change the `Handle` dataclass:

```python
@dataclass
class Handle:
    id: str
    kind: str  # "json" | "text" | "dataframe" | "binary"
    path: str  # POSIX path relative to the session root
    source: str
    bytes: int
    preview: str
    schema: dict[str, str] | None = None
    n_rows: int | None = None
    n_cols: int | None = None
    input_id: str | None = None       # host-supplied id of an uploaded input
    content_type: str | None = None   # advisory type of an uploaded input
    description: str | None = None    # host-supplied description of an uploaded input
```

and add to `HandleStore`, after `register`:

```python
    def put_input(self, *, path: str, size: int, preview: str, source: str, input_id: str,
                  content_type: str | None = None, description: str | None = None) -> Handle:
        """Register a host-ingested upload whose file already exists under ``inputs/``.

        Parent-authored (the host wrote the bytes), so it does not go through the sandbox
        adoption path. Idempotent by ``input_id``: an existing input is returned unchanged.
        """
        existing = self.inputs().get(input_id)
        if existing is not None:
            return existing
        try:
            safe_path(self.root, path)
        except PathEscapesRootError as e:
            raise ValueError(f"input path escapes root: {path!r}") from e
        handle = Handle(id=self._new_id(), kind="binary", path=path, source=source, bytes=size,
                        preview=preview, input_id=input_id, content_type=content_type,
                        description=description)
        self._handles[handle.id] = handle
        self._save_manifest()
        return handle

    def inputs(self) -> dict[str, Handle]:
        """Uploaded inputs, keyed by host-supplied ``input_id``."""
        return {h.input_id: h for h in self._handles.values() if h.input_id is not None}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_handles.py`
Expected: PASS (all, including pre-existing manifest round-trip tests)

- [ ] **Step 5: Commit**

```bash
git add tether/handles.py tests/test_handles.py
git commit -m "feat(handles): input handles keyed by input_id, persisted in the manifest"
```

### Task 8: `Session.add_input` / `Session.inputs`; review-focus tests (TETHER-5)

**Files:**
- Modify: `tether/session.py`
- Test: `tests/test_inputs.py` (already written, currently failing), plus three tests below
  appended to it and one to `tests/test_deliver.py`

**Interfaces:**
- Consumes: `validate_segment`, `safe_filename`, `safe_path`, `HandleStore.put_input`,
  `HandleStore.inputs`, `handles._PREVIEW_CHARS`.
- Produces: `Session.add_input(source: Path | bytes, *, input_id: str, name: str,
  content_type: str | None = None, description: str | None = None) -> Handle`;
  `Session.inputs -> dict[str, Handle]`.

- [ ] **Step 1: Add the review-focus tests.** Append to `tests/test_inputs.py`:

```python
def test_preview_bounded_for_single_huge_line_and_bad_utf8(tmp_path):
    from tether.handles import _PREVIEW_CHARS
    sess = _session(tmp_path)
    h = sess.add_input(b"\xff\xfe" + b"x" * 100_000, input_id="f1", name="big.txt")
    assert len(h.preview) <= _PREVIEW_CHARS
    assert "�" in h.preview


def test_empty_upload_is_accepted(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"", input_id="f1", name="empty.csv")
    assert h.bytes == 0 and (sess.root / h.path).read_bytes() == b""


def test_rejects_non_regular_source(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="regular file"):
        sess.add_input(tmp_path, input_id="f1", name="dir")
```

Append to `tests/test_deliver.py`:

```python
def test_publish_handle_xlsx_without_openpyxl_is_an_error(tmp_path, monkeypatch):
    host = _Host()
    sess = _session(tmp_path, host)
    h = sess.store.put(pd.DataFrame({"a": [1]}), source="t")

    def no_openpyxl(self, *a, **k):
        raise ImportError("Missing optional dependency 'openpyxl'")

    monkeypatch.setattr(pd.DataFrame, "to_excel", no_openpyxl)
    out = _tool(sess, "publish_handle")(h.id, format="xlsx")
    assert "openpyxl" in out["error"] and host.calls == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_inputs.py tests/test_deliver.py`
Expected: `test_inputs.py` FAILs with `AttributeError: 'Session' object has no attribute
'add_input'`; the new `test_deliver.py` test PASSes already (Task 6's `_write_output` catches
`ImportError`) — it pins behavior, it is not a new feature.

- [ ] **Step 3: Implement.** In `tether/session.py` add imports:

```python
import mimetypes
import os
import stat
import tempfile
```

```python
from .handles import _PREVIEW_CHARS, Handle, HandleStore
from .paths import PathEscapesRootError, safe_filename, safe_path, validate_segment
```

module constants after `_DELIVER`:

```python
_INPUTS = "inputs"
_TEXT_PREVIEW_EXTS = {".csv", ".tsv", ".txt", ".json", ".md"}
_PREVIEW_LINES = 5
_PREVIEW_READ_BYTES = 4096
```

methods on `Session` (after `publish`):

```python
    @property
    def inputs(self) -> dict[str, Handle]:
        """User-provided inputs, keyed by host-supplied ``input_id``."""
        return self.store.inputs()

    def add_input(self, source: Path | bytes, *, input_id: str, name: str,
                  content_type: str | None = None, description: str | None = None) -> Handle:
        """Place a user upload at ``inputs/<input_id>/<name>`` and register it as a handle.

        The upload is untrusted and is **never parsed here**: it becomes a ``binary`` handle
        whose preview is built from raw bytes only. Idempotent by ``input_id`` (also across
        restarts, via the manifest). The file is made read-only (``0444``); the container tier
        additionally mounts ``inputs/`` read-only. Not safe to call while a turn is running —
        use ``Conversation.add_input`` / ``aadd_input``, which enforce that.
        """
        validate_segment(input_id, what="input id")
        existing = self.store.inputs().get(input_id)
        if existing is not None:
            return existing
        filename = safe_filename(name, fallback="upload.bin")
        size = _input_size(source)
        if size > self.config.max_input_bytes:
            raise ValueError(f"input too large ({size} bytes > {self.config.max_input_bytes})")

        target_dir = self._input_dir(input_id)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".upload_")
        try:
            with os.fdopen(fd, "wb") as out:
                if isinstance(source, (bytes, bytearray)):
                    out.write(source)
                else:
                    with open(source, "rb") as src:
                        shutil.copyfileobj(src, out)
            os.chmod(tmp, 0o444)
            target = target_dir / filename
            os.replace(tmp, target)   # replaces (never writes through) a planted symlink
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        ctype = content_type or mimetypes.guess_type(filename)[0]
        return self.store.put_input(
            path=f"{_INPUTS}/{input_id}/{filename}", size=size,
            preview=_input_preview(target, filename, size, ctype),
            source=f"upload:{filename}", input_id=input_id, content_type=ctype,
            description=description)

    def _input_dir(self, input_id: str) -> Path:
        """Create and return ``<root>/inputs/<input_id>``, refusing any planted symlink."""
        for rel in (_INPUTS, f"{_INPUTS}/{input_id}"):
            expected = self.root / rel
            try:
                resolved = safe_path(self.root, rel)
            except PathEscapesRootError as e:
                raise ValueError(f"{rel}/ resolves outside the workspace") from e
            if resolved != expected:
                raise ValueError(f"{rel}/ is a link; refusing to write through it")
            expected.mkdir(exist_ok=True)
        return self.root / _INPUTS / input_id
```

module-level helpers (after `_resolve_root`):

```python
def _input_size(source: Path | bytes) -> int:
    if isinstance(source, (bytes, bytearray)):
        return len(source)
    st = os.stat(source)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"input source is not a regular file: {source}")
    return st.st_size


def _input_preview(path: Path, filename: str, size: int, content_type: str | None) -> str:
    """Name/size/type, plus the first lines for text-like files. Raw bytes only, no parsing."""
    head = f"<uploaded file {filename}, {size} bytes, {content_type or 'unknown type'}>"
    if Path(filename).suffix.lower() not in _TEXT_PREVIEW_EXTS:
        return head
    with open(path, "rb") as f:
        raw = f.read(_PREVIEW_READ_BYTES)
    lines = raw.decode("utf-8", errors="replace").splitlines()[:_PREVIEW_LINES]
    return (head + "\n" + "\n".join(lines))[:_PREVIEW_CHARS]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_inputs.py tests/test_deliver.py tests/test_session.py`
Expected: PASS except `test_conversation_*` (Task 9) and
`test_container_mounts_inputs_read_only` / `test_container_does_not_mount_symlinked_inputs`
(Task 10).

- [ ] **Step 5: Commit**

```bash
git add tether/session.py tests/test_inputs.py tests/test_deliver.py
git commit -m "feat(inputs): Session.add_input ingests uploads as read-only binary handles"
```

### Task 9: `Conversation.add_input` / `aadd_input` / `inputs` (TETHER-5)

**Files:**
- Modify: `tether/conversation.py`
- Test: `tests/test_inputs.py` (existing `test_conversation_*` tests plus one below)

**Interfaces:**
- Consumes: `Session.add_input`, `Session.inputs`.
- Produces: `Conversation.add_input(...) -> Handle` (raises `RuntimeError` mid-turn);
  `async Conversation.aadd_input(...) -> Handle` (waits for the turn lock);
  `Conversation.inputs -> dict[str, Handle]`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_inputs.py`:

```python
def test_input_is_recopied_after_reap(tmp_path):
    async def open_conv():
        return await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]), bundles=("code",))

    async def run():
        first = await open_conv()
        first.add_input(b"x", input_id="f1", name="a.txt")
        await first.aclose()                       # reap_on_close=True deletes the root
        second = await open_conv()
        try:
            assert second.inputs == {}
            h = second.add_input(b"x", input_id="f1", name="a.txt")
            return (second.session.root / h.path).read_bytes()
        finally:
            await second.aclose()

    assert asyncio.run(run()) == b"x"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_inputs.py -k conversation_or_reap`
Expected: FAIL — `AttributeError: 'Conversation' object has no attribute 'add_input'`

- [ ] **Step 3: Implement** — in `tether/conversation.py`, extend the `TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    from pathlib import Path

    from .api import Result
    from .handles import Handle
```

and add methods after `aask`:

```python
    @property
    def inputs(self) -> dict[str, "Handle"]:
        """User-provided inputs in this conversation's workspace, by ``input_id``."""
        return self.session.inputs

    def add_input(self, source: "Path | bytes", *, input_id: str, name: str,
                  content_type: str | None = None,
                  description: str | None = None) -> "Handle":
        """Ingest a user upload between turns (see ``Session.add_input``).

        Raises ``RuntimeError`` while a turn is running; use ``aadd_input`` to wait instead.
        """
        if self._lock.locked():
            raise RuntimeError("cannot add an input while a turn is running; use aadd_input()")
        return self.session.add_input(source, input_id=input_id, name=name,
                                      content_type=content_type, description=description)

    async def aadd_input(self, source: "Path | bytes", *, input_id: str, name: str,
                         content_type: str | None = None,
                         description: str | None = None) -> "Handle":
        """Ingest a user upload, waiting for any running turn to finish first."""
        async with self._lock:
            return self.session.add_input(source, input_id=input_id, name=name,
                                          content_type=content_type, description=description)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_inputs.py tests/test_conversation.py`
Expected: PASS except the two container-mount tests (Task 10)

- [ ] **Step 5: Commit**

```bash
git add tether/conversation.py tests/test_inputs.py
git commit -m "feat(inputs): Conversation.add_input / aadd_input with turn-lock safety"
```

### Task 10: Read-only `inputs/` mount in the container tier; live acceptance

**Files:**
- Modify: `tether/sandbox_container.py` (`_build_run_argv`)
- Test: `tests/test_inputs.py` (two mount tests, already written), `tests/test_isolation.py`
  (`test_container_mounts_only_root_and_runtime` must still pass — no inputs dir there),
  `tests/test_container_live.py` (append two)

**Interfaces:**
- Consumes: `Session.add_input`, `Session.create(..., on_publish=...)`.
- Produces: `-v <root>/inputs:/workspace/inputs:ro` in the run argv when `<root>/inputs` is a
  real directory.

- [ ] **Step 1: Write the live tests** — append to `tests/test_container_live.py`:

```python
def test_inputs_are_read_only_in_container(croot):
    from tether import Session, TetherConfig
    sess = Session.create(TetherConfig(root_dir=croot,
                                       sandbox=SandboxConfig(backend="container")))
    h = sess.add_input(b"a,b\n1,2\n", input_id="f1", name="data.csv")
    res = sess.sandbox.run_code(
        "import os\nfrom tether_sandbox import load, emit\n"
        f"p = load({h.id!r})\ndata = open(p).read()\n"
        "def attempt(fn):\n"
        "    try:\n        fn()\n        return 'allowed'\n"
        "    except OSError:\n        return 'denied'\n"
        "emit([data, attempt(lambda: os.chmod(p, 0o644)),\n"
        "      attempt(lambda: open(p, 'a').write('x')), attempt(lambda: os.remove(p))])\n")
    assert res.error is None, res.error
    assert res.result == ["a,b\n1,2\n", "denied", "denied", "denied"]
    assert (croot / h.path).read_bytes() == b"a,b\n1,2\n"


def test_publish_from_container(croot):
    from tether import Session, TetherConfig
    calls = []
    sess = Session.create(TetherConfig(root_dir=croot,
                                       sandbox=SandboxConfig(backend="container")),
                          on_publish=lambda pf: calls.append(pf) or {"id": 1})
    res = sess.sandbox.run_code(
        "import os\nfrom tether_sandbox import publish\n"
        "os.makedirs('/workspace/outputs', exist_ok=True)\n"
        "open('/workspace/outputs/a.csv', 'w').write('a\\n1\\n')\n"
        "publish('/workspace/outputs/a.csv', name='a.csv')\n")
    assert res.error is None, res.error
    assert [p.rel_path for p in calls] == ["outputs/a.csv"]
    assert res.published[0]["host"] == {"id": 1}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_inputs.py -k container tests/test_container_live.py`
Expected: `test_container_mounts_inputs_read_only` FAILs (no `:ro` mount);
`test_inputs_are_read_only_in_container` FAILs (`chmod` / append `allowed`).
`test_publish_from_container` should already PASS (Task 6) — it pins the `/workspace` mapping.

- [ ] **Step 3: Implement** — in `ContainerSandbox._build_run_argv`, immediately after the
`argv += ["--user", ..., "-w", "/workspace"]` block:

```python
        inputs = ctx.root / "inputs"
        # Uploaded inputs are read-only to sandboxed code: overlay them :ro on the rw workspace.
        # Only a real directory is mounted -- a symlink planted by an earlier run would make the
        # runtime bind-mount whatever host path it points at.
        if inputs.is_dir() and not inputs.is_symlink():
            argv += ["-v", f"{inputs}:/workspace/inputs:ro"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_inputs.py tests/test_isolation.py tests/test_sandbox_container.py tests/test_container_live.py`
Expected: PASS (live tests run when podman/docker is available; they must not be skipped for
this task's sign-off on a machine that has a runtime)

- [ ] **Step 5: Commit**

```bash
git add tether/sandbox_container.py tests/test_container_live.py
git commit -m "feat(sandbox): mount inputs/ read-only in the container tier"
```

### Task 11: Phase-2 docs and exports

**Files:**
- Modify: `tether/__init__.py`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_bundles.py` (append one)

**Interfaces:**
- Produces: `from tether import PublishedFile, OnPublish, PublishError`.

- [ ] **Step 1: Failing test** — append to `tests/test_bundles.py`:

```python
def test_public_exports_for_host_integration():
    import tether
    for name in ("PublishedFile", "OnPublish", "PublishError", "sandbox_preflight",
                 "PreflightReport", "SandboxRuntimeUnavailable"):
        assert hasattr(tether, name) and name in tether.__all__
```

- [ ] **Step 2: Run** `uv run pytest -p no:warnings tests/test_bundles.py` → FAIL on `PublishedFile`.

- [ ] **Step 3: Implement.** In `tether/__init__.py` add
`from .publish import OnPublish, PublishedFile, PublishError` and the three names to `__all__`.

In `README.md`, add to the Tool surface table:

```markdown
| `publish_file(path, name=None, description=None)` | Deliver a workspace file to the host (`deliver` bundle; only with `on_publish`) |
| `publish_handle(handle_id, format=None, name=None)` | Deliver a handle as csv/xlsx/parquet/json (`deliver` bundle; only with `on_publish`) |
```

Add a guide section `### Exchanging files with the user` after "Sessions: one-shot vs
continuous":

````markdown
### Exchanging files with the user

**Delivering files.** Pass `on_publish` (on `Tether(...)`, or per conversation on `aopen`) and
select the `deliver` bundle. The model writes a file under `outputs/` and calls `publish_file`
(or `publish()` inside `run_python`, or `publish_handle` for a handle). Tether validates every
request in the parent — inside the root, a regular file and not a symlink, not under
`handles/` or `.scripts/` or a control file, at most `max_publish_bytes` — then calls your
callback with a `PublishedFile(path, rel_path, name, size, sha256, content_type, description,
source)`. Copy the file during the callback; `path` is valid only until it returns. Its return
value reaches the model under `"host"`; if it raises, the tool returns an error and the run
continues. The callback runs synchronously in the tool call's context (so your contextvars are
visible) but possibly on a worker thread — make it thread-safe. Sandbox `publish()` requests
are processed after the script exits cleanly.

```python
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
````

Add to the Configuration table:

```markdown
| `max_publish_bytes` | `100 MiB` | Largest file `publish_file` / `publish()` will deliver |
| `max_input_bytes` | `100 MiB` | Largest upload `add_input` will accept |
```

Append to `CHANGELOG.md` under `## Unreleased`:

```markdown
### Publishing files to the host
- New opt-in `deliver` bundle: `publish_file(path, name=None, description=None)` and
  `publish_handle(handle_id, format=None, name=None, description=None)`; exposed only when a
  host callback is configured.
- New `publish(path, name=None, description=None)` helper inside `run_python`; requests are
  processed after a clean exit and reported in `ExecResult.published`.
- New `Tether(on_publish=...)` / `aopen(on_publish=...)` receiving a `PublishedFile`.
- New `TetherConfig.max_publish_bytes: int = 100 MB`.

### Ingesting user files
- New `Conversation.add_input` / `aadd_input` / `inputs` (and `Session.add_input` /
  `Session.inputs`): uploads become read-only `binary` handles under `inputs/<input_id>/`,
  idempotent by `input_id` across restarts, never parsed in the host process.
- New `Handle` fields `input_id`, `content_type`, `description` (omitted when unset).
- New `TetherConfig.max_input_bytes: int = 100 MB`.
- The container tier mounts `inputs/` read-only.
```

- [ ] **Step 4: Run** `uv run pytest -p no:warnings tests/test_bundles.py` → PASS.

- [ ] **Step 5: Commit**

```bash
git add tether/__init__.py README.md CHANGELOG.md tests/test_bundles.py
git commit -m "docs: file exchange guide, config rows, and changelog for host integration"
```

### Task 12: Verification and hand-off

**Files:**
- Modify: `docs/superpowers/specs/2026-10-01-host-integration-design.md` (only if the
  implementation diverged during Tasks 7–11)

- [ ] **Step 1: Full suite** — `uv run pytest -p no:warnings`
Expected: 0 failed; skips only the opt-in live model/network tests.

- [ ] **Step 2: Live container suite** — `uv run pytest -p no:warnings tests/test_container_live.py`
Expected: all pass, none skipped (podman is available on the dev machine).

- [ ] **Step 3: Lint** — `uv run ruff check tether tests/test_agent_instructions.py tests/test_isolation.py tests/test_image_readiness.py tests/test_filenames.py tests/test_publish.py tests/test_deliver.py tests/test_inputs.py`
Expected: no findings in these paths (pre-existing findings elsewhere are out of scope).

- [ ] **Step 4: Naming rule** — run the conventions file's pre-commit grep over tracked files
and this branch's new files.
Expected: no hits in files this branch touched (pre-existing hits elsewhere are out of scope).

- [ ] **Step 5: Code review** — run superpowers:requesting-code-review over the branch diff
against `main`, with the security-relevant units called out: `validate_publication`,
`_process_publications`, `_write_output`, `Session.add_input` / `_input_dir`, the inputs mount.

- [ ] **Step 6: Release note for the host** — report the branch name and the commit to pin.
Do **not** bump `__version__` or tag without the user's go-ahead.

## Integration with `feat/security-hardening`

The hardening branch is being implemented in parallel. Expected textual conflicts and their
resolution when the second branch rebases onto the first:

| File | Conflict | Resolution |
|---|---|---|
| `tether/sandbox.py` | Both add `SandboxRuntimeUnavailable` | Identical definition; keep one |
| `tether/sandbox.py` | Hardening returns `(ids, ingest_error)` from `_ingest_new_handles` and adds `ControlPlaneLimits`; this branch adds `publish_error` and `published` | Join all three errors: `(base_error, emit_error, ingest_error, publish_error)`; keep both new `__init__` attributes |
| `tether/session.py` | Both wrap `ContainerSandbox` construction in `_build_sandbox`; hardening adds `limits` | One wrap; keep `_check_isolation` first; pass `limits`; message names both the `local` opt-out and `require_isolation` |
| `tether/config.py` | Hardening flips `backend` default and adds control-plane caps | Keep both; this branch's fields are independent |
| `tether/runtime/tether_sandbox.py` | Hardening slims `save()` | Independent of `publish()`; keep both |
| `tether/handles.py` | Hardening splits writers into describers and adds `adopt()` | Independent of `put_input` / new fields; keep both |
| `tether/api.py`, `tether/manager.py` | Hardening adds `ToolFactory` expansion | Keyword additions on both sides; merge by hand |
| `tests/conftest.py` | Hardening pins `backend="local"` suite-wide | This branch's tests pass explicit backends where it matters; no change needed |
| `tether/sandbox.py` | This branch wraps runs in `self.lock`, renames the body to `_run_script`, and replaces the `pid_counter` token with `secrets.token_hex(8)` | Keep the lock and random token; apply hardening's ingestion changes inside `_run_script` |
| `tether/sandbox_container.py` | This branch adds `--name` and remove-on-timeout | Keep; hardening's `limits` param is independent |

### Handoff note for the hardening branch

The final review of this branch found gaps that also affect code WS1 owns; its spec covers
neither:

1. **Control files other than publish** (`_new_handles_*`, `_emit_*`, `_registry_*`) are still
   created with `write_text` (follows symlinks) and read with blocking `open`. Sandboxed code
   can replace its own `_new_handles` / `_emit` file with a FIFO during a run and hang the
   parent on ingestion. Apply the same pattern as the publish file: create
   `O_CREAT|O_EXCL|O_NOFOLLOW`, read `O_NOFOLLOW|O_NONBLOCK` and require `S_ISREG`.
2. **`run_code` writes into `.scripts/` via `mkstemp(dir=root/.scripts)`**: if sandboxed code
   leaves `.scripts` as a symlink to a host directory, the next `run_code` writes the script
   there before `safe_path` rejects the run. Resolve `.scripts` through `safe_path` and
   require it to equal `root/.scripts` before writing.
3. **Concurrency and timeouts** are fixed on this branch (run lock, container removal on
   timeout); WS1 code should assume runs are serialized by `_OrchestratedSandbox.lock`.
