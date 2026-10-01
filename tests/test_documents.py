import os

import httpx

import tether.tools.documents as docmod
from tether import TetherConfig, Session
from tether.config import DocumentConfig, FetchConfig
from tether.tools.documents import prefetch_models, read_document


def _session(tmp_path, **fetch_kw):
    return Session.create(TetherConfig(root_dir=tmp_path / "r", fetch=FetchConfig(**fetch_kw)))


def test_path_source_converts_to_markdown_handle(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    summary = read_document(sess, "report.pdf", convert=lambda src: "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert summary["kind"] == "text"
    assert "Title" in summary["preview"]
    assert summary["source"] == "read_document(report.pdf)"
    hid = summary["id"]
    assert "| a | b |" in sess.store.get(hid)


def test_path_is_resolved_under_root_before_conversion(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "sub").mkdir()
    (sess.root / "sub" / "doc.docx").write_bytes(b"x")
    seen = {}

    def fake_convert(src):
        seen["src"] = src
        return "# ok"

    read_document(sess, "sub/doc.docx", convert=fake_convert)
    assert seen["src"] == str(sess.root / "sub" / "doc.docx")


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


def test_read_document_caps_the_downloaded_body_at_max_bytes(tmp_path, monkeypatch):
    session = _session(tmp_path, max_bytes=10)
    monkeypatch.setattr("tether.tools.documents.guarded_get",
                        lambda url, cfg, *, client, resolve=None:
                        httpx.Response(200, content=b"x" * 100))
    sizes = []
    out = read_document(session, "https://example.com/big.pdf",
                        convert=lambda src: sizes.append(os.path.getsize(src)) or "# md")
    assert out["kind"] == "text"
    assert sizes == [10]


def test_read_document_closes_its_client_on_every_path(tmp_path, monkeypatch):
    session = _session(tmp_path)
    clients = []
    real_client = httpx.Client

    def tracking_client(*a, **kw):
        c = real_client(*a, **kw)
        clients.append(c)
        return c

    monkeypatch.setattr("tether.tools.documents.httpx.Client", tracking_client)

    def failing(url, cfg, *, client, resolve=None):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr("tether.tools.documents.guarded_get", failing)
    read_document(session, "https://example.com/x.pdf", convert=lambda s: "")
    assert len(clients) == 1 and clients[0].is_closed


def _stub_get(monkeypatch, content=b"%PDF-1.4 fake"):
    monkeypatch.setattr("tether.tools.documents.guarded_get",
                        lambda url, cfg, *, client, resolve=None:
                        httpx.Response(200, content=content))


def test_downloaded_documents_are_not_listed_as_artifacts(tmp_path, monkeypatch):
    session = _session(tmp_path)
    (session.root / "report.txt").write_text("mine", encoding="utf-8")
    _stub_get(monkeypatch)
    out = read_document(session, "https://example.com/a.pdf", convert=lambda s: "# md")
    assert out["kind"] == "text"
    assert session.artifacts == ["report.txt"]
    assert list(session.root.glob("_documents/doc_*.pdf"))   # it really was downloaded


def test_overlong_url_suffix_does_not_crash_and_lands_as_bin(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _stub_get(monkeypatch)
    seen = []
    out = read_document(session, "https://example.com/doc." + "a" * 300,
                        convert=lambda src: seen.append(src) or "# md")
    assert out["kind"] == "text"
    assert seen[0].endswith(".bin")


def test_ordinary_suffix_is_preserved(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _stub_get(monkeypatch)
    seen = []
    read_document(session, "https://example.com/a/b.docx?x=1",
                  convert=lambda src: seen.append(src) or "# md")
    assert seen[0].endswith(".docx")


def test_filesystem_failure_during_download_is_a_structured_error(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _stub_get(monkeypatch)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("tether.tools.documents.tempfile.mkstemp", boom)
    out = read_document(session, "https://example.com/a.pdf", convert=lambda s: "")
    assert "could not store" in out["error"]


def test_path_escape_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "../../etc/passwd", convert=lambda src: "nope")
    assert "error" in out and "escape" in out["error"].lower()
    assert out["source"] == "../../etc/passwd"
    assert not sess.handles


def test_unknown_scheme_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "ftp://host/f.pdf", convert=lambda src: "nope")
    assert "error" in out and "scheme" in out["error"].lower()
    assert not sess.handles


def test_conversion_failure_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "broken.pdf").write_bytes(b"x")

    def boom(src):
        raise ValueError("corrupt pdf")

    out = read_document(sess, "broken.pdf", convert=boom)
    assert "error" in out and "could not read document" in out["error"].lower()
    assert "corrupt pdf" in out["error"]
    assert out["source"] == "broken.pdf"
    assert not sess.handles


def test_missing_docling_returns_actionable_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "x.pdf").write_bytes(b"x")

    def not_installed(src):
        raise ModuleNotFoundError("No module named 'docling'")

    out = read_document(sess, "x.pdf", convert=not_installed)
    assert "error" in out and "docling" in out["error"].lower()
    assert "--extra docling" in out["error"]
    assert not sess.handles


def test_default_converter_uses_config_ocr_off_by_default(tmp_path, monkeypatch):
    sess = _session(tmp_path)                                   # default DocumentConfig: ocr=False
    (sess.root / "f.pdf").write_bytes(b"x")
    captured = {}
    monkeypatch.setattr(docmod, "_docling_convert",
                        lambda src, ocr: captured.update(src=src, ocr=ocr) or "# md")
    out = read_document(sess, "f.pdf")                          # no injected converter -> default path
    assert out["kind"] == "text"
    assert captured["ocr"] is False
    assert captured["src"] == str(sess.root / "f.pdf")


def test_default_converter_enables_ocr_when_configured(tmp_path, monkeypatch):
    sess = Session.create(TetherConfig(root_dir=tmp_path / "r", documents=DocumentConfig(ocr=True)))
    (sess.root / "f.pdf").write_bytes(b"x")
    captured = {}
    monkeypatch.setattr(docmod, "_docling_convert",
                        lambda src, ocr: captured.update(ocr=ocr) or "# md")
    read_document(sess, "f.pdf")
    assert captured["ocr"] is True


def test_prefetch_models_skips_ocr_models_by_default():
    calls = {}
    prefetch_models(downloader=lambda **kw: calls.update(kw))
    assert calls["with_rapidocr"] is False                      # matches the OCR-off default


def test_prefetch_models_includes_ocr_models_when_requested():
    calls = {}
    prefetch_models(downloader=lambda **kw: calls.update(kw), ocr=True)
    assert calls["with_rapidocr"] is True
