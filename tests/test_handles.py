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


def test_put_input_registers_binary_handle(tmp_path):
    store = HandleStore(tmp_path)
    (tmp_path / "inputs" / "f1").mkdir(parents=True)
    (tmp_path / "inputs" / "f1" / "a.csv").write_bytes(b"x")
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="upload:a.csv",
                        input_id="f1", content_type="text/csv", description="d")
    assert (h.kind, h.input_id, h.content_type, h.description) == ("binary", "f1", "text/csv", "d")
    assert store.inputs() == {"f1": h}
    assert store.get(h.id) == str((tmp_path / "inputs/f1/a.csv").resolve())


def test_put_input_is_idempotent_and_survives_reload(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="s", input_id="f1")
    assert store.put_input(path="inputs/f1/b.csv", size=9, preview="q", source="s",
                           input_id="f1") == h
    assert HandleStore(tmp_path).inputs() == {"f1": h}


def test_put_input_rejects_escaping_path(tmp_path):
    with pytest.raises(ValueError):
        HandleStore(tmp_path).put_input(path="../x", size=1, preview="p", source="s",
                                        input_id="f1")


def test_non_input_summary_has_no_input_fields(tmp_path):
    s = HandleStore(tmp_path).put("hello", source="t").summary()
    assert not {"input_id", "content_type", "description"} & s.keys()


def test_put_input_replace_keeps_id_and_persists(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put_input(path="inputs/f1/a.csv", size=1, preview="p", source="s", input_id="f1")
    h2 = store.put_input(path="inputs/f1/b.csv", size=2, preview="q", source="s2",
                         input_id="f1", replace=True)
    assert h2.id == h.id and h2.path == "inputs/f1/b.csv"
    assert HandleStore(tmp_path).inputs()["f1"].path == "inputs/f1/b.csv"
