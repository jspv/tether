import pandas as pd
import pytest

from tether.handles import HandleStore


def test_put_and_get_json_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1, "b": [1, 2, 3]}, source="tool:x")
    assert h.kind == "json"
    assert store.get(h.id) == {"a": 1, "b": [1, 2, 3]}


def test_bytes_autodetect_binary_kind(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put(b"\x00\x01\x02 raw bytes", source="s")
    assert h.kind == "binary"


def test_put_and_get_binary_preserves_bytes_and_returns_path(tmp_path):
    from pathlib import Path

    store = HandleStore(tmp_path)
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 fake xls bytes \x00\x01"
    h = store.put(data, source="fetch_url(x.xls)", kind="binary", ext=".xls")
    assert h.kind == "binary"
    assert h.path.endswith(".xls")            # extension preserved for pandas/Docling
    assert "binary" in h.preview.lower()       # no garbled text preview
    p = store.get(h.id)                         # binary get() returns the file path (str)
    assert isinstance(p, str)
    assert Path(p).read_bytes() == data         # bytes intact, not mangled


def test_put_and_get_text_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("hello world", source="tool:x")
    assert h.kind == "text"
    assert store.get(h.id) == "hello world"


def test_put_and_get_dataframe_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    df = pd.DataFrame({"x": [1, 2], "y": [3.0, 4.0]})
    h = store.put(df, source="tool:query")
    assert h.kind == "dataframe"
    assert h.n_rows == 2
    assert h.n_cols == 2
    assert h.schema == {"x": "int64", "y": "float64"}
    pd.testing.assert_frame_equal(store.get(h.id), df)


def test_handle_summary_omits_none_and_includes_preview(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("abc", source="tool:x")
    summary = h.summary()
    assert summary["id"] == h.id
    assert summary["kind"] == "text"
    assert "preview" in summary
    assert "schema" not in summary  # None fields dropped


def test_ids_are_sequential_and_unique(tmp_path):
    store = HandleStore(tmp_path)
    h1 = store.put("a", source="s")
    h2 = store.put("b", source="s")
    assert (h1.id, h2.id) == ("h1", "h2")


def test_files_are_written_under_root_handles_dir(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1}, source="s")
    assert (tmp_path / h.path).exists()
    assert h.path.startswith("handles/")


def test_get_unknown_id_raises_keyerror_with_message(tmp_path):
    store = HandleStore(tmp_path)
    with pytest.raises(KeyError, match="no handle with id"):
        store.get("nope")


def test_explicit_id_advances_counter_no_collision(tmp_path):
    store = HandleStore(tmp_path)
    store.put("seed", source="s", id="h3")   # explicit id
    nxt = store.put("auto", source="s")       # next auto-id must not reuse h1..h3
    assert nxt.id == "h4"
    assert store.get("h3") == "seed"          # explicit handle not overwritten


def test_register_invalid_record_raises_valueerror(tmp_path):
    store = HandleStore(tmp_path)
    with pytest.raises(ValueError, match="invalid handle record"):
        store.register({"id": "h1", "bogus": True})  # missing required fields


def test_register_rejects_path_escaping_root(tmp_path):
    # The child supplies `path`; a record pointing outside root must be rejected
    # so the trusted parent never reads an arbitrary file via get().
    store = HandleStore(tmp_path)
    rec = {"id": "h1", "kind": "text", "path": "../secret_outside.txt",
           "source": "run_python", "bytes": 5, "preview": "x"}
    with pytest.raises(ValueError, match="escapes root"):
        store.register(rec)
    assert "h1" not in store.manifest_handles()  # not registered


def test_register_external_record_round_trips(tmp_path):
    # Simulates the subprocess helper having written a file + metadata record.
    store = HandleStore(tmp_path)
    (tmp_path / "handles").mkdir(exist_ok=True)
    (tmp_path / "handles" / "h7.txt").write_text("from child")
    rec = {"id": "h7", "kind": "text", "path": "handles/h7.txt",
           "source": "run_python", "bytes": 10, "preview": "from child"}
    h = store.register(rec)
    assert h.id == "h7"
    assert store.get("h7") == "from child"


def test_handle_store_rehydrates_manifest_and_counter(tmp_path):
    from tether.handles import HandleStore
    s1 = HandleStore(tmp_path)
    h1 = s1.put({"a": 1}, source="t")
    h2 = s1.put("hello", source="t")
    assert (h1.id, h2.id) == ("h1", "h2")

    s2 = HandleStore(tmp_path)                       # new store, same root -> rehydrate
    assert set(s2.manifest().keys()) == {"h1", "h2"}
    assert s2.get("h1") == {"a": 1}                  # backing file still readable
    assert s2.put("again", source="t").id == "h3"    # id counter resumed (no h1 collision)


def test_rehydrate_skips_corrupt_record(tmp_path):
    import json
    from tether.handles import HandleStore
    HandleStore(tmp_path).put({"a": 1}, source="t")   # valid h1, persists manifest
    mf = tmp_path / "handles" / "_manifest.json"
    data = json.loads(mf.read_text())
    data["bad"] = {"id": "bad"}                        # missing required Handle fields
    mf.write_text(json.dumps(data))
    s2 = HandleStore(tmp_path)                         # must not raise
    assert "h1" in s2.manifest() and "bad" not in s2.manifest()


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


def test_describe_dataframe_pins_pandas_dtype_vocabulary(tmp_path):
    """Guard against the schema silently switching to arrow-native type names.

    The put-vs-describe comparison cannot catch this: put() derives its metadata by
    calling _describe_dataframe, so both sides of that equality are the same code.
    These values are pandas' own dtype reprs -- arrow would render them "int64"/"double"/
    "string"/"bool"/"timestamp[us]".
    """
    store = HandleStore(tmp_path / "r")
    df = pd.DataFrame({"i": range(3), "f": [1.5] * 3, "s": ["a"] * 3, "b": [True] * 3})
    handle = store.put(df, source="t")

    described = store._describe_dataframe(store.root / handle.path)

    assert described["schema"] == {"i": "int64", "f": "float64", "s": "str", "b": "bool"}


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
