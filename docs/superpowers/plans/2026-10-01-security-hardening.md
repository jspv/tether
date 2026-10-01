# Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the project's documented trust boundary real — sandboxed code can no longer
forge the metadata the model sees, egress cannot reach internal addresses, conversations stop
sharing live MCP connections, and real isolation is the default.

**Architecture:** Four independent workstreams over disjoint files. The parent stops trusting
handle metadata written inside the sandbox and derives it from the bytes on disk instead
(reading only the parquet footer and first row group); a new `tether/egress.py` owns all
address policy and is wired into both parent-side egress points; an explicit `ToolFactory`
marker lets each conversation build its own MCP connections; and `SandboxConfig.backend`
flips to `container` with a loud failure when no runtime exists.

**Tech Stack:** Python 3.12, `uv`, pytest, pandas/pyarrow, httpx, Microsoft Agent Framework
(`agent-framework-core`), podman/docker for the container tier.

**Spec:** `docs/superpowers/specs/2026-09-17-security-hardening-design.md`

## Global Constraints

- **TDD, always.** Every unit gets its failing test before implementation. These are
  security-critical and held to the `safe_path` bar.
- **No assistant attribution** in any tracked file — do not name the AI assistant or coding
  tool used to produce this work in code, comments, tests, docs, or commit messages, and do
  not use "CC" as shorthand for one. Describe design lineage neutrally ("single-agent-loop
  harness", "references-not-payloads"). Verify with the command in the checklist below.
- Python **3.12+**. Dependencies are already declared; this plan adds **no new dependency**
  (`pyarrow`, `httpx`, `pandas` are all existing).
- Run tests with `uv run pytest`; lint with `uv run ruff check .`. The suite must stay
  **offline** — no network, no model, no container runtime required. Baseline before starting:
  **242 passed, 6 skipped** (248 collected). Report passed/skipped separately; the
  often-quoted "248" is the collected total, not the pass count.
- Live tests stay gated behind `TETHER_LIVE=1`; container tests stay gated on a runtime
  being present.
- `safe_path` (`tether/paths.py`) is **not modified** by this plan. It is already correct.
- Preserve each tool's existing error convention: `fetch_url` returns
  `{"error", "status", "url"}`, `read_document` returns `{"error", "source"}`. Blocked
  addresses are **returned**, not raised, so the agent can adapt.
- Work on a branch; do not commit to `main`. Commit after every task.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `tether/handles.py` | Modify | Split "write bytes" from "describe bytes"; add `adopt()` — the trust inversion |
| `tether/runtime/tether_sandbox.py` | Modify | Child stops computing metadata; writes a minimal record |
| `tether/sandbox.py` | Modify | Ingest via `adopt()`; bound the control-plane files |
| `tether/config.py` | Modify | New caps on `TetherConfig`; new `FetchConfig` fields; backend default flip |
| `tether/egress.py` | **Create** | All address policy: `validate_url`, `guarded_get`, `BlockedAddressError` |
| `tether/tools/fetch.py` | Modify | Route through `guarded_get` |
| `tether/tools/documents.py` | Modify | Fetch URLs through the guard; hand Docling a local path only |
| `tether/api.py` | Modify | `ToolFactory` / `tool_factory`; expand factories in `asolve` |
| `tether/manager.py` | Modify | Expand factories per conversation |
| `tether/session.py` | Modify | Raise `SandboxRuntimeUnavailable` when no runtime |
| `tether/__init__.py` | Modify | Export the new public names |
| `tests/conftest.py` | **Create** | Autouse fixture pinning `backend="local"` for the suite |
| `tests/test_handles.py` | Modify | `adopt()` validation, immutability, forged-metadata correction |
| `tests/test_sandbox.py` | Modify | Ingestion via adopt; control-plane bounds |
| `tests/test_egress.py` | **Create** | Address classification, allowlist, redirect hops |
| `tests/tools/test_fetch.py` | Modify | Blocked address returns a structured error |
| `tests/tools/test_documents.py` | Modify | URL source is fetched, Docling gets a local path |
| `tests/test_api.py` | Modify | `tool_factory` round-trip |
| `tests/test_manager.py` | Modify | Two conversations get distinct tool instances |
| `tests/test_config.py` | Modify | New defaults, including `backend == "container"` |

---

# Phase 1 — Zero-trust handle metadata

### Task 1: Split describing from writing in `HandleStore`

Pure refactor plus the footer-only parquet reader. No behavior change — existing tests must
stay green, which is the point of doing this first.

**Files:**
- Modify: `tether/handles.py:92-124` (the `_write_*` methods)
- Test: `tests/test_handles.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HandleStore._describe_dataframe(path: Path) -> dict[str, Any]`,
  `_describe_textual(path: Path) -> dict[str, Any]` (serves BOTH the `json` and `text` kinds —
  their descriptions are identical, so there is one body, not two copies), and
  `_describe_binary(path: Path) -> dict[str, Any]`. Each returns a dict with keys
  `bytes`, `preview`, and for dataframes additionally `schema`, `n_rows`, `n_cols`.
  Task 2 dispatches to all three.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_handles.py`:

```python
import pandas as pd

from tether.handles import HandleStore


def test_describe_dataframe_matches_put(tmp_path):
    """A described file and a put() handle agree exactly — the parity property that
    replaces the hand-maintained duplication in runtime/tether_sandbox.py."""
    store = HandleStore(tmp_path / "r")
    df = pd.DataFrame({"i": range(10), "s": ["a"] * 10, "f": [1.5] * 10})
    handle = store.put(df, source="t")

    described = store._describe_dataframe(store.root / handle.path)

    assert described["schema"] == handle.schema
    assert described["preview"] == handle.preview
    assert described["n_rows"] == handle.n_rows == 10
    assert described["n_cols"] == handle.n_cols == 3
    assert described["bytes"] == handle.bytes


def test_describe_dataframe_reads_only_first_row_group(tmp_path):
    """A 500MB handle must not be materialized to describe it: schema and row count come
    from the footer, the preview from row group 0 only."""
    import pyarrow.parquet as pq

    store = HandleStore(tmp_path / "r")
    path = store.root / "handles" / "big.parquet"
    pd.DataFrame({"n": range(1000)}).to_parquet(path, row_group_size=100)

    read_groups = []
    real_read_row_group = pq.ParquetFile.read_row_group

    def spy(self, i, *a, **kw):
        read_groups.append(i)
        return real_read_row_group(self, i, *a, **kw)

    pq.ParquetFile.read_row_group = spy
    try:
        described = store._describe_dataframe(path)
    finally:
        pq.ParquetFile.read_row_group = real_read_row_group

    assert read_groups == [0]                      # never touched groups 1..9
    assert described["n_rows"] == 1000
    assert described["preview"].startswith("n\n0\n1\n2\n3\n4\n")
    assert "... (5 of 1000 rows)" in described["preview"]


def test_describe_dataframe_empty_frame(tmp_path):
    store = HandleStore(tmp_path / "r")
    df = pd.DataFrame({"a": pd.Series([], dtype="int64")})
    handle = store.put(df, source="t")
    described = store._describe_dataframe(store.root / handle.path)
    assert described["n_rows"] == 0
    assert described["preview"] == handle.preview


def test_describe_text_and_json_bound_the_preview(tmp_path):
    store = HandleStore(tmp_path / "r")
    text_path = store.root / "handles" / "x.txt"
    text_path.write_text("z" * 10_000, encoding="utf-8")
    assert len(store._describe_textual(text_path)["preview"]) == 800

    json_path = store.root / "handles" / "x.json"
    json_path.write_text('["' + "z" * 10_000 + '"]', encoding="utf-8")
    assert len(store._describe_textual(json_path)["preview"]) == 800
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_handles.py -k describe -v`
Expected: FAIL — `AttributeError: 'HandleStore' object has no attribute '_describe_dataframe'`

- [ ] **Step 3: Add the describers and rewrite `_write_*` to use them**

In `tether/handles.py`, add the four describers. The parquet one reads the **footer** for
schema and row count and only row group 0 for the preview — verified to produce byte-identical
output to today's pandas path for mixed, small, empty, and nullable-`Int64` frames:

```python
    def _describe_dataframe(self, path: Path) -> dict[str, Any]:
        """Describe a parquet file without materializing it.

        Schema and row count come from the footer; the preview reads only the first row
        group. Row groups are ordered, so row group 0 holds the first ``_PREVIEW_ROWS``
        rows. ``schema_arrow.empty_table().to_pandas()`` yields the pandas dtypes the
        equivalent ``put()`` would report, without reading any data.
        """
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(path)
        n_rows = int(pf.metadata.num_rows)
        schema_df = pf.schema_arrow.empty_table().to_pandas()
        if pf.num_row_groups:
            head = pf.read_row_group(0).slice(0, _PREVIEW_ROWS).to_pandas()
            preview = head.to_csv(index=False)
        else:
            preview = ""
        if n_rows > _PREVIEW_ROWS:
            preview += f"... ({_PREVIEW_ROWS} of {n_rows} rows)"
        return {
            "bytes": path.stat().st_size,
            "preview": preview,
            "schema": {c: str(t) for c, t in schema_df.dtypes.items()},
            "n_rows": n_rows,
            "n_cols": int(len(schema_df.columns)),
        }

    def _describe_textual(self, path: Path) -> dict[str, Any]:
        """Describe a json or text handle. One body serves both kinds: their summaries are
        identical, and two copies of this would drift the first time one format needed
        different preview handling.

        Reads only the preview window, never the whole file: ``adopt`` routes
        sandbox-authored files through here, and the container tier does not cap how large
        a file the child may write. ``f.read(n)`` on a text stream returns n *characters*
        with multibyte sequences handled, and ``st_size`` is the file's byte length -- which
        for the UTF-8 files ``put`` writes equals the encoded length of the text.
        """
        with path.open(encoding="utf-8") as f:
            preview = f.read(_PREVIEW_CHARS)
        return {"bytes": path.stat().st_size, "preview": preview}

    def _describe_binary(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        ext = path.suffix or ".bin"
        return {"bytes": size, "preview": f"<binary file, {size} bytes, {ext}>"}
```

Then rewrite the writers to write-then-describe, so one code path produces metadata:

```python
    def _write_binary(self, hid: str, data: bytes, source: str, ext: str | None) -> Handle:
        # Store raw bytes intact so the file (xls/pdf/image/...) stays readable by pandas,
        # Docling, etc. The extension is preserved so libraries can infer the format.
        rel = f"handles/{hid}{ext or '.bin'}"
        path = self.root / rel
        path.write_bytes(bytes(data))
        return Handle(id=hid, kind="binary", path=rel, source=source,
                      **self._describe_binary(path))

    def _write_dataframe(self, hid: str, df: Any, source: str) -> Handle:
        rel = f"handles/{hid}.parquet"
        path = self.root / rel
        df.to_parquet(path)
        return Handle(id=hid, kind="dataframe", path=rel, source=source,
                      **self._describe_dataframe(path))

    def _write_json(self, hid: str, obj: Any, source: str) -> Handle:
        # ``default=str`` keeps non-JSON-native types (datetime, Decimal, ...) from
        # crashing serialization, but they round-trip back as strings via get().
        rel = f"handles/{hid}.json"
        path = self.root / rel
        path.write_text(json.dumps(obj, default=str), encoding="utf-8")
        return Handle(id=hid, kind="json", path=rel, source=source,
                      **self._describe_textual(path))

    def _write_text(self, hid: str, obj: str, source: str) -> Handle:
        rel = f"handles/{hid}.txt"
        path = self.root / rel
        path.write_text(obj, encoding="utf-8")
        return Handle(id=hid, kind="text", path=rel, source=source,
                      **self._describe_textual(path))
```

- [ ] **Step 4: Run the full suite — this is a refactor, nothing may regress**

Run: `uv run pytest -q`
Expected: 248 passed, 6 skipped (the new describe tests bring the count up; nothing fails)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tether/handles.py tests/test_handles.py
git commit -m "refactor(handles): split describing from writing; derive parquet metadata from the footer"
```

---

### Task 2: `HandleStore.adopt()` — the trust inversion

**Files:**
- Modify: `tether/handles.py` (add `adopt` next to `register`)
- Test: `tests/test_handles.py`

**Interfaces:**
- Consumes: the four `_describe_*` methods from Task 1.
- Produces: `HandleStore.adopt(*, id: str, kind: str, path: str, source: str) -> Handle`,
  raising `ValueError` on any rejection. Task 3 calls it from `sandbox.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_handles.py`:

```python
import pytest

from tether.handles import HandleStore


def _write_parquet(store, rel="handles/h9.parquet", rows=3):
    import pandas as pd
    pd.DataFrame({"a": range(rows)}).to_parquet(store.root / rel)
    return rel


def test_adopt_ignores_forged_metadata(tmp_path):
    """THE central test: a child may claim anything; the store reports the truth."""
    store = HandleStore(tmp_path / "r")
    rel = _write_parquet(store, rows=3)

    handle = store.adopt(id="h9", kind="dataframe", path=rel, source="run_python")

    assert handle.n_rows == 3                      # not whatever a child claimed
    assert handle.n_cols == 1
    assert handle.schema == {"a": "int64"}
    assert handle.preview.startswith("a\n0\n1\n2\n")
    assert handle.bytes == (store.root / rel).stat().st_size


def test_adopt_rejects_existing_id(tmp_path):
    """Handles are immutable: sandboxed code cannot repoint h1 at different bytes."""
    store = HandleStore(tmp_path / "r")
    original = store.put({"real": True}, source="trusted")
    rel = _write_parquet(store, rel="handles/evil.parquet")

    with pytest.raises(ValueError, match="already exists"):
        store.adopt(id=original.id, kind="dataframe", path=rel, source="run_python")

    assert store.summary(original.id) == original.summary()   # untouched


def test_adopt_rejects_path_escaping_root(tmp_path):
    store = HandleStore(tmp_path / "r")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes root"):
        store.adopt(id="h9", kind="text", path="../outside.txt", source="run_python")


def test_adopt_rejects_missing_file(tmp_path):
    store = HandleStore(tmp_path / "r")
    with pytest.raises(ValueError, match="not a regular file"):
        store.adopt(id="h9", kind="text", path="handles/nope.txt", source="run_python")


def test_adopt_rejects_directory(tmp_path):
    store = HandleStore(tmp_path / "r")
    (store.root / "handles" / "adir").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        store.adopt(id="h9", kind="text", path="handles/adir", source="run_python")


def test_adopt_rejects_symlink(tmp_path):
    """A symlink resolving inside the root still passes safe_path, so it is rejected
    by the regular-file check instead."""
    store = HandleStore(tmp_path / "r")
    target = store.root / "handles" / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = store.root / "handles" / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="not a regular file"):
        store.adopt(id="h9", kind="text", path="handles/link.txt", source="run_python")


def test_adopt_rejects_unknown_kind(tmp_path):
    store = HandleStore(tmp_path / "r")
    p = store.root / "handles" / "x.txt"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown handle kind"):
        store.adopt(id="h9", kind="pickle", path="handles/x.txt", source="run_python")


def test_adopt_bounds_preview_of_huge_file(tmp_path):
    store = HandleStore(tmp_path / "r")
    p = store.root / "handles" / "big.txt"
    p.write_text("z" * 5_000_000, encoding="utf-8")
    handle = store.adopt(id="h9", kind="text", path="handles/big.txt", source="run_python")
    assert len(handle.preview) == 800          # context cannot be flooded
    assert handle.bytes == 5_000_000


def test_adopt_and_put_produce_identical_summaries(tmp_path):
    import pandas as pd
    store = HandleStore(tmp_path / "r")
    df = pd.DataFrame({"a": range(7), "b": ["x"] * 7})
    put_handle = store.put(df, source="same")

    other = HandleStore(tmp_path / "r2")
    rel = "handles/h1.parquet"
    df.to_parquet(other.root / rel)
    adopted = other.adopt(id="h1", kind="dataframe", path=rel, source="same")

    assert adopted.summary() == put_handle.summary()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_handles.py -k adopt -v`
Expected: FAIL — `AttributeError: 'HandleStore' object has no attribute 'adopt'`

- [ ] **Step 3: Implement `adopt`**

Add to `tether/handles.py`, directly after `register`:

```python
    # json and text describe identically, so both kinds dispatch to the one shared body.
    _DESCRIBERS = {"dataframe": "_describe_dataframe", "json": "_describe_textual",
                   "text": "_describe_textual", "binary": "_describe_binary"}

    def adopt(self, *, id: str, kind: str, path: str, source: str) -> Handle:
        """Register a file written by sandboxed code, deriving its metadata here.

        The child reports only what it alone knows -- which file it wrote, and why. Every
        field that reaches model context (preview, bytes, schema, n_rows, n_cols) is
        computed from the bytes on disk, so a hostile child cannot describe its output
        falsely. Handles are immutable: adopting an id that already exists is refused, so
        the record for a handle the child did not create cannot be repointed.
        """
        if id in self._handles:
            raise ValueError(f"handle id {id!r} already exists; handles are immutable")
        describer = self._DESCRIBERS.get(kind)
        if describer is None:
            raise ValueError(f"unknown handle kind: {kind!r}")
        resolved = safe_path(self.root, path)  # raises PathEscapesRootError -> ValueError subclass
        # A directory, symlink, FIFO or device would hang or mislead the describer. safe_path
        # already resolved symlinks, so compare against the unresolved path to catch them.
        if not resolved.is_file() or (self.root / path).is_symlink():
            raise ValueError(f"handle record path is not a regular file: {path!r}")
        described = getattr(self, describer)(resolved)
        handle = Handle(id=id, kind=kind, path=path, source=source, **described)
        self._handles[id] = handle
        self._advance_counter(id)
        self._save_manifest()
        return handle
```

Note `PathEscapesRootError` already subclasses `ValueError` (`paths.py:8`), so the
`escapes root` test passes without extra wrapping.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_handles.py -v`
Expected: PASS, all tests in the file

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tether/handles.py tests/test_handles.py
git commit -m "feat(handles): add adopt() — derive sandbox handle metadata, reject id reuse"
```

---

### Task 3: Slim the child record and ingest via `adopt()`

**Files:**
- Modify: `tether/runtime/tether_sandbox.py:40-70` (`save`)
- Modify: `tether/sandbox.py:142-158` (`_ingest_new_handles`)
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `HandleStore.adopt(...)` from Task 2.
- Produces: the child's new-handles record shape — exactly
  `{"id": str, "kind": str, "path": str, "source": str}`. Task 4 bounds how many of these
  are read.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sandbox.py`:

```python
import json

from tether.config import SandboxConfig
from tether.handles import HandleStore
from tether.sandbox import LocalSubprocessSandbox


def _sandbox(tmp_path):
    root = tmp_path / "r"
    store = HandleStore(root)
    return LocalSubprocessSandbox(root=root, store=store, config=SandboxConfig()), store


def test_child_record_carries_no_metadata(tmp_path):
    """The child reports id/kind/path/source and nothing else."""
    sb, store = _sandbox(tmp_path)
    res = sb.run_code(
        "import pandas as pd\n"
        "from tether_sandbox import save\n"
        "save('h1', pd.DataFrame({'a': [1, 2, 3]}))\n"
    )
    assert res.error is None, res.error
    assert res.new_handles == ["h1"]
    # metadata came from the parent, computed from the file
    assert store.summary("h1")["n_rows"] == 3
    assert store.summary("h1")["schema"] == {"a": "int64"}


def test_forged_child_metadata_is_overridden(tmp_path):
    """A child writing a record by hand cannot make the store report false metadata."""
    sb, store = _sandbox(tmp_path)
    res = sb.run_code(
        "import json, os\n"
        "import pandas as pd\n"
        "pd.DataFrame({'a': range(1000)}).to_parquet(os.path.join('handles', 'h1.parquet'))\n"
        "rec = {'id': 'h1', 'kind': 'dataframe', 'path': 'handles/h1.parquet',\n"
        "       'source': 'run_python', 'n_rows': 2, 'preview': 'all clean!',\n"
        "       'bytes': 10, 'schema': {'a': 'string'}}\n"
        "open(os.environ['TETHER_NEW_HANDLES'], 'a').write(json.dumps(rec) + '\\n')\n"
    )
    assert res.error is None, res.error
    summary = store.summary("h1")
    assert summary["n_rows"] == 1000              # not the claimed 2
    assert summary["preview"] != "all clean!"
    assert summary["schema"] == {"a": "int64"}    # not the claimed string
    assert summary["bytes"] > 10


def test_child_cannot_repoint_an_existing_handle(tmp_path):
    """Adopting an existing id is refused; ingestion stays tolerant and continues."""
    sb, store = _sandbox(tmp_path)
    original = store.put({"trusted": True}, source="parent")
    res = sb.run_code(
        "import json, os\n"
        "open(os.path.join('handles', 'evil.txt'), 'w').write('attacker data')\n"
        f"rec = {{'id': {original.id!r}, 'kind': 'text', 'path': 'handles/evil.txt',\n"
        "       'source': 'run_python'}\n"
        "open(os.environ['TETHER_NEW_HANDLES'], 'a').write(json.dumps(rec) + '\\n')\n"
    )
    assert res.error is None, res.error
    assert res.new_handles == []                                  # rejected
    assert store.summary(original.id) == original.summary()       # untouched


def test_one_corrupt_record_does_not_abort_ingestion(tmp_path):
    sb, store = _sandbox(tmp_path)
    res = sb.run_code(
        "import json, os\n"
        "open(os.path.join('handles', 'a.txt'), 'w').write('a')\n"
        "f = open(os.environ['TETHER_NEW_HANDLES'], 'a')\n"
        "f.write('{not json\\n')\n"
        "f.write(json.dumps({'id': 'h1', 'kind': 'text', 'path': 'handles/a.txt',\n"
        "                    'source': 'run_python'}) + '\\n')\n"
        "f.close()\n"
    )
    assert res.new_handles == ["h1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -k "forged or repoint or carries_no_metadata" -v`
Expected: FAIL — forged metadata is currently trusted, so `n_rows == 2` and
`preview == "all clean!"`

- [ ] **Step 3: Slim the child's `save()`**

Replace the body of `save` in `tether/runtime/tether_sandbox.py` (deleting all metadata
computation, the `_PREVIEW_CHARS`/`_PREVIEW_ROWS` constants, and the stale
"kept in sync by hand" comment):

```python
def save(handle_id: str, obj: Any, source: str = "run_python") -> str:
    """Write ``obj`` as a handle file and tell the parent it exists.

    Only id/kind/path/source are reported: the parent derives every field that reaches
    model context from the bytes on disk, so there is nothing to keep in sync here and
    nothing this side can misreport.
    """
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        kind, rel = "dataframe", f"handles/{handle_id}.parquet"
        obj.to_parquet(_ROOT / rel)
    elif isinstance(obj, (dict, list)):
        kind, rel = "json", f"handles/{handle_id}.json"
        (_ROOT / rel).write_text(json.dumps(obj, default=str), encoding="utf-8")
    else:
        kind, rel = "text", f"handles/{handle_id}.txt"
        (_ROOT / rel).write_text(str(obj), encoding="utf-8")

    with _NEW.open("a") as f:
        f.write(json.dumps({"id": handle_id, "kind": kind, "path": rel,
                            "source": source}) + "\n")
    return handle_id
```

- [ ] **Step 4: Ingest through `adopt()`**

Replace `_ingest_new_handles` in `tether/sandbox.py`:

```python
    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        """Adopt handles the child wrote.

        Records are untrusted: ``adopt`` derives all metadata from the file itself and
        refuses an id that already exists. Tolerant by design -- a rejected or corrupt
        record is skipped, not fatal, so one bad record cannot abort ingestion or leave
        the store inconsistent.
        """
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                handle = self.store.adopt(id=rec["id"], kind=rec["kind"],
                                          path=rec["path"], source=rec.get("source", "run_python"))
                ids.append(handle.id)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                continue
        return ids
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. If a pre-existing test asserted a child-supplied preview verbatim,
update it to assert the derived value — that is the intended behavior change.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add tether/runtime/tether_sandbox.py tether/sandbox.py tests/test_sandbox.py
git commit -m "feat(sandbox): adopt child handles with parent-derived metadata"
```

---

### Task 4: Bound the control plane

**Files:**
- Modify: `tether/config.py` (three new `TetherConfig` fields)
- Modify: `tether/sandbox.py` (emit size check; new-handles read + record caps)
- Test: `tests/test_sandbox.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `_ingest_new_handles` from Task 3.
- Produces: `TetherConfig.max_emit_bytes: int = 1048576`,
  `TetherConfig.max_control_bytes: int = 8388608`, `TetherConfig.max_new_handles: int = 256`.
  `_OrchestratedSandbox.__init__` gains an optional `limits` argument carrying them.

Note: `_OrchestratedSandbox` currently receives only `SandboxConfig`, but these caps live on
`TetherConfig` by design (they bound the parent↔child channel, not a backend). `Session`
builds the sandbox, so pass them explicitly rather than reaching for a global.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from tether.config import TetherConfig


def test_control_plane_caps_have_defaults():
    cfg = TetherConfig()
    assert cfg.max_emit_bytes == 1024 * 1024
    assert cfg.max_control_bytes == 8 * 1024 * 1024
    assert cfg.max_new_handles == 256
```

Add to `tests/test_sandbox.py`:

```python
from tether.config import SandboxConfig
from tether.handles import HandleStore
from tether.sandbox import ControlPlaneLimits, LocalSubprocessSandbox


def _tiny_limits_sandbox(tmp_path, **kw):
    root = tmp_path / "r"
    store = HandleStore(root)
    limits = ControlPlaneLimits(**{"max_emit_bytes": 1024, "max_control_bytes": 1024,
                                   "max_new_handles": 2, **kw})
    return LocalSubprocessSandbox(root=root, store=store, config=SandboxConfig(),
                                  limits=limits), store


def test_oversized_emit_is_rejected_not_parsed(tmp_path):
    sb, _ = _tiny_limits_sandbox(tmp_path)
    res = sb.run_code("from tether_sandbox import emit\nemit('x' * 50_000)\n")
    assert res.result is None
    assert "emit payload too large" in res.error


def test_new_handles_record_count_is_capped(tmp_path):
    sb, _ = _tiny_limits_sandbox(tmp_path)
    res = sb.run_code(
        "from tether_sandbox import save\n"
        "for i in range(10):\n"
        "    save(f'h{i}', {'i': i})\n"
    )
    assert len(res.new_handles) == 2                 # max_new_handles
    assert "too many new handles" in (res.error or "")


def test_oversized_new_handles_file_is_bounded(tmp_path):
    sb, _ = _tiny_limits_sandbox(tmp_path, max_new_handles=1000)
    res = sb.run_code(
        "import os\n"
        "open(os.environ['TETHER_NEW_HANDLES'], 'a').write('x' * 20_000)\n"
    )
    assert "control file too large" in (res.error or "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -k "oversized or capped" tests/test_config.py -k caps -v`
Expected: FAIL — `ImportError: cannot import name 'ControlPlaneLimits'`

- [ ] **Step 3: Add the config fields**

In `tether/config.py`, add to `TetherConfig` after `max_output_tokens`:

```python
    # Control-plane bounds (parent <-> sandbox channel). These live here rather than on
    # SandboxConfig because they bound the orchestration channel, not a backend's behavior.
    max_emit_bytes: int = 1024 * 1024          # emit payload cap, checked before parsing
    max_control_bytes: int = 8 * 1024 * 1024   # new-handles file read cap
    max_new_handles: int = 256                 # records adopted per run
```

- [ ] **Step 4: Enforce the bounds in the sandbox**

In `tether/sandbox.py`, add the limits dataclass near `ExecResult`:

```python
@dataclass
class ControlPlaneLimits:
    """Bounds on the parent<->child control channel. Defaults mirror TetherConfig."""
    max_emit_bytes: int = 1024 * 1024
    max_control_bytes: int = 8 * 1024 * 1024
    max_new_handles: int = 256
```

Accept it in `_OrchestratedSandbox.__init__`:

```python
    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None,
                 limits: ControlPlaneLimits | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self.limits = limits or ControlPlaneLimits()
        self._run_counter = 0
```

Replace the emit-reading block in `run_script` (currently `sandbox.py:119-123`):

```python
            result = None
            emit_error = None
            if launched.exit_code == 0 and emit_file.exists():
                size = emit_file.stat().st_size
                if size > self.limits.max_emit_bytes:
                    # Check the size BEFORE parsing: a hostile child must not be able to
                    # flood context (or the parser) through the result channel.
                    emit_error = (f"tether: emit payload too large ({size} bytes > "
                                  f"{self.limits.max_emit_bytes}); save a handle instead")
                else:
                    try:
                        result = json.loads(emit_file.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as e:
                        emit_error = f"tether: malformed emit payload: {e}"
```

`run_script` must surface ingestion problems, so change the ingestion call and error join:

```python
            new_handles, ingest_error = self._ingest_new_handles(new_handles_file)
            base_error = (launched.stderr.strip() or None) if launched.exit_code != 0 else None
            error = "\n".join(p for p in (base_error, emit_error, ingest_error) if p) or None
```

And make `_ingest_new_handles` return the pair, enforcing both caps:

```python
    def _ingest_new_handles(self, new_handles_file: Path) -> tuple[list[str], str | None]:
        """Adopt handles the child wrote; return (ids, error).

        Records are untrusted: ``adopt`` derives all metadata from the file itself and
        refuses an id that already exists. Tolerant by design -- a rejected or corrupt
        record is skipped, not fatal. Two hard bounds stop a hostile child from using this
        channel as a flood: the file is read only up to ``max_control_bytes``, and at most
        ``max_new_handles`` records are adopted.
        """
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids, None
        if new_handles_file.stat().st_size > self.limits.max_control_bytes:
            return ids, (f"tether: control file too large "
                         f"({new_handles_file.stat().st_size} bytes > "
                         f"{self.limits.max_control_bytes}); no handles ingested")
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if len(ids) >= self.limits.max_new_handles:
                return ids, (f"tether: too many new handles (cap "
                             f"{self.limits.max_new_handles}); later records dropped")
            try:
                rec = json.loads(line)
                handle = self.store.adopt(id=rec["id"], kind=rec["kind"],
                                          path=rec["path"], source=rec.get("source", "run_python"))
                ids.append(handle.id)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                continue
        return ids, None
```

- [ ] **Step 5: Pass the limits through `Session`**

In `tether/session.py`, `_build_sandbox` gains the limits and both backends receive them:

```python
def _build_sandbox(root: Path, store: HandleStore, sandbox_config: SandboxConfig,
                   limits: ControlPlaneLimits | None = None) -> SandboxExecutor:
    """Pick the sandbox backend from config."""
    if sandbox_config.backend == "container":
        from .sandbox_container import ContainerSandbox  # local import: optional backend
        return ContainerSandbox(root=root, store=store, config=sandbox_config, limits=limits)
    return LocalSubprocessSandbox(root=root, store=store, config=sandbox_config, limits=limits)
```

and `Session.create` builds them from the `TetherConfig` it already holds:

```python
        limits = ControlPlaneLimits(max_emit_bytes=config.max_emit_bytes,
                                    max_control_bytes=config.max_control_bytes,
                                    max_new_handles=config.max_new_handles)
        sandbox = _build_sandbox(root, store, config.sandbox, limits)
```

Import `ControlPlaneLimits` from `.sandbox` at the top of `session.py`.

- [ ] **Step 6: Forward the limits through `ContainerSandbox`**

`ContainerSandbox` defines its **own** `__init__` (`sandbox_container.py:23`), so it must
accept and forward `limits` or the container tier silently keeps the defaults:

```python
class ContainerSandbox(_OrchestratedSandbox):
    def __init__(self, root: Path | str, store, config: SandboxConfig | None = None,
                 runtime: str | None = None,
                 limits: ControlPlaneLimits | None = None) -> None:
        super().__init__(root, store, config, limits)
        if runtime is not None:
            self._runtime = runtime
        else:
            from .container_runtime import detect_runtime
            self._runtime = detect_runtime(self.config.container_runtime)
```

Import `ControlPlaneLimits` from `.sandbox` in `sandbox_container.py`.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Fix any caller of `_ingest_new_handles` that assumed a bare list.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check .
git add tether/config.py tether/sandbox.py tether/sandbox_container.py tether/session.py \
        tests/test_sandbox.py tests/test_config.py
git commit -m "feat(sandbox): bound the emit and new-handles control channel"
```

---

# Phase 2 — Egress guard

### Task 5: `tether/egress.py` — address policy

**Files:**
- Create: `tether/egress.py`
- Modify: `tether/config.py` (`FetchConfig`: `allow_private_hosts`, `max_redirects`)
- Test: `tests/test_egress.py` (create)

**Interfaces:**
- Consumes: `FetchConfig` from `tether/config.py`.
- Produces: `BlockedAddressError(ValueError)`;
  `validate_url(url: str, cfg: FetchConfig, *, resolve: Callable[[str], list[str]] | None = None) -> None`.
  Task 6 calls `validate_url`; Tasks 7-8 catch `BlockedAddressError`.

The `resolve` parameter is injected so **no test performs DNS**.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_egress.py`:

```python
import pytest

from tether.config import FetchConfig
from tether.egress import BlockedAddressError, validate_url


def _resolver(mapping):
    """Stub DNS: hostname -> list of addresses. No network in tests."""
    def resolve(host: str) -> list[str]:
        return mapping[host]
    return resolve


@pytest.mark.parametrize("addr,label", [
    ("127.0.0.1", "ipv4 loopback"),
    ("::1", "ipv6 loopback"),
    ("::ffff:127.0.0.1", "ipv4-mapped loopback"),
    ("10.0.0.5", "private 10/8"),
    ("172.16.0.1", "private 172.16/12"),
    ("192.168.1.1", "private 192.168/16"),
    ("fd00::1", "ipv6 unique-local"),
    ("169.254.169.254", "cloud metadata"),
    ("169.254.1.1", "link-local"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("100.64.0.1", "RFC6598 CGNAT — no standard flag catches this one"),
])
def test_validate_url_rejects_internal_addresses(addr, label):
    cfg = FetchConfig()
    with pytest.raises(BlockedAddressError):
        validate_url("http://target.example/x", cfg,
                     resolve=_resolver({"target.example": [addr]}))


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_validate_url_allows_public_addresses(addr):
    validate_url("https://example.com/x", FetchConfig(),
                 resolve=_resolver({"example.com": [addr]}))


def test_any_denied_address_blocks_the_host():
    """A hostname resolving to both a public and a private address is denied -- the
    attacker picks which one connect() uses, so one bad answer is enough."""
    with pytest.raises(BlockedAddressError):
        validate_url("https://split.example/x", FetchConfig(),
                     resolve=_resolver({"split.example": ["93.184.216.34", "127.0.0.1"]}))


def test_allowlist_admits_a_named_host():
    cfg = FetchConfig(allow_private_hosts=("intranet.corp",))
    validate_url("http://intranet.corp/report", cfg,
                 resolve=_resolver({"intranet.corp": ["10.1.2.3"]}))


def test_allowlist_is_case_insensitive():
    cfg = FetchConfig(allow_private_hosts=("Intranet.Corp",))
    validate_url("http://intranet.corp/x", cfg,
                 resolve=_resolver({"intranet.corp": ["10.1.2.3"]}))


def test_allowlist_admits_a_cidr():
    cfg = FetchConfig(allow_private_hosts=("10.1.0.0/16",))
    validate_url("http://db.internal/x", cfg,
                 resolve=_resolver({"db.internal": ["10.1.2.3"]}))


def test_cidr_allowlist_does_not_admit_other_private_space():
    cfg = FetchConfig(allow_private_hosts=("10.1.0.0/16",))
    with pytest.raises(BlockedAddressError):
        validate_url("http://other.internal/x", cfg,
                     resolve=_resolver({"other.internal": ["10.9.9.9"]}))


def test_empty_allowlist_admits_nothing_internal():
    assert FetchConfig().allow_private_hosts == ()
    with pytest.raises(BlockedAddressError):
        validate_url("http://x.internal/x", FetchConfig(),
                     resolve=_resolver({"x.internal": ["10.0.0.1"]}))


def test_literal_ip_url_is_checked_without_dns():
    """A URL with a bare IP must be validated too, and must not need a resolver."""
    with pytest.raises(BlockedAddressError):
        validate_url("http://127.0.0.1:8080/admin", FetchConfig())
    validate_url("http://93.184.216.34/", FetchConfig())


def test_unresolvable_host_is_blocked():
    def resolve(host):
        raise OSError("name resolution failed")
    with pytest.raises(BlockedAddressError, match="could not resolve"):
        validate_url("http://nope.invalid/x", FetchConfig(), resolve=resolve)


def test_url_without_host_is_blocked():
    with pytest.raises(BlockedAddressError, match="no host"):
        validate_url("http:///x", FetchConfig())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_egress.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tether.egress'`

- [ ] **Step 3: Add the config fields**

In `tether/config.py`, extend `FetchConfig`:

```python
@dataclass
class FetchConfig:
    max_bytes: int = 10_000_000
    timeout_s: float = 30.0
    allowed_schemes: tuple[str, ...] = ("http", "https")
    # Hostnames or CIDRs exempted from the internal-address denylist. Empty by default:
    # internal data sources are a legitimate use case, but they must be named explicitly.
    allow_private_hosts: tuple[str, ...] = ()
    max_redirects: int = 5
```

- [ ] **Step 4: Implement `tether/egress.py`**

```python
"""Egress policy: the single place that decides whether a URL may be fetched.

The model chooses the URLs this project fetches, and fetched content is itself untrusted,
so egress is a model-controlled capability. This module denies the internal address space
by default and re-validates every redirect hop, so a 302 cannot walk a request inward.

Known residual risk: validation resolves the hostname and then hands the URL to httpx,
which resolves it again -- a DNS entry that changes in between (rebinding) is not caught.
Closing that needs connect-to-validated-IP with a Host override; see the spec's out-of-scope
section.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx

from .config import FetchConfig

# RFC 6598 carrier-grade NAT. Verified: no ipaddress flag reports this range private,
# reserved, or otherwise special, yet it is routinely routable inside cloud and carrier
# networks. Every other special-use range is already covered by the flag checks below.
_EXTRA_DENIED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


class BlockedAddressError(ValueError):
    """Raised when a URL's host resolves to an address egress policy forbids."""


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _is_denied(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if str(addr) in _METADATA_IPS:
        return True
    if any(addr in net for net in _EXTRA_DENIED_NETS):
        return True
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _allowed_by_config(host: str, addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
                       cfg: FetchConfig) -> bool:
    for entry in cfg.allow_private_hosts:
        if entry.lower() == host.lower():
            return True
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue       # a hostname entry, already compared above
    return False


def validate_url(url: str, cfg: FetchConfig, *,
                 resolve: Callable[[str], list[str]] | None = None) -> None:
    """Raise BlockedAddressError unless every address ``url``'s host resolves to is allowed.

    *Every* address must pass: a host answering with both a public and an internal address
    is denied, because the attacker -- not us -- effectively picks which one is connected to.
    ``resolve`` is injectable so tests never perform DNS.
    """
    host = urlparse(url).hostname
    if not host:
        raise BlockedAddressError(f"no host in url: {url!r}")

    try:                                  # a literal IP needs no resolution
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        resolver = resolve or _resolve
        try:
            addrs = resolver(host)
        except OSError as e:
            raise BlockedAddressError(f"could not resolve host {host!r}: {e}") from e
        if not addrs:
            raise BlockedAddressError(f"could not resolve host {host!r}: no addresses")

    for raw in addrs:
        addr = ipaddress.ip_address(raw)
        if _is_denied(addr) and not _allowed_by_config(host, addr, cfg):
            raise BlockedAddressError(
                f"blocked internal address {raw} for host {host!r}; add the host or its "
                f"CIDR to FetchConfig.allow_private_hosts to permit it")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_egress.py -v`
Expected: PASS, all 20+ cases

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add tether/egress.py tether/config.py tests/test_egress.py
git commit -m "feat(egress): deny internal addresses by default, with an explicit allowlist"
```

---

### Task 6: `guarded_get` — re-validate every redirect hop

**Files:**
- Modify: `tether/egress.py`
- Test: `tests/test_egress.py`

**Interfaces:**
- Consumes: `validate_url`, `BlockedAddressError` from Task 5.
- Produces: `guarded_get(url: str, cfg: FetchConfig, *, client: httpx.Client, resolve=None) -> httpx.Response`.
  Tasks 7-8 call it. The caller owns the client's lifecycle.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_egress.py`:

```python
import httpx

from tether.egress import guarded_get


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _public(*hosts):
    return lambda host: ["93.184.216.34"] if host in hosts else ["127.0.0.1"]


def test_guarded_get_returns_a_direct_response():
    def handler(request):
        return httpx.Response(200, text="hello")

    with _client(handler) as c:
        resp = guarded_get("https://example.com/x", FetchConfig(), client=c,
                           resolve=_public("example.com"))
    assert resp.status_code == 200
    assert resp.text == "hello"


def test_guarded_get_follows_a_public_redirect():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://example.com/end"})
        return httpx.Response(200, text="arrived")

    with _client(handler) as c:
        resp = guarded_get("https://example.com/start", FetchConfig(), client=c,
                           resolve=_public("example.com"))
    assert resp.text == "arrived"


def test_guarded_get_blocks_a_redirect_into_loopback():
    """The attack this exists to stop: a public URL 302-ing to an internal address."""
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/admin"})

    with _client(handler) as c:
        with pytest.raises(BlockedAddressError, match="127.0.0.1"):
            guarded_get("https://example.com/start", FetchConfig(), client=c,
                        resolve=_public("example.com"))


def test_guarded_get_blocks_a_redirect_to_the_metadata_ip():
    def handler(request):
        return httpx.Response(302,
                              headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    with _client(handler) as c:
        with pytest.raises(BlockedAddressError):
            guarded_get("https://example.com/start", FetchConfig(), client=c,
                        resolve=_public("example.com"))


def test_guarded_get_resolves_a_relative_redirect_before_validating():
    """A relative Location must be joined against the current URL, or the host check
    would run against an empty host and the hop would escape validation."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(200, text="ok")

    with _client(handler) as c:
        resp = guarded_get("https://example.com/start", FetchConfig(), client=c,
                           resolve=_public("example.com"))
    assert resp.text == "ok"
    assert seen == ["https://example.com/start", "https://example.com/next"]


def test_guarded_get_enforces_max_redirects():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://example.com/loop"})

    with _client(handler) as c:
        with pytest.raises(httpx.HTTPError, match="too many redirects"):
            guarded_get("https://example.com/loop", FetchConfig(max_redirects=3), client=c,
                        resolve=_public("example.com"))


def test_guarded_get_blocks_the_initial_url_before_any_request():
    called = []

    def handler(request):
        called.append(str(request.url))
        return httpx.Response(200)

    with _client(handler) as c:
        with pytest.raises(BlockedAddressError):
            guarded_get("http://10.0.0.1/x", FetchConfig(), client=c, resolve=_public())
    assert called == []          # never left the process
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_egress.py -k guarded -v`
Expected: FAIL — `ImportError: cannot import name 'guarded_get'`

- [ ] **Step 3: Implement `guarded_get`**

Append to `tether/egress.py`:

```python
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def guarded_get(url: str, cfg: FetchConfig, *, client: httpx.Client,
                resolve: Callable[[str], list[str]] | None = None) -> httpx.Response:
    """GET ``url``, validating the initial address and every redirect hop.

    Redirects are followed here rather than by httpx, because httpx would follow them
    without consulting egress policy -- a public URL could then 302 straight into the
    internal network. ``client`` must be configured with ``follow_redirects=False``;
    the caller owns its lifecycle.
    """
    current = url
    for _ in range(cfg.max_redirects + 1):
        validate_url(current, cfg, resolve=resolve)
        resp = client.get(current)
        if resp.status_code not in _REDIRECT_CODES:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        current = urljoin(current, location)  # relative Location -> absolute, then re-validate
    raise httpx.HTTPError(f"too many redirects (> {cfg.max_redirects}) starting at {url!r}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_egress.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add tether/egress.py tests/test_egress.py
git commit -m "feat(egress): add guarded_get with per-hop redirect validation"
```

---

### Task 7: Route `fetch_url` through the guard

**Files:**
- Modify: `tether/tools/fetch.py:28-34` (`_default_client`) and the `fetch_url` request
- Test: `tests/tools/test_fetch.py`

**Interfaces:**
- Consumes: `guarded_get`, `BlockedAddressError` from Tasks 5-6.
- Produces: no new names. `fetch_url`'s contract is unchanged — a blocked address returns
  `{"error", "status": None, "url"}`, like any other network failure.

- [ ] **Step 1: Write the failing tests**

Add to `tests/tools/test_fetch.py`:

```python
import httpx

from tether.config import FetchConfig, TetherConfig
from tether.session import Session
from tether.tools.fetch import fetch_url


def _session(tmp_path, **fetch_kw):
    return Session.create(TetherConfig(root_dir=tmp_path / "r",
                                       fetch=FetchConfig(**fetch_kw)))


def test_fetch_url_returns_structured_error_for_internal_address(tmp_path):
    """Blocked, and reported the way a network failure is -- so the agent can adapt."""
    session = _session(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="should never be reached")),
        follow_redirects=False)
    with client:
        out = fetch_url(session, "http://169.254.169.254/latest/meta-data/", client=client)
    assert out["status"] is None
    assert "blocked internal address" in out["error"]
    assert out["url"] == "http://169.254.169.254/latest/meta-data/"


def test_fetch_url_allows_an_allowlisted_internal_host(tmp_path):
    session = _session(tmp_path, allow_private_hosts=("127.0.0.1",))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="internal report",
                                 headers={"content-type": "text/plain"})),
        follow_redirects=False)
    with client:
        out = fetch_url(session, "http://127.0.0.1/report", client=client)
    assert "error" not in out
    assert out["kind"] == "text"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/tools/test_fetch.py -k "internal or allowlisted" -v`
Expected: FAIL — the first test gets a 200 and stores a handle instead of erroring

- [ ] **Step 3: Wire the guard in**

In `tether/tools/fetch.py`, add the import and stop httpx following redirects itself:

```python
from ..egress import BlockedAddressError, guarded_get
```

```python
def _default_client(cfg: FetchConfig) -> httpx.Client:
    # follow_redirects=False: guarded_get follows hops itself so each one is re-validated
    # against egress policy. Letting httpx follow them would skip that check.
    return httpx.Client(
        timeout=cfg.timeout_s,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
    )
```

Replace the request block inside `fetch_url`:

```python
        try:
            resp = guarded_get(url, cfg, client=client)
        except BlockedAddressError as e:
            return {"error": f"blocked by egress policy: {e}", "status": None, "url": url}
        except httpx.HTTPError as e:
            return {"error": f"request failed: {e}", "status": None, "url": url}
```

Update the docstring line "Follows redirects and sends a browser User-Agent." to:
"Follows redirects, re-validating each hop against egress policy, and sends a browser
User-Agent."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/tools/test_fetch.py -v`
Expected: PASS. Existing tests that inject a client keep working — `guarded_get` uses the
injected client, and a literal-IP or stubbed host is validated without DNS.

If an existing test fetches a non-public literal host (e.g. `http://test/`), give that
session `allow_private_hosts` or switch the URL to a public literal — do **not** weaken the
default.

- [ ] **Step 5: Leave `tools/web.py` alone — deliberately**

Do **not** add the guard to `web_search` / `web_extract`. Our egress target there is
`api.tavily.com`, a fixed public host; the model-supplied URL travels in the request body and
is fetched by Tavily on its own infrastructure, never by this process. Guarding it would
block legitimate extractions without removing any capability from an attacker. This step
exists only so the next reader does not "complete" the work by patching it.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check .
git add tether/tools/fetch.py tests/tools/test_fetch.py
git commit -m "feat(fetch): route fetch_url through the egress guard"
```

---

### Task 8: Stop handing URLs to Docling

**Files:**
- Modify: `tether/tools/documents.py:42-78` (`read_document`)
- Test: `tests/tools/test_documents.py`

**Interfaces:**
- Consumes: `guarded_get`, `BlockedAddressError` from Tasks 5-6.
- Produces: no new public names. `read_document`'s signature and `{"error", "source"}`
  convention are unchanged; internally, a URL source is downloaded first and the converter
  always receives a **local path**.

- [ ] **Step 1: Write the failing tests**

Add to `tests/tools/test_documents.py`:

```python
import httpx

from tether.config import FetchConfig, TetherConfig
from tether.session import Session
from tether.tools.documents import read_document


def _session(tmp_path, **fetch_kw):
    return Session.create(TetherConfig(root_dir=tmp_path / "r",
                                       fetch=FetchConfig(**fetch_kw)))


def test_read_document_blocks_an_internal_url(tmp_path):
    session = _session(tmp_path)
    out = read_document(session, "http://169.254.169.254/latest/meta-data/",
                        convert=lambda src: "should never run")
    assert "error" in out
    assert "blocked by egress policy" in out["error"]


def test_read_document_hands_the_converter_a_local_path_not_a_url(tmp_path, monkeypatch):
    """Docling must never receive the URL: it would follow its own redirects, outside
    the guard. We download through guarded_get and convert the local file."""
    session = _session(tmp_path)
    seen = []

    def fake_guarded_get(url, cfg, *, client, resolve=None):
        seen.append(url)
        return httpx.Response(200, content=b"%PDF-1.4 fake",
                              headers={"content-type": "application/pdf"})

    monkeypatch.setattr("tether.tools.documents.guarded_get", fake_guarded_get)

    converted = []

    def convert(src: str) -> str:
        converted.append(src)
        return "# Title\n\nbody"

    out = read_document(session, "https://example.com/report.pdf", convert=convert)

    assert out["kind"] == "text"
    assert seen == ["https://example.com/report.pdf"]
    assert len(converted) == 1
    assert not converted[0].startswith("http")           # a local path, never the URL
    assert str(session.root) in converted[0]             # and inside the session root


def test_read_document_still_converts_a_workspace_path(tmp_path):
    session = _session(tmp_path)
    (session.root / "doc.txt").write_text("hello", encoding="utf-8")
    converted = []

    def convert(src: str) -> str:
        converted.append(src)
        return "# hello"

    out = read_document(session, "doc.txt", convert=convert)
    assert out["kind"] == "text"
    assert converted[0] == str(session.root / "doc.txt")


def test_read_document_reports_a_download_failure(tmp_path, monkeypatch):
    session = _session(tmp_path)

    def failing(url, cfg, *, client, resolve=None):
        raise httpx.HTTPError("connection reset")

    monkeypatch.setattr("tether.tools.documents.guarded_get", failing)
    out = read_document(session, "https://example.com/x.pdf", convert=lambda s: "")
    assert "could not download" in out["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/tools/test_documents.py -k "internal_url or local_path or download" -v`
Expected: FAIL — today the converter receives the URL verbatim

- [ ] **Step 3: Download through the guard, convert the local file**

In `tether/tools/documents.py`, add imports:

```python
import httpx

from ..egress import BlockedAddressError, guarded_get
```

Replace the source-resolution branch in `read_document`:

```python
    scheme = urlparse(source).scheme
    if scheme in ("http", "https"):
        # Docling fetches URLs itself and follows its own redirects, which egress policy
        # cannot see. Download through the guard instead and convert the local file, so
        # every hop is validated and Docling has exactly one input shape: a local path.
        try:
            downloaded = _download(session, source)
        except BlockedAddressError as e:
            return {"error": f"blocked by egress policy: {e}", "source": source}
        except httpx.HTTPError as e:
            return {"error": f"could not download {source!r}: {e}", "source": source}
        target = str(downloaded)
    elif scheme == "":
        try:
            target = str(safe_path(session.root, source))
        except PathEscapesRootError:
            return {"error": f"path escapes the workspace root: {source!r}", "source": source}
    else:
        return {"error": f"unsupported source scheme {scheme!r}; pass a workspace path or an "
                         "http(s) URL", "source": source}
```

And add the helper above `read_document`:

```python
def _download(session: Session, url: str) -> Path:
    """Fetch ``url`` through the egress guard into the session root; return the local path.

    The filename keeps the URL's extension so Docling can infer the format, and lands under
    a dedicated subdirectory so downloads are not mistaken for user artifacts.
    """
    cfg = session.config.fetch
    suffix = Path(urlparse(url).path).suffix or ".bin"
    dest_dir = session.root / ".documents"
    dest_dir.mkdir(exist_ok=True)

    client = httpx.Client(timeout=cfg.timeout_s, follow_redirects=False,
                          headers={"User-Agent": _USER_AGENT})
    try:
        resp = guarded_get(url, cfg, client=client)
        resp.raise_for_status()
        fd, abspath = tempfile.mkstemp(prefix="doc_", suffix=suffix, dir=dest_dir)
        os.close(fd)
        dest = Path(abspath)
        dest.write_bytes(resp.content[:cfg.max_bytes])
        return dest
    finally:
        client.close()
```

Add the needed imports at the top of the module (`os`, `tempfile`, `Path`) and the
User-Agent constant, matching `fetch.py`:

```python
import os
import tempfile
from pathlib import Path

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
```

Update the module docstring's second line — it currently says URLs are "passed straight to
Docling, which fetches it" — to say they are downloaded through the egress guard first.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/tools/test_documents.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check .
git add tether/tools/documents.py tests/tools/test_documents.py
git commit -m "feat(documents): download URLs through the egress guard, never hand Docling a URL"
```

---

# Phase 3 — Per-conversation tool isolation

### Task 9: `ToolFactory` and per-conversation expansion

**Files:**
- Modify: `tether/api.py` (add `ToolFactory`, `tool_factory`, expand in `asolve`)
- Modify: `tether/manager.py:88-95` (expand per conversation)
- Modify: `tether/__init__.py` (export)
- Test: `tests/test_api.py`, `tests/test_manager.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ToolFactory` (frozen dataclass with field `build: Callable[[], Any]`),
  `tool_factory(build: Callable[[], Any]) -> ToolFactory`, and
  `expand_tools(tools: list) -> list` — the shared helper both `asolve` and
  `SessionManager.aopen` use.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
import pytest

from tether.api import ToolFactory, expand_tools, tool_factory


def test_tool_factory_wraps_a_callable():
    sentinel = object()
    factory = tool_factory(lambda: sentinel)
    assert isinstance(factory, ToolFactory)
    assert factory.build() is sentinel


def test_expand_tools_calls_each_factory_once():
    calls = []

    def build():
        calls.append(1)
        return f"tool-{len(calls)}"

    tools = [tool_factory(build)]
    first, second = expand_tools(tools), expand_tools(tools)

    assert first == ["tool-1"]
    assert second == ["tool-2"]        # a fresh instance per expansion
    assert len(calls) == 2


def test_expand_tools_passes_plain_tools_through_untouched():
    def my_tool(x: int) -> int:
        """A plain python tool is itself callable; it must not be mistaken for a factory."""
        return x

    out = expand_tools([my_tool])
    assert out == [my_tool]            # not called, not unwrapped


def test_expand_tools_handles_a_mixed_list():
    def my_tool() -> str:
        """Doc."""
        return "direct"

    out = expand_tools([my_tool, tool_factory(lambda: "built")])
    assert out == [my_tool, "built"]


def test_tool_factory_rejects_a_non_callable():
    with pytest.raises(TypeError):
        tool_factory("not callable")
```

Add to `tests/test_manager.py`:

```python
import asyncio

from tether import TetherConfig, Tether, tool_factory
from tether.testing import StubChatClient, text


class _FakeMCP:
    """Stands in for a connected MCP server: identity matters, and so does close()."""
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_each_conversation_gets_its_own_tool_instance(tmp_path):
    built = []

    def build():
        instance = _FakeMCP()
        built.append(instance)
        return instance

    # NOTE: this repo has no pytest-asyncio. Async tests wrap an inner `run()` coroutine
    # and drive it with asyncio.run(), matching every other test in this file.
    async def run():
        tether = Tether(TetherConfig(root_dir=tmp_path / "base"),
                        client=StubChatClient([text("x")]),
                        tools=[tool_factory(build)])
        try:
            a = await tether.aopen("conv-a")
            b = await tether.aopen("conv-b")
            assert a is not b
            assert len(built) == 2
            assert built[0] is not built[1]   # no shared connection across conversations

            await tether._sessions().close("conv-a")
            assert built[0].closed is True
            assert built[1].closed is False   # closing one must not close the other's
        finally:
            await tether.aclose_sessions()

    asyncio.run(run())
```

`StubChatClient` takes a **non-empty** script (`StubChatClient(script: list[Content])`
indexes `script[-1]` once exhausted, so `[]` raises `IndexError`); `[text("x")]` is the
convention used throughout `tests/`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py -k "factory or expand" tests/test_manager.py -k own_tool -v`
Expected: FAIL — `ImportError: cannot import name 'ToolFactory' from 'tether.api'`

- [ ] **Step 3: Implement the factory**

In `tether/api.py`, after the `Result` dataclass:

```python
@dataclass(frozen=True)
class ToolFactory:
    """A deferred tool: ``build()`` is called once per conversation.

    Marked explicitly rather than detected, because a plain python tool is itself a
    callable -- sniffing would silently misclassify a user's zero-argument tool as a
    factory and call it at setup time.
    """
    build: Callable[[], Any]


def tool_factory(build: Callable[[], Any]) -> ToolFactory:
    """Wrap a zero-argument callable so each conversation gets its own tool instance.

    Use this for anything stateful or connected -- an MCP server above all. A single live
    MCPTool shared across conversations is owned and closed by whichever session finishes
    first, which is both a lifecycle bug and a cross-conversation leak.
    """
    if not callable(build):
        raise TypeError(f"tool_factory expects a zero-argument callable, got {type(build)!r}")
    return ToolFactory(build)


def expand_tools(tools: list | None) -> list:
    """Resolve ToolFactory entries into fresh instances; pass everything else through."""
    return [t.build() if isinstance(t, ToolFactory) else t for t in (tools or [])]
```

- [ ] **Step 4: Expand at both construction sites**

In `tether/api.py`, `asolve` (currently `api.py:95`):

```python
        conv = await Conversation.acreate(
            id="oneshot", config=self.config, client=self._make_client(),
            tools=expand_tools(self._tools) + expand_tools(tools),
            bundles=self._bundles, reap_on_close=not keep)
```

In `tether/manager.py`, `aopen` (currently `manager.py:92`) — import `expand_tools` from
`.api` inside the method to avoid a circular import, matching how `manager` already defers
imports:

```python
            from .api import expand_tools

            conv = await Conversation.acreate(
                id=conv_id, config=config, client=self._tether._make_client(),
                tools=expand_tools(self._tether._tools) + expand_tools(tools),
                bundles=bundles if bundles is not None else self._tether._bundles,
                reap_on_close=True,
            )
```

- [ ] **Step 5: Export the new names**

In `tether/__init__.py`:

```python
from .api import Tether, Result, ToolFactory, solve, tool_factory
```

and add `"ToolFactory"`, `"tool_factory"` to `__all__` next to `"Tether"`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py tests/test_manager.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check .
git add tether/api.py tether/manager.py tether/__init__.py tests/test_api.py tests/test_manager.py
git commit -m "feat(api): add tool_factory so each conversation builds its own MCP connections"
```

---

# Phase 4 — Isolation by default

### Task 10: Flip the default backend and fail loudly

Last deliberately: the preceding work is validated on the tier every contributor can run,
and this task is a default flip plus a fixture.

**Files:**
- Modify: `tether/config.py` (`SandboxConfig.backend` default)
- Modify: `tether/session.py:169-175` (`_build_sandbox` guard)
- Modify: `tether/sandbox.py` (docstring: "best-effort" → "no isolation")
- Modify: `tether/__init__.py` (export `SandboxRuntimeUnavailable`)
- Create: `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_container_runtime.py`

**Interfaces:**
- Consumes: `_build_sandbox` as modified in Task 4 (it now takes `limits`).
- Produces: `SandboxRuntimeUnavailable(RuntimeError)`, defined in `tether/sandbox.py` and
  re-exported from `tether`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from tether.config import SandboxConfig, TetherConfig


def test_container_is_the_default_backend(monkeypatch):
    """Real isolation by default.

    The suite's autouse fixture pins ``local`` via TETHER_SANDBOX_BACKEND, which would
    otherwise hide a regression in the shipped default -- so this test deletes the
    variable and asserts what a user actually gets.
    """
    monkeypatch.delenv("TETHER_SANDBOX_BACKEND", raising=False)
    assert SandboxConfig().backend == "container"
    assert TetherConfig().sandbox.backend == "container"


def test_sandbox_backend_env_override(monkeypatch):
    monkeypatch.setenv("TETHER_SANDBOX_BACKEND", "local")
    assert SandboxConfig().backend == "local"
    assert SandboxConfig(backend="container").backend == "container"   # explicit arg wins
```

Add to `tests/test_container_runtime.py`:

```python
import pytest

from tether.config import SandboxConfig, TetherConfig
from tether.handles import HandleStore
from tether.sandbox import SandboxRuntimeUnavailable
from tether.session import _build_sandbox


def test_missing_runtime_raises_and_names_the_opt_out(tmp_path, monkeypatch):
    def no_runtime(override, which=None):
        raise RuntimeError("no container runtime found (looked for podman, docker)")

    # ContainerSandbox.__init__ does `from .container_runtime import detect_runtime` at call
    # time, so patching the module attribute is what takes effect.
    monkeypatch.setattr("tether.container_runtime.detect_runtime", no_runtime)
    store = HandleStore(tmp_path / "r")

    with pytest.raises(SandboxRuntimeUnavailable) as excinfo:
        _build_sandbox(tmp_path / "r", store, SandboxConfig(backend="container"))

    message = str(excinfo.value)
    assert 'backend = "local"' in message       # tells the user exactly how to proceed
    assert "no isolation" in message


def test_local_backend_needs_no_runtime(tmp_path):
    store = HandleStore(tmp_path / "r")
    sandbox = _build_sandbox(tmp_path / "r", store, SandboxConfig(backend="local"))
    assert type(sandbox).__name__ == "LocalSubprocessSandbox"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k default_backend tests/test_container_runtime.py -k "missing_runtime or needs_no_runtime" -v`
Expected: FAIL — default is still `"local"`; `SandboxRuntimeUnavailable` does not exist

- [ ] **Step 3: Create the suite-wide fixture first**

This must land before the default flips, or 248 tests start demanding a container runtime.
Create `tests/conftest.py`:

```python
"""Suite-wide defaults.

The shipped default sandbox backend is ``container`` (real isolation). The test suite must
stay offline, fast, and runnable without podman or docker, so every test gets ``local``
unless it asks otherwise. Tests that specifically exercise the container tier build their
own SandboxConfig and are gated on a runtime being present.

This pins the backend through the environment variable the field's default_factory reads.
Do NOT use ``monkeypatch.setattr(SandboxConfig, "backend", "local")`` -- that is a silent
no-op. A dataclass bakes its defaults into ``__init__.__defaults__`` at class-creation
time, so reassigning the class attribute afterwards does not change what
``SandboxConfig()`` produces (verified).

``test_config.py`` asserts the *shipped* default and must therefore delete the variable
rather than rely on this fixture.
"""

import pytest


@pytest.fixture(autouse=True)
def _local_sandbox_by_default(monkeypatch):
    monkeypatch.setenv("TETHER_SANDBOX_BACKEND", "local")
```

- [ ] **Step 4: Add the exception and the guard**

In `tether/sandbox.py`, next to `ExecResult`:

```python
class SandboxRuntimeUnavailable(RuntimeError):
    """Raised when the container backend is selected but no container runtime exists."""
```

And change the `LocalSubprocessSandbox` docstring to state the posture plainly:

```python
class LocalSubprocessSandbox(_OrchestratedSandbox):
    """Runs the script in a scrubbed-env child process with rlimits + a wall-clock timeout.

    **This tier provides no isolation**: the code runs as the host user, sharing the kernel,
    the filesystem beyond the root, and the network. The rlimits and timeout bound resource
    use, not privilege. Use the container backend for a real boundary.
    """
```

In `tether/session.py`, guard the container branch:

```python
def _build_sandbox(root: Path, store: HandleStore, sandbox_config: SandboxConfig,
                   limits: ControlPlaneLimits | None = None) -> SandboxExecutor:
    """Pick the sandbox backend from config (default: container).

    A missing container runtime is a hard error, never a fallback: silently dropping to the
    local tier would hand back a no-isolation sandbox while the caller believes the code is
    contained. The opt-out has to be explicit.
    """
    if sandbox_config.backend == "container":
        from .sandbox_container import ContainerSandbox  # local import: optional backend
        try:
            # ContainerSandbox.__init__ calls detect_runtime itself and raises RuntimeError
            # when no runtime exists, so wrap construction rather than detecting twice.
            return ContainerSandbox(root=root, store=store, config=sandbox_config,
                                    limits=limits)
        except RuntimeError as e:
            raise SandboxRuntimeUnavailable(
                f"{e}\nThe container backend is the default because run_python executes "
                f'model-authored code. Install podman or docker, or set '
                f'TetherConfig.sandbox.backend = "local" to run it with no isolation.'
            ) from e
    return LocalSubprocessSandbox(root=root, store=store, config=sandbox_config, limits=limits)
```

Import `SandboxRuntimeUnavailable` alongside the other `.sandbox` imports in `session.py`.

- [ ] **Step 5: Flip the default**

In `tether/config.py`, add the factory above `SandboxConfig` and use it for the field. A
`default_factory` (rather than a bare literal) is what makes the default pinnable from the
environment — which is how the test suite stays runtime-free and how a deployment can
select a tier without editing code:

```python
def _default_sandbox_backend() -> str:
    """Shipped default: the container tier, i.e. real isolation.

    ``TETHER_SANDBOX_BACKEND`` overrides it. Setting it to ``local`` opts out of isolation
    entirely, so it is only for environments that have made that choice deliberately --
    CI and the test suite, which must run without a container runtime. An explicit
    ``SandboxConfig(backend=...)`` argument still wins over the variable.
    """
    return os.environ.get("TETHER_SANDBOX_BACKEND", "container")
```

```python
    backend: Literal["local", "container"] = field(default_factory=_default_sandbox_backend)
```

`import os` at the top of `config.py` (`field` is already imported). Verified: the shipped
default is `container`, the variable pins `local`, an explicit argument overrides both, and
`dataclasses.replace` preserves the resolved value — which `manager.py:88` relies on when
it rewrites `root_dir` per conversation.

- [ ] **Step 6: Export the exception**

In `tether/__init__.py`, add `SandboxRuntimeUnavailable` to the `.sandbox` import and to
`__all__`.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: 248+ passed, 6 skipped — unchanged counts, because the fixture pins `local`.
Also verify the fixture is actually doing the work:

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, including the new default-backend assertion

- [ ] **Step 8: Pin the backend in the eval harness**

In `evals/run_evals.py`, set `backend="local"` explicitly on the config it builds, with a
one-line comment that evals measure model behavior and should not require a runtime.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check .
git add tether/config.py tether/session.py tether/sandbox.py tether/__init__.py \
        tests/conftest.py tests/test_config.py tests/test_container_runtime.py evals/run_evals.py
git commit -m "feat(sandbox): default to the container backend; fail loudly without a runtime"
```

---

### Task 11: Documentation pass

**Files:**
- Modify: `README.md` (Security and confinement, Sandbox tiers, Configuration, MCP servers)
- Modify: `docs/ROADMAP.md` (move the four items out of "In progress")

**Interfaces:**
- Consumes: every behavior change from Tasks 1-10.
- Produces: no code.

- [ ] **Step 1: Update the sandbox-tier documentation**

In `README.md`, under **Sandbox tiers** and **Security and confinement**:
- Present `container` as the default and `local` as the explicit opt-out.
- Change `local`'s description from "Fast, no dependencies; best-effort isolation" to state
  it provides **no isolation** — code runs as the host user.
- Note that a missing container runtime is a hard error naming the `local` opt-out.
- Add a **Layer 3 — Egress** bullet: internal address space is denied by default, every
  redirect hop is re-validated, and `FetchConfig.allow_private_hosts` opts internal sources
  back in.
- Replace the "Layer 2 — Executed code" text so the default tier described is the container.

- [ ] **Step 2: Document the handle-integrity guarantee**

Add to **Security and confinement** a short bullet, since it is a user-visible property:

> **Handle integrity.** Metadata the model sees for a handle (schema, row count, preview) is
> always derived by the harness from the bytes on disk, never reported by the sandboxed code
> that wrote them, and handles are immutable once created. A script cannot describe its
> output falsely or repoint an existing handle.

- [ ] **Step 3: Update the configuration table**

Add rows for `allow_private_hosts`, `max_redirects`, `max_emit_bytes`, `max_control_bytes`,
and `max_new_handles`, and change the `sandbox` row's note to say the backend now defaults to
`container`.

Document `TETHER_SANDBOX_BACKEND` alongside it: it overrides the default backend, and setting
it to `local` opts out of isolation — intended for CI and test environments that have made
that choice deliberately. State plainly that an explicit `SandboxConfig(backend=...)` wins
over the variable.

Also flag `max_file_size_mb` as **local-tier only**. It is already documented as unenforced
on the container tier, but that tier is now the default — a config field that silently does
nothing under the default backend is a trap for the next reader.

- [ ] **Step 4: Document `tool_factory` in the MCP section**

After the existing stdio MCP example, add the multi-conversation guidance:

```python
from tether import Tether, tool_factory
from agent_framework import MCPStdioTool

# One live MCPTool is fine for a single run. For a host serving many conversations
# (AG-UI, CopilotKit), wrap it so each conversation connects its own server:
h = Tether(tools=[tool_factory(lambda: MCPStdioTool(name="msgraph", command="uv",
                                                    args=["run", "msgraph-mcp"]))])
```

- [ ] **Step 5: Update the roadmap**

In `docs/ROADMAP.md`, remove the four hardening items from **In progress**, note the phase as
delivered with the spec path, and move the two residual items into **Planned** or the
existing deferred list: IP pinning for DNS rebinding, and control-plane relocation as
optional defense-in-depth.

- [ ] **Step 6: Verify no stale claims remain**

```bash
grep -rn "best-effort" README.md tether/
grep -rn "backend.*local" README.md

# No assistant attribution on the shipped surface. The name is assembled at runtime so this
# command does not itself plant the token it searches for. docs/superpowers/ is excluded on
# purpose: the internal specs and plans state the convention and would always self-match.
NAME=$(printf 'cl%sude' 'a')
grep -rilE "$NAME|\bCC\b" tether/ tests/ README.md examples/ evals/
```
Expected: no "best-effort isolation" claims; no doc text calling `local` the default; the
attribution grep returns nothing.

- [ ] **Step 7: Run everything and commit**

```bash
uv run pytest -q
uv run ruff check .
git add README.md docs/ROADMAP.md
git commit -m "docs: document container-by-default, egress policy, and handle integrity"
```

---

## Verification checklist

Before declaring the phase complete, confirm each with actual command output — not inspection:

- [ ] `uv run pytest -q` — all pass, 6 skipped (live/container gated). Record the count.
- [ ] `uv run ruff check .` — clean.
- [ ] `TETHER_LIVE=1 uv run pytest -q` with a container runtime present — the container tier
      still round-trips handles after the control-plane change.
- [ ] `uv run python -c "from tether import TetherConfig; print(TetherConfig().sandbox.backend)"`
      prints `container`.
- [ ] On a machine (or `PATH`) without podman/docker, `uv run tether "hi"` fails with the
      message naming `backend = "local"` — not a traceback, and not a silent local run.
- [ ] `NAME=$(printf 'cl%sude' 'a'); grep -rilE "$NAME|\bCC\b" tether/ tests/ README.md examples/ evals/`
      returns nothing (shipped surface carries no assistant attribution).
