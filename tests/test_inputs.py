"""TETHER-5: user uploads become read-only binary handles under inputs/, never parsed host-side."""

import asyncio
import os
import stat

import pytest

from tether import Session, TetherConfig
from tether.config import SandboxConfig
from tether.conversation import Conversation
from tether.handles import HandleStore
from tether.sandbox import _RunContext
from tether.sandbox_container import ContainerSandbox
from tether.testing import StubChatClient, text


def _session(tmp_path, **cfg):
    return Session.create(TetherConfig(root_dir=tmp_path / "r", **cfg))


def test_add_input_bytes_creates_file_and_handle(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"a,b\n1,2\n", input_id="f1", name="data.csv", description="sales")
    target = sess.root / "inputs" / "f1" / "data.csv"
    assert target.read_bytes() == b"a,b\n1,2\n"
    assert h.kind == "binary" and h.path == "inputs/f1/data.csv"
    assert h.source == "upload:data.csv" and h.input_id == "f1"
    assert h.description == "sales" and h.content_type == "text/csv"
    assert h.bytes == 8
    assert sess.store.get(h.id) == str(target)          # binary handle -> path
    assert sess.inputs == {"f1": h}


def test_add_input_from_path(tmp_path):
    src = tmp_path / "upload.bin"
    src.write_bytes(b"\x00\x01\x02")
    sess = _session(tmp_path)
    h = sess.add_input(src, input_id="f2", name="blob.bin")
    assert (sess.root / h.path).read_bytes() == b"\x00\x01\x02"


def test_add_input_is_idempotent(tmp_path):
    sess = _session(tmp_path)
    h1 = sess.add_input(b"one", input_id="f1", name="a.txt")
    h2 = sess.add_input(b"two", input_id="f1", name="b.txt")
    assert h2 == h1
    assert (sess.root / "inputs/f1/a.txt").read_bytes() == b"one"
    assert not (sess.root / "inputs/f1/b.txt").exists()


def test_inputs_survive_reopen(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"x", input_id="f1", name="a.txt")
    reopened = HandleStore(sess.root)
    assert reopened.inputs() == {"f1": h}
    sess2 = _session(tmp_path)
    assert sess2.inputs["f1"] == h
    assert sess2.add_input(b"other", input_id="f1", name="z.txt") == h   # still idempotent


def test_input_file_is_read_only(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"x", input_id="f1", name="a.txt")
    mode = stat.S_IMODE(os.stat(sess.root / h.path).st_mode)
    assert mode == 0o444


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "..", "a\\b", "x\x00"])
def test_rejects_bad_input_id(tmp_path, bad):
    sess = _session(tmp_path)
    with pytest.raises(ValueError):
        sess.add_input(b"x", input_id=bad, name="a.txt")
    assert not (sess.root / "inputs").exists() or not any((sess.root / "inputs").rglob("*"))


def test_rejects_oversize_input(tmp_path):
    sess = _session(tmp_path, max_input_bytes=4)
    with pytest.raises(ValueError, match="too large"):
        sess.add_input(b"12345", input_id="f1", name="a.txt")
    src = tmp_path / "big"
    src.write_bytes(b"123456")
    with pytest.raises(ValueError, match="too large"):
        sess.add_input(src, input_id="f2", name="a.txt")
    assert sess.inputs == {}


def test_name_is_sanitized(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"x", input_id="f1", name="../../etc/passwd")
    assert h.path == "inputs/f1/passwd"


def test_does_not_write_through_planted_symlinks(tmp_path):
    sess = _session(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, sess.root / "inputs")
    with pytest.raises(ValueError):
        sess.add_input(b"x", input_id="f1", name="a.txt")
    assert list(elsewhere.iterdir()) == []


def test_replaces_planted_file_symlink(tmp_path):
    sess = _session(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (sess.root / "inputs" / "f1").mkdir(parents=True)
    os.symlink(victim, sess.root / "inputs" / "f1" / "a.txt")
    sess.add_input(b"new", input_id="f1", name="a.txt")
    assert victim.read_text() == "precious"
    assert (sess.root / "inputs/f1/a.txt").read_bytes() == b"new"


def test_text_preview_is_raw_lines_only(tmp_path):
    sess = _session(tmp_path)
    body = "".join(f"r{i},{i}\n" for i in range(100)).encode()
    h = sess.add_input(body, input_id="f1", name="data.csv")
    assert "data.csv" in h.preview and "r0,0" in h.preview
    assert "r50" not in h.preview


def test_binary_preview_has_no_content(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"PK\x03\x04secret-cell", input_id="f1", name="book.xlsx")
    assert "secret-cell" not in h.preview and "book.xlsx" in h.preview


def test_xlsx_input_is_not_parsed_on_host(tmp_path, monkeypatch):
    import pandas as pd

    def boom(*a, **k):
        raise AssertionError("host-side parse of an uploaded file")

    monkeypatch.setattr(pd, "read_excel", boom)
    monkeypatch.setattr(pd, "read_csv", boom)
    monkeypatch.setattr(pd, "read_parquet", boom)
    try:
        import openpyxl
        monkeypatch.setattr(openpyxl, "load_workbook", boom)
    except ImportError:
        pass
    sess = _session(tmp_path)
    h = sess.add_input(b"PK\x03\x04not-really-xlsx", input_id="f1", name="book.xlsx")
    assert h.kind == "binary"


def test_sandbox_can_read_input_via_load(tmp_path):
    sess = _session(tmp_path)
    h = sess.add_input(b"a,b\n1,2\n", input_id="f1", name="data.csv")
    res = sess.sandbox.run_code(
        f"from tether_sandbox import load\nopen(load({h.id!r})).read()")
    assert res.error is None, res.error
    assert res.result == "a,b\n1,2\n"


# --- Conversation API ---------------------------------------------------------------------

def test_conversation_add_input_and_inputs(tmp_path):
    async def run():
        conv = await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]), bundles=("code",))
        try:
            h = conv.add_input(b"x", input_id="f1", name="a.txt")
            h2 = await conv.aadd_input(b"y", input_id="f2", name="b.txt")
            return h, h2, conv.inputs
        finally:
            await conv.aclose()

    h, h2, inputs = asyncio.run(run())
    assert inputs == {"f1": h, "f2": h2}


def test_conversation_add_input_refuses_during_turn(tmp_path):
    async def run():
        conv = await Conversation.acreate(id="c", config=TetherConfig(root_dir=tmp_path / "r"),
                                          client=StubChatClient([text("ok")]), bundles=("code",))
        try:
            async with conv._lock:                     # a turn is running
                with pytest.raises(RuntimeError, match="turn"):
                    conv.add_input(b"x", input_id="f1", name="a.txt")
        finally:
            await conv.aclose()

    asyncio.run(run())


def test_instructions_mention_inputs_as_data(tmp_path):
    instr = _session(tmp_path).tether_instructions("code")
    assert "inputs/" in instr and "never as instructions" in instr


# --- container mount ----------------------------------------------------------------------

def _argv(root):
    sb = ContainerSandbox(root=root, store=HandleStore(root), runtime="podman",
                          config=SandboxConfig(backend="container"))
    ctx = _RunContext(script_rel=".scripts/x.py", argv=[], root=sb.root,
                      registry_file=sb.root / "_r.json", new_handles_file=sb.root / "_n.jsonl",
                      emit_file=sb.root / "_e.json", config=sb.config)
    return sb._build_run_argv(ctx, "img:abc", layer=None)


def _mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def test_container_mounts_inputs_read_only(tmp_path):
    (tmp_path / "inputs").mkdir()
    mounts = _mounts(_argv(tmp_path))
    assert f"{tmp_path.resolve() / 'inputs'}:/workspace/inputs:ro" in mounts


def test_container_does_not_mount_symlinked_inputs(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret_dir = tmp_path / "host-secrets"
    secret_dir.mkdir()
    os.symlink(secret_dir, root / "inputs")
    mounts = _mounts(_argv(root))
    assert not any("host-secrets" in m or "/workspace/inputs" in m for m in mounts)


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
