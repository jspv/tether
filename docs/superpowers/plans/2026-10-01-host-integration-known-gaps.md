# Host Integration Known Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the eight deferred final-review findings (G1–G8) recorded in the host
integration spec's "Known gaps" section.

**Architecture:** Small, local changes on the existing seams: containment-first ordering in
`validate_publication`; self-healing, filesystem-verified input ingestion in `Session`; an
off-loop copy in `Conversation.aadd_input`; bundle-gated sandbox publishing; a shared build
deadline and a named, removable pip-layer container in `container_runtime`.

**Tech Stack:** Python 3.12, pytest, `uv`, podman (live tests only).

**Spec:** `docs/superpowers/specs/2026-10-01-host-integration-design.md` — section
"Known gaps (deferred from final review, 2026-10-01)".

## Global Constraints

- Branch `fix/known-gaps`, in its own git worktree, based on local `main`
  (`1f0ae89`). Run everything through `uv run`; `git checkout -- uv.lock` before committing.
- Changes stay additive for host call sites: `Session.create(config, *, on_publish=None,
  bundles=None)` — `bundles=None` keeps today's behavior.
- **TDD**: failing test first, watch it fail, then implement.
- **Commits only when the user authorizes them.** Each task lists its commit command.
- **Naming hard rule** from the local agent-conventions file applies to every tracked file and
  commit message; run its pre-commit grep before committing.
- **Shared resources:** never remove containers, processes, or volumes except ones this work
  created, by exact name or by a filter that can only match them (`name=tether-`,
  `ancestor=<tether image>`). The podman VM also runs the user's dev containers.
- Test command: `uv run pytest -p no:warnings <paths>`; baseline 409 passed, 8 skipped.

## Review Focus

Inputs the gap descriptions imply but don't spell out, most likely to bite first:

1. **A symlink loop inside the root passed to publish** — expect a `PublishError`, not a
   `RuntimeError`/`OSError` escaping `Session.publish`. Pinned in Task 1
   (`test_symlink_loop_is_a_publish_error`).
2. **`inputs/<id>` planted as a real directory with mode `0555`** — expect `add_input` to
   succeed, not fail on `mkstemp`. Pinned in Task 2 (`test_readonly_planted_input_dir_is_repaired`).
3. **Two concurrent `aadd_input` calls with the same id** — expect one copy and one handle.
   Pinned in Task 3 (`test_concurrent_aadd_input_same_id_copies_once`).
4. **`bundles=()` (empty selection means "all")** — expect sandbox publishing enabled, as
   today. Pinned in Task 4 (`test_empty_bundle_selection_keeps_sandbox_publish`).
5. **`build_timeout_s` already used up by the image build** — expect `SandboxImageError`
   before the pip step, never a zero/negative subprocess timeout. Pinned in Task 5
   (`test_exhausted_deadline_raises_before_pip`).

---

### Task 1: G1 — containment before existence in `validate_publication`

**Files:**
- Modify: `tether/publish.py` (`validate_publication`, its docstring)
- Test: `tests/test_publish.py` (append)

**Interfaces:**
- Produces: unchanged signature; every escape raises `PublishError("path is outside the
  workspace: ...")` regardless of whether the host path exists.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_publish.py`:

```python
def test_escape_errors_do_not_reveal_host_existence(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "exists.txt").write_text("x")
    os.symlink(tmp_path, root / "outputs")          # planted parent-dir symlink
    messages = set()
    for p in (str(tmp_path / "exists.txt"), str(tmp_path / "missing.txt"), "/etc",
              "outputs/exists.txt", "outputs/missing.txt"):
        with pytest.raises(PublishError) as ei:
            validate_publication(root, p, name=None, description=None, source="s",
                                 max_bytes=MAX)
        messages.add(str(ei.value).replace(repr(p), "<p>"))
    assert messages == {"path is outside the workspace: <p>"}


def test_symlink_loop_is_a_publish_error(tmp_path):
    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    _reject(tmp_path, "a")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_publish.py -k "existence or loop"`
Expected: `test_escape_errors_do_not_reveal_host_existence` FAILs (several distinct messages:
"no such file", "refusing to publish a symlink", "outside"). The loop test may already pass
(lstat sees a symlink); it pins that behavior through the reorder.

- [ ] **Step 3: Implement** — in `tether/publish.py`, replace the block from
`unresolved = ...` through the `safe_path` `except` clauses with:

```python
    # Containment first, with one message for every escape. Checking existence first would let
    # sandboxed code probe the host filesystem through which error it gets back.
    try:
        resolved = safe_path(root, path)
    except PathEscapesRootError as e:
        raise PublishError(f"path is outside the workspace: {path!r}") from e
    except (OSError, ValueError, RuntimeError) as e:   # NUL/surrogates, symlink loops
        raise PublishError(f"invalid publication path: {path!r}") from e
    unresolved = candidate if candidate.is_absolute() else root / candidate
    try:
        st = os.lstat(unresolved)
    except (OSError, ValueError) as e:
        raise PublishError(f"no such file: {path!r}") from e
    if stat.S_ISLNK(st.st_mode):
        raise PublishError(f"refusing to publish a symlink: {path!r}")
    if not stat.S_ISREG(st.st_mode):
        raise PublishError(f"not a regular file: {path!r}")
```

and update the docstring's first paragraph to:

```python
    Order matters: ``safe_path`` first, so ``..``, absolute paths outside the root, and
    symlinks (final or parent) that escape all get the same error whether or not the host path
    exists; then ``lstat`` on the *unresolved* in-root path rejects a final-component symlink;
    internal trees and control files are refused; finally the file is opened with
    ``O_NOFOLLOW`` and size + digest are taken from that descriptor, so they describe the bytes
    that were actually checked.
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_publish.py tests/test_deliver.py tests/test_review_fixes.py`
Expected: PASS (all existing rejection tests still reject; only messages changed for escapes)

- [ ] **Step 5: Commit**

```bash
git add tether/publish.py tests/test_publish.py
git commit -m "fix(publish): check containment before existence so errors reveal nothing about host paths"
```

### Task 2: G2, G3, G7 — verified, self-healing, bounded input ingestion

**Files:**
- Modify: `tether/handles.py` (`put_input(..., replace=False)`)
- Modify: `tether/session.py` (`_add_input`, `_input_dir`, new `_input_intact`, module
  helpers `_ensure_real_dir`, `_clear_squatter`, `_force_remove`, `_copy_bounded`)
- Test: `tests/test_handles.py`, `tests/test_inputs.py` (append; update one existing test)

**Interfaces:**
- Produces: `HandleStore.put_input(..., replace: bool = False) -> Handle` — with
  `replace=True`, an existing input's record is overwritten **keeping its handle id**.
- Behavior change: a planted symlink at `inputs/` is now **replaced** (and the upload
  succeeds) rather than raising `ValueError`; the outside target is still never written.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_handles.py`:

```python
def test_put_input_replace_keeps_id_and_persists(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="s", input_id="f1")
    h2 = store.put_input(path="inputs/f1/b.csv", size=2, preview="q", source="s2",
                         input_id="f1", replace=True)
    assert h2.id == h.id and h2.path == "inputs/f1/b.csv"
    assert HandleStore(tmp_path).inputs()["f1"].path == "inputs/f1/b.csv"
```

In `tests/test_inputs.py`, **replace** `test_does_not_write_through_planted_symlinks` with:

```python
def test_planted_inputs_symlink_is_replaced_not_followed(tmp_path):
    sess = _session(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, sess.root / "inputs")
    h = sess.add_input(b"x", input_id="f1", name="a.txt")
    assert list(elsewhere.iterdir()) == []
    assert not (sess.root / "inputs").is_symlink()
    assert (sess.root / h.path).read_bytes() == b"x"
```

and append:

```python
def test_planted_inputs_file_is_replaced(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "inputs").write_text("squatter")
    h = sess.add_input(b"x", input_id="f1", name="a.txt")
    assert (sess.root / h.path).read_bytes() == b"x"


def test_planted_input_id_file_is_replaced(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "inputs").mkdir()
    (sess.root / "inputs" / "f1").write_text("squatter")
    assert (sess.root / sess.add_input(b"x", input_id="f1", name="a.txt").path).exists()


def test_directory_squatting_on_target_name_is_removed(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "inputs" / "f1" / "a.txt" / "nested").mkdir(parents=True)
    h = sess.add_input(b"x", input_id="f1", name="a.txt")
    assert (sess.root / h.path).read_bytes() == b"x"


def test_readonly_planted_input_dir_is_repaired(tmp_path):
    sess = _session(tmp_path)
    d = sess.root / "inputs" / "f1"
    d.mkdir(parents=True)
    d.chmod(0o555)
    try:
        h = sess.add_input(b"x", input_id="f1", name="a.txt")
        assert (sess.root / h.path).read_bytes() == b"x"
    finally:
        d.chmod(0o755)


def _make_writable(p):
    p.chmod(0o644)


def test_deleted_input_is_recopied_under_same_id(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"one", input_id="f1", name="a.txt")
    _make_writable(sess.root / h.path)
    (sess.root / h.path).unlink()
    h2 = sess.add_input(b"two", input_id="f1", name="b.txt")
    assert h2.id == h.id and h2.path == "inputs/f1/b.txt"
    assert (sess.root / h2.path).read_bytes() == b"two"
    assert HandleStore(sess.root).inputs()["f1"].path == "inputs/f1/b.txt"


def test_symlinked_input_file_is_recopied(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"one", input_id="f1", name="a.txt")
    target = sess.root / h.path
    _make_writable(target)
    target.unlink()
    os.symlink("/etc/hosts", target)
    h2 = sess.add_input(b"two", input_id="f1", name="a.txt")
    assert not (sess.root / h2.path).is_symlink()
    assert (sess.root / h2.path).read_bytes() == b"two"


def test_forged_manifest_record_is_not_trusted(tmp_path):
    import json
    sess = _session(tmp_path)
    h = sess.add_input(b"one", input_id="f1", name="a.txt")
    mf = sess.root / "handles" / "_manifest.json"
    data = json.loads(mf.read_text())
    data[h.id]["path"] = "handles/_manifest.json"            # forged by sandboxed code
    mf.write_text(json.dumps(data))
    sess2 = _session(tmp_path)
    h2 = sess2.add_input(b"two", input_id="f1", name="a.txt")
    assert h2.path == "inputs/f1/a.txt" and h2.id == h.id
    assert (sess2.root / h2.path).read_bytes() == b"two"


def test_source_that_grows_past_the_cap_is_rejected(tmp_path, monkeypatch):
    import tether.session as session_mod
    src = tmp_path / "grows.bin"
    src.write_bytes(b"0123456789")
    monkeypatch.setattr(session_mod, "_input_size", lambda source: 1)   # stat'ed when small
    sess = _session(tmp_path, max_input_bytes=4)
    with pytest.raises(ValueError, match="too large"):
        sess.add_input(src, input_id="f1", name="a.bin")
    assert sess.inputs == {}
    assert not any(p.name.startswith(".upload_") for p in (sess.root / "inputs").rglob("*"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -p no:warnings tests/test_handles.py tests/test_inputs.py`
Expected: FAILs on `replace` (TypeError), every planted-entry test (ValueError / OSError),
the deleted/symlinked/forged re-copy tests (stale handle returned), and the grow test (no
error raised).

- [ ] **Step 3: Implement.** In `tether/handles.py`, change `put_input`:

```python
    def put_input(self, *, path: str, size: int, preview: str, source: str, input_id: str,
                  content_type: str | None = None, description: str | None = None,
                  replace: bool = False) -> Handle:
        """Register a host-ingested upload whose file already exists under ``inputs/``.

        Parent-authored (the host wrote the bytes), so it does not go through the sandbox
        adoption path. Idempotent by ``input_id``: an existing input is returned unchanged,
        unless ``replace`` is set, which overwrites its record and keeps its handle id.
        """
        existing = self.inputs().get(input_id)
        if existing is not None and not replace:
            return existing
        try:
            safe_path(self.root, path)
        except PathEscapesRootError as e:
            raise ValueError(f"input path escapes root: {path!r}") from e
        hid = existing.id if existing is not None else self._new_id()
        handle = Handle(id=hid, kind="binary", path=path, source=source, bytes=size,
                        preview=preview, input_id=input_id, content_type=content_type,
                        description=description)
        self._handles[handle.id] = handle
        self._save_manifest()
        return handle
```

In `tether/session.py`, add `from pathlib import Path, PurePosixPath` (replacing the existing
`from pathlib import Path`) and `_COPY_CHUNK = 1024 * 1024` next to `_PREVIEW_READ_BYTES`.
Replace `_add_input` and `_input_dir` with:

```python
    def _add_input(self, source: Path | bytes, *, input_id: str, name: str,
                   content_type: str | None, description: str | None) -> Handle:
        validate_segment(input_id, what="input id")
        existing = self.store.inputs().get(input_id)
        if existing is not None and self._input_intact(existing, input_id):
            return existing
        filename = safe_filename(name, fallback="upload.bin")
        size = _input_size(source)
        if size > self.config.max_input_bytes:
            raise ValueError(f"input too large ({size} bytes > {self.config.max_input_bytes})")

        target_dir = self._input_dir(input_id)
        target = target_dir / filename
        _clear_squatter(target)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".upload_")
        try:
            with os.fdopen(fd, "wb") as out:
                _copy_bounded(source, out, self.config.max_input_bytes)
            os.chmod(tmp, 0o444)
            os.replace(tmp, target)   # replaces (never writes through) a planted symlink
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        ctype = content_type or mimetypes.guess_type(filename)[0]
        return self.store.put_input(
            path=f"{_INPUTS}/{input_id}/{filename}", size=size,
            preview=_input_preview(target, filename, size, ctype),
            source=f"upload:{filename}", input_id=input_id, content_type=ctype,
            description=description, replace=existing is not None)

    def _input_intact(self, handle: Handle, input_id: str) -> bool:
        """Whether a recorded input still points at its own regular file under inputs/<id>/.

        In the container tier the manifest is writable by sandboxed code, so the record is
        checked against the filesystem instead of being trusted.
        """
        parts = PurePosixPath(handle.path).parts
        if handle.kind != "binary" or len(parts) != 3 or parts[:2] != (_INPUTS, input_id):
            return False
        for rel in (_INPUTS, f"{_INPUTS}/{input_id}"):
            if (self.root / rel).is_symlink():
                return False
        try:
            return stat.S_ISREG(os.lstat(self.root / handle.path).st_mode)
        except OSError:
            return False

    def _input_dir(self, input_id: str) -> Path:
        """Create and return ``<root>/inputs/<input_id>`` as real, writable directories.

        Sandboxed code may have left a symlink, file, or read-only directory at either level;
        under the session lock it is removed (never followed) or repaired, so an upload can
        never be blocked or redirected.
        """
        path = self.root
        for part in (_INPUTS, input_id):
            path = path / part
            _ensure_real_dir(path)
        return path
```

and add module-level helpers after `_input_preview`:

```python
def _ensure_real_dir(path: Path) -> None:
    """Make ``path`` a real directory we can write: replace anything else, never follow it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir()
        return
    if not stat.S_ISDIR(st.st_mode):
        path.unlink()          # symlink, file, FIFO, ...: removed, not followed
        path.mkdir()
        return
    os.chmod(path, 0o755)      # lstat just proved it is a real directory, not a link


def _clear_squatter(target: Path) -> None:
    """Remove a directory squatting on an input's filename (``os.replace`` cannot)."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(target, onexc=_force_remove)   # rmtree does not follow symlinks


def _force_remove(func, path, exc) -> None:
    """rmtree error hook: a read-only directory inside the squatter is made writable."""
    os.chmod(os.path.dirname(path), 0o700)
    func(path)


def _copy_bounded(source: Path | bytes, out, limit: int) -> None:
    """Copy ``source`` into ``out``; raise if more than ``limit`` bytes arrive."""
    if isinstance(source, (bytes, bytearray)):
        out.write(source)      # length already checked against the limit
        return
    copied = 0
    with open(source, "rb") as src:
        while chunk := src.read(_COPY_CHUNK):
            copied += len(chunk)
            if copied > limit:
                raise ValueError(f"input too large (more than {limit} bytes while copying)")
            out.write(chunk)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -p no:warnings tests/test_handles.py tests/test_inputs.py tests/test_review_fixes.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tether/handles.py tether/session.py tests/test_handles.py tests/test_inputs.py
git commit -m "fix(inputs): verify recorded inputs, repair planted entries, bound host-path copies"
```

### Task 3: G6 — `aadd_input` copies off the event loop

**Files:**
- Modify: `tether/conversation.py` (`aadd_input`)
- Test: `tests/test_inputs.py` (append)

**Interfaces:**
- Consumes: `Session.add_input` (Task 2).
- Produces: unchanged signature; the copy runs in `asyncio.to_thread` under the turn lock.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_inputs.py`:

```python
def test_aadd_input_does_not_block_event_loop(tmp_path, monkeypatch):
    import time

    async def run():
        conv = await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]), bundles=("code",))
        real = conv.session.add_input

        def slow(*a, **k):
            time.sleep(0.5)
            return real(*a, **k)

        monkeypatch.setattr(conv.session, "add_input", slow)
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(50):
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        await conv.aadd_input(b"x", input_id="f1", name="a.txt")
        during = ticks
        task.cancel()
        await conv.aclose()
        return during

    assert asyncio.run(run()) >= 10


def test_concurrent_aadd_input_same_id_copies_once(tmp_path):
    async def run():
        conv = await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]), bundles=("code",))
        try:
            a, b = await asyncio.gather(
                conv.aadd_input(b"one", input_id="f1", name="a.txt"),
                conv.aadd_input(b"two", input_id="f1", name="b.txt"))
            return a, b, sorted(p.name for p in (conv.session.root / "inputs/f1").iterdir())
        finally:
            await conv.aclose()

    a, b, files = asyncio.run(run())
    assert a == b and files == ["a.txt"]
```

- [ ] **Step 2: Run** `uv run pytest -p no:warnings tests/test_inputs.py -k aadd`
Expected: `test_aadd_input_does_not_block_event_loop` FAILs (ticks ≈ 0–1);
the concurrency test passes already (turn lock) and pins it.

- [ ] **Step 3: Implement** — in `tether/conversation.py`, `aadd_input` body:

```python
        """Ingest a user upload, waiting for any running turn to finish first. The copy runs
        in a worker thread so a large upload does not stall the event loop."""
        async with self._lock:
            return await asyncio.to_thread(
                self.session.add_input, source, input_id=input_id, name=name,
                content_type=content_type, description=description)
```

- [ ] **Step 4: Run** `uv run pytest -p no:warnings tests/test_inputs.py tests/test_conversation.py` → PASS

- [ ] **Step 5: Commit**

```bash
git add tether/conversation.py tests/test_inputs.py
git commit -m "fix(inputs): run aadd_input's copy off the event loop"
```

### Task 4: G4 + G5 — sandbox `publish()` requires the `deliver` bundle; docstring

> **Decision point (spec G4):** this task implements the recommended gating. If the review
> picks "keep behavior, fix the README" instead, replace Steps 1–3 with a README sentence:
> "Sandbox `publish()` is enabled whenever `on_publish` is set; the `deliver` bundle only adds
> the tools and the instructions." G5 is unaffected.

**Files:**
- Modify: `tether/session.py` (`Session.create(..., bundles=None)`)
- Modify: `tether/conversation.py` (pass `bundles` to `Session.create`)
- Modify: `tether/tools/registry.py` (`run_python` docstring)
- Modify: `README.md` ("Exchanging files with the user")
- Test: `tests/test_deliver.py` (append)

**Interfaces:**
- Produces: `Session.create(config, *, on_publish=None, bundles: tuple[str, ...] | None = None)`;
  sandbox publishing is on iff `on_publish` is set and (`bundles is None` or `"deliver"` is in
  `bundles.selected_bundles(bundles)`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_deliver.py`:

```python
def test_sandbox_publish_disabled_without_deliver_bundle(tmp_path):
    host = _Host()
    sess = Session.create(TetherConfig(root_dir=tmp_path / "r"), on_publish=host,
                          bundles=("code",))
    res = sess.sandbox.run_code(_PUBLISH_CODE)
    assert "publishing is not enabled" in res.stderr and host.calls == []


def test_sandbox_publish_enabled_with_deliver_bundle(tmp_path):
    host = _Host()
    sess = Session.create(TetherConfig(root_dir=tmp_path / "r"), on_publish=host,
                          bundles=("code", "deliver"))
    assert sess.sandbox.run_code(_PUBLISH_CODE).published and len(host.calls) == 1


def test_empty_bundle_selection_keeps_sandbox_publish(tmp_path):
    host = _Host()
    sess = Session.create(TetherConfig(root_dir=tmp_path / "r"), on_publish=host, bundles=())
    sess.sandbox.run_code(_PUBLISH_CODE)
    assert len(host.calls) == 1


def test_conversation_without_deliver_disables_sandbox_publish(tmp_path):
    from tether.conversation import Conversation

    async def run():
        conv = await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]),
                                          bundles=("code",), on_publish=_Host())
        try:
            return conv.session.sandbox.publisher
        finally:
            await conv.aclose()

    assert asyncio.run(run()) is None


def test_run_python_description_mentions_publish(tmp_path):
    sess = _session(tmp_path, _Host())
    assert "publish(" in _tool(sess, "run_python").__doc__
```

- [ ] **Step 2: Run** `uv run pytest -p no:warnings tests/test_deliver.py -k "bundle or description"`
Expected: the `bundles=` tests FAIL with `TypeError: ... unexpected keyword argument 'bundles'`;
the description test FAILs; `test_conversation_without_deliver...` FAILs (publisher set).

- [ ] **Step 3: Implement.** In `tether/session.py`, `Session.create`:

```python
    def create(cls, config: TetherConfig, *, on_publish: OnPublish | None = None,
               bundles: tuple[str, ...] | None = None) -> "Session":
        """Open the workspace. Sandbox ``publish()`` is enabled only when ``on_publish`` is set
        and the ``deliver`` bundle is selected (``bundles=None`` means all bundles)."""
        _check_isolation(config.sandbox)   # before touching the filesystem
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = _build_sandbox(root, store, config.sandbox)
        session = cls(root=root, store=store, sandbox=sandbox, config=config,
                      on_publish=on_publish)
        if hasattr(sandbox, "lock"):
            session.io_lock = sandbox.lock        # one lock for sandbox runs + parent file work
        deliver = bundles is None or _DELIVER in _bundles.selected_bundles(bundles)
        if on_publish is not None and deliver and hasattr(sandbox, "publisher"):
            sandbox.publisher = session.publish   # enables publish() inside run_python
        return session
```

In `tether/conversation.py`: `session = Session.create(config, on_publish=on_publish, bundles=bundles)`.

In `tether/tools/registry.py`, the `run_python` wrapper docstring:

```python
        """Run Python in the sandbox. Give `code` (inline) or `path` (a script file). Scripts
        may use load(id)/save(id, obj)/emit(obj), and publish(path) when file delivery is
        enabled. Returns stdout/result/error/new_handles/published."""
```

In `README.md`, after "Sandbox `publish()` requests are processed after the script exits
cleanly." add: "Both the tools and `publish()` require the `deliver` bundle **and** a
callback; without either, they are not available to the model."

- [ ] **Step 4: Run** `uv run pytest -p no:warnings tests/test_deliver.py tests/test_review_fixes.py tests/test_registry.py tests/test_conversation.py` → PASS

- [ ] **Step 5: Commit**

```bash
git add tether/session.py tether/conversation.py tether/tools/registry.py README.md tests/test_deliver.py
git commit -m "fix(deliver): sandbox publish() requires the deliver bundle; document it for the model"
```

### Task 5: G8 — one build deadline; removable pip-layer container

**Files:**
- Modify: `tether/container_runtime.py` (`ensure_image(..., *, timeout_s=None)`,
  `ensure_layer`, new `_remaining`)
- Modify: `README.md` (image readiness note)
- Test: `tests/test_image_readiness.py` (append)

**Interfaces:**
- Produces: `ensure_image(runtime, tag, config, run=subprocess.run, *, timeout_s: float | None = None)`
  (`None` → `config.build_timeout_s`); `ensure_layer` bounds image build + pip install together
  by one `build_timeout_s` deadline and names its container `tether-layer-<hex>`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_image_readiness.py`:

```python
def test_layer_build_shares_one_deadline(tmp_path, monkeypatch):
    import tether.container_runtime as cr

    clock = [0.0]
    monkeypatch.setattr(cr.time, "monotonic", lambda: clock[0])
    seen = {}

    def run(argv, **kw):
        if argv[1:3] == ["image", "inspect"]:
            return _Proc(1)
        if argv[1] == "build":
            seen["build"] = kw["timeout"]
            clock[0] += 60.0                      # the build took 60 s of the budget
            return _Proc(0)
        if "pip" in argv:
            seen["pip"] = kw["timeout"]
        return _Proc(0)

    ensure_layer("podman", SandboxConfig(pip_packages=("six",), build_timeout_s=100.0),
                 base=tmp_path, run=run)
    assert seen["build"] == 100.0 and seen["pip"] == pytest.approx(40.0)


def test_exhausted_deadline_raises_before_pip(tmp_path, monkeypatch):
    import tether.container_runtime as cr

    clock = [0.0]
    monkeypatch.setattr(cr.time, "monotonic", lambda: clock[0])
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return _Proc(1)
        if argv[1] == "build":
            clock[0] += 150.0
        return _Proc(0)

    with pytest.raises(SandboxImageError, match="timed out"):
        ensure_layer("podman", SandboxConfig(pip_packages=("six",), build_timeout_s=100.0),
                     base=tmp_path, run=run)
    assert not any("pip" in a for a in calls)


def test_layer_timeout_removes_named_container(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if "pip" in argv:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return _Proc(0)

    with pytest.raises(SandboxImageError):
        ensure_layer("podman", SandboxConfig(pip_packages=("six",)), base=tmp_path, run=run)
    pip_argv = next(a for a in calls if "pip" in a)
    name = pip_argv[pip_argv.index("--name") + 1]
    assert name.startswith("tether-layer-")
    assert ["podman", "rm", "-f", name] in calls
```

- [ ] **Step 2: Run** `uv run pytest -p no:warnings tests/test_image_readiness.py -k "deadline or named"`
Expected: FAIL (`cr.time` missing / pip timeout is 100 / no `--name`).

- [ ] **Step 3: Implement.** In `tether/container_runtime.py` add `import secrets` and
`import time`, and `_RM_TIMEOUT_S = 30.0` next to `_INSPECT_TIMEOUT_S`. Change
`ensure_image`'s signature and build call:

```python
def ensure_image(runtime: str, tag: str, config: SandboxConfig,
                 run: Callable = subprocess.run, *, timeout_s: float | None = None) -> None:
```

```python
    budget = config.build_timeout_s if timeout_s is None else timeout_s
    try:
        proc = run(build, capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired as e:
        raise SandboxImageError(
            f"building sandbox image {tag} timed out after {budget:.0f}s "
            f"(can the runtime reach the base-image registry?)") from e
```

Add after `build_command_hint`:

```python
def _remaining(deadline: float, what: str) -> float:
    """Seconds left before ``deadline``; raises once the shared build budget is spent."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise SandboxImageError(f"{what} timed out (build_timeout_s exhausted)")
    return left
```

In `ensure_layer`, replace from `target.mkdir(...)` through the pip `except` block with:

```python
    target.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + config.build_timeout_s   # one budget for build + install
    tag = image_tag(config.preinstalled)
    ensure_image(runtime, tag, config, run,
                 timeout_s=_remaining(deadline, f"building sandbox image {tag}"))
    name = f"tether-layer-{secrets.token_hex(8)}"
    cmd = [runtime, "run", "--rm", "--name", name]
    # See ContainerSandbox._build_run_argv: rootless podman maps the host user to container-root,
    # so the host-owned /layer bind mount is unwritable to the hardening --user. keep-id maps the
    # host uid through so --user owns the mount. (podman-only; docker rootless rejects it.)
    if runtime == "podman":
        cmd += ["--userns=keep-id"]
    cmd += ["--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{target}:/layer:rw", tag,
            "pip", "install", "--no-cache-dir", "--target", "/layer", *config.pip_packages]
    try:
        proc = run(cmd, capture_output=True, text=True,
                   timeout=_remaining(deadline, f"provisioning pip_packages into {target}"))
    except subprocess.TimeoutExpired as e:
        try:   # killing the CLI does not stop the container; remove it by name
            run([runtime, "rm", "-f", name], capture_output=True, timeout=_RM_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise SandboxImageError(
            f"provisioning pip_packages into {target} timed out after "
            f"{config.build_timeout_s:.0f}s") from e
```

In `README.md`, append to the "Image readiness" paragraph: "`build_timeout_s` is one budget
for the image build and the pip layer together; a timed-out pip-layer container is removed, but
a timed-out image build may finish in the runtime's background."

- [ ] **Step 4: Run** `uv run pytest -p no:warnings tests/test_image_readiness.py tests/test_container_runtime.py` → PASS

- [ ] **Step 5: Commit**

```bash
git add tether/container_runtime.py README.md tests/test_image_readiness.py
git commit -m "fix(sandbox): one build deadline for image + layer; remove timed-out layer container"
```

### Task 6: Verification, spec status, review

**Files:**
- Modify: `docs/superpowers/specs/2026-10-01-host-integration-design.md` (Known gaps status)
- Modify: `CHANGELOG.md`

- [ ] **Step 1:** In the spec's "Known gaps" section, change "Status: **open**" to
"Status: **fixed** on `fix/known-gaps`" and record the G4 outcome (gated, or README-only).

- [ ] **Step 2:** Append to `CHANGELOG.md` under `## Unreleased`:

```markdown
### Known-gap fixes
- Publish errors no longer reveal whether a host path exists (containment is checked first).
- `add_input` re-copies an input whose recorded file is missing, replaced, or forged (same
  handle id), repairs entries planted under `inputs/`, and bounds host-path copies.
- `aadd_input` copies in a worker thread.
- Sandbox `publish()` requires the `deliver` bundle (`Session.create(..., bundles=None)` keeps
  the previous behavior for direct callers).
- `build_timeout_s` is one budget for the image build and pip layer; a timed-out pip-layer
  container is removed.
```

- [ ] **Step 3: Full suite** — `uv run pytest -p no:warnings` → 0 failed.
- [ ] **Step 4: Live container suite** — `uv run pytest -p no:warnings tests/test_container_live.py`
→ all pass. Afterwards check `podman ps -q --filter name=tether-` is empty; if not, remove
**only those ids**.
- [ ] **Step 5: Lint + naming** — `uv run ruff check tether tests` (no new findings) and the
conventions file's pre-commit grep (no hits in touched files).
- [ ] **Step 6: Review** — superpowers:requesting-code-review over `main..fix/known-gaps`.
- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-01-host-integration-design.md CHANGELOG.md
git commit -m "docs: mark host-integration known gaps fixed"
```
