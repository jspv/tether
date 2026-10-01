"""TETHER-4 core: every publication is validated in the parent before the host sees it."""

import hashlib
import os

import pytest

from tether.publish import PublishError, PublishedFile, validate_publication

MAX = 1024


def _write(root, rel, data=b"x,y\n1,2\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_valid_file_is_described(tmp_path):
    data = b"x,y\n1,2\n"
    _write(tmp_path, "outputs/a.csv", data)
    pf = validate_publication(tmp_path, "outputs/a.csv", name=None, description="d",
                              source="tool:publish_file", max_bytes=MAX)
    assert isinstance(pf, PublishedFile)
    assert pf.path == (tmp_path / "outputs/a.csv").resolve()
    assert pf.rel_path == "outputs/a.csv"
    assert pf.name == "a.csv"
    assert pf.size == len(data)
    assert pf.sha256 == hashlib.sha256(data).hexdigest()
    assert pf.content_type == "text/csv"
    assert pf.description == "d" and pf.source == "tool:publish_file"


def test_absolute_path_inside_root_is_accepted(tmp_path):
    _write(tmp_path, "outputs/a.csv")
    pf = validate_publication(tmp_path, str(tmp_path / "outputs/a.csv"), name=None,
                              description=None, source="s", max_bytes=MAX)
    assert pf.rel_path == "outputs/a.csv"


def test_name_is_sanitized(tmp_path):
    _write(tmp_path, "outputs/a.csv")
    pf = validate_publication(tmp_path, "outputs/a.csv", name="../../evil\x00.csv",
                              description=None, source="s", max_bytes=MAX)
    assert pf.name == "evil.csv"


def _reject(root, path, **kw):
    with pytest.raises(PublishError):
        validate_publication(root, path, name=kw.get("name"), description=None, source="s",
                             max_bytes=kw.get("max_bytes", MAX))


def test_rejects_dotdot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path, "x")                     # a real file just outside the root
    _reject(root, "../x")


def test_rejects_absolute_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = _write(tmp_path, "secret.txt")
    _reject(root, str(outside))
    _reject(root, "/etc/hosts")


def test_rejects_symlink_pointing_outside(tmp_path):
    root = tmp_path / "root"
    (root / "outputs").mkdir(parents=True)
    outside = _write(tmp_path, "secret.txt")
    os.symlink(outside, root / "outputs" / "link.csv")
    _reject(root, "outputs/link.csv")


def test_rejects_symlink_even_inside_root(tmp_path):
    _write(tmp_path, "outputs/real.csv")
    os.symlink(tmp_path / "outputs/real.csv", tmp_path / "outputs/link.csv")
    _reject(tmp_path, "outputs/link.csv")


def test_rejects_file_under_symlinked_dir_escaping_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path, "elsewhere/secret.csv")
    os.symlink(tmp_path / "elsewhere", root / "outputs")
    _reject(root, "outputs/secret.csv")


def test_rejects_directory(tmp_path):
    (tmp_path / "outputs").mkdir()
    _reject(tmp_path, "outputs")


def test_rejects_missing(tmp_path):
    _reject(tmp_path, "outputs/nope.csv")


def test_rejects_fifo(tmp_path):
    (tmp_path / "outputs").mkdir()
    os.mkfifo(tmp_path / "outputs" / "p")
    _reject(tmp_path, "outputs/p")


@pytest.mark.parametrize("rel", ["handles/h1.parquet", "handles/_manifest.json",
                                 ".scripts/inline_x.py", "_emit_1_2.json",
                                 "_new_handles_1_2.jsonl", "_publish_1_2.jsonl"])
def test_rejects_internal_files(tmp_path, rel):
    _write(tmp_path, rel)
    _reject(tmp_path, rel)


def test_rejects_oversize(tmp_path):
    _write(tmp_path, "outputs/big.bin", b"x" * (MAX + 1))
    _reject(tmp_path, "outputs/big.bin")


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_rejects_bad_path_values(tmp_path, bad):
    _reject(tmp_path, bad)
