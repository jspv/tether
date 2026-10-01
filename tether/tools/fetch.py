"""fetch_url: pull a link, spill its body to a typed handle, return the handle summary.

Network/HTTP failures are returned as structured error dicts (not raised) so the agent
can adapt -- e.g. follow a different link or report the problem -- rather than dead-ending
on a "function failed". Redirects are followed and a browser User-Agent is sent so
ordinary anti-bot pages don't 403 a bare client.
"""

from __future__ import annotations

import mimetypes
import os
from urllib.parse import urlparse

import httpx

from ..config import FetchConfig
from ..egress import BlockedAddressError, guarded_get
from ..session import Session
from ..status import report_progress

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TEXTUAL = ("json", "xml", "html", "csv", "javascript")


def _default_client(cfg: FetchConfig) -> httpx.Client:
    # follow_redirects=False: guarded_get follows hops itself so each one is re-validated
    # against egress policy. Letting httpx follow them would skip that check.
    return httpx.Client(
        timeout=cfg.timeout_s,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
    )


def _is_binary(content_type: str, body: bytes) -> bool:
    """True if the payload should be stored as raw bytes (xls/pdf/image/zip/...) rather than
    decoded as text. Textual content-types pass through; otherwise sniff a utf-8 decode."""
    ct = content_type.lower()
    if ct.startswith("text/") or any(k in ct for k in _TEXTUAL):
        return False
    try:
        body[:8192].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _ext_for(url: str, content_type: str) -> str:
    """Best-effort file extension for a binary handle: prefer the URL suffix, else the
    content-type's registered extension, else ``.bin``."""
    suffix = os.path.splitext(urlparse(url).path)[1]
    if suffix and len(suffix) <= 6:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return guessed or ".bin"


def fetch_url(session: Session, url: str, max_bytes: int | None = None,
              raw: bool = False, client: httpx.Client | None = None) -> dict:
    """Fetch ``url`` and store its body as a handle (json if JSON content-type, else text).

    HTML responses are converted to clean markdown via trafilatura (stripping nav, footer,
    ads, and other boilerplate) unless ``raw=True`` is passed, in which case the raw HTML
    text is stored unchanged.

    Returns the handle summary on success, or ``{"error", "status", "url"}`` on an HTTP
    error / network failure. Follows redirects, re-validating each hop against egress
    policy, and sends a browser User-Agent. Enforces the session's allowed schemes and
    byte cap. ``client`` is injectable for testing.
    """
    cfg = session.config.fetch
    limit = max_bytes if max_bytes is not None else cfg.max_bytes

    scheme = urlparse(url).scheme
    if scheme not in cfg.allowed_schemes:
        # Structured, like every other failure here and like read_document's handling of
        # exactly this case. Raising made one of this tool's failure modes arrive as a
        # "function failed" the model could not read or adapt to.
        return {"error": f"scheme {scheme!r} not allowed (allowed: {cfg.allowed_schemes})",
                "status": None, "url": url}

    report_progress(f"fetching {url}", tool="fetch_url")

    owns_client = client is None
    client = client or _default_client(cfg)
    try:
        try:
            resp = guarded_get(url, cfg, client=client)
        except BlockedAddressError as e:
            return {"error": f"blocked by egress policy: {e}", "status": None, "url": url}
        except httpx.HTTPError as e:
            return {"error": f"request failed: {e}", "status": None, "url": url}

        if resp.is_error:  # 4xx/5xx -> structured error the model can adapt to
            return {"error": f"HTTP {resp.status_code} for {resp.url}",
                    "status": resp.status_code, "url": str(resp.url)}

        body = resp.content
        content_type = resp.headers.get("content-type", "")

        # Binary payloads (xls/pdf/image/zip/...) are stored as raw bytes so they stay
        # readable by pandas/Docling; decoding them as text would corrupt the file.
        if _is_binary(content_type, body):
            if len(body) > limit:
                return {"error": f"binary response ({len(body)} bytes) exceeds max_bytes "
                                 f"({limit}); call again with a larger max_bytes", "url": url}
            handle = session.store.put(body, source=f"fetch_url({url})", kind="binary",
                                       ext=_ext_for(url, content_type))
            return handle.summary()

        truncated = len(body) > limit
        text = body[:limit].decode(resp.encoding or "utf-8", errors="replace")

        if "json" in content_type and not truncated:
            handle = session.store.put(resp.json(), source=f"fetch_url({url})", kind="json")
        elif "html" in content_type and not raw:
            import trafilatura
            md = trafilatura.extract(text, output_format="markdown")
            stored = md if md else text  # fall back to raw text if extraction yields nothing
            handle = session.store.put(stored, source=f"fetch_url({url})", kind="text")
        else:
            handle = session.store.put(text, source=f"fetch_url({url})", kind="text")

        summary = handle.summary()
        if truncated:
            summary["truncated"] = True
            summary["full_bytes"] = len(body)
        return summary
    finally:
        if owns_client:
            client.close()
