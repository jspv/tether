# Web Research + Document Ingestion — Design (Phase 2d)

- **Date:** 2026-05-30
- **Status:** Approved design (brainstormed); supersedes the placeholder
  `2026-05-30-web-research-capability-spec.md`.
- **Motivation:** Self-eval (`evals/run_evals.py`) — 8/10 tasks pass, but every open-web
  research task fails (no web search; `fetch_url` returns raw HTML soup). A controlled
  comparison (a general-purpose coding agent driving a **small model** of the same tier as
  our gpt-4o-mini) *succeeded* on the pricing task, isolating the cause as **tooling, not
  model**.

## Goal

Give the agent eyes on **two information sources** it currently can't use well, feeding
clean content into the existing "handle → analyze-in-sandbox" loop:
1. **Web research** — discover sources (search) and get clean page content (smart fetch).
2. **Document ingestion** — turn PDF/Office/spreadsheet files into clean markdown + tables.

Built in **two implementation plans** under this one spec: **2d-web first** (fixes the
failing eval tasks), then **2d-docs**.

## Decisions (resolved in brainstorm)

| Decision | Choice | Why |
|---|---|---|
| Search provider | **Tavily** | Built for agents: returns extracted content + a direct `answer`; simple REST; free tier. `/extract` also covers smart-fetch. |
| Smart fetch | **Hybrid** | `web_search` returns Tavily-extracted content; `fetch_url` does local HTML→markdown; Tavily `/extract` for hard pages. |
| HTML→markdown lib | **trafilatura** | Top main-content extraction (≈0.88 mean in benchmarks); strips boilerplate. markitdown's HTML is whole-page/noisier. |
| Document conversion | **Docling** | Reviews: markitdown is a "basic text scraper" weak on PDF tables/structure; Docling (+ Mistral/LlamaParse) far better at tables — which is what data integration needs. User has had good results with Docling. |
| Provider abstraction | **None (YAGNI)** | Isolate Tavily HTTP in helpers; swappable later without an interface we don't yet need. |
| JS rendering / headless browser | **Out of scope** | Note for a later phase. trafilatura + Tavily handle server-rendered/docs pages. |

## Components

| Unit | Responsibility | New dep |
|---|---|---|
| `config.py` (modify) | `SearchConfig(provider="tavily", api_key=None, max_results=5, timeout_s=20.0)` + `search: SearchConfig` on `HarnessConfig`; `api_key` from `TAVILY_API_KEY` in `.env` | — |
| `tools/web.py` (new) | `web_search(query, max_results)` → Tavily `/search`; `web_extract(url)` → Tavily `/extract` → markdown handle. `_tavily_search`/`_tavily_extract` helpers; injectable httpx client | httpx (have) |
| `tools/fetch.py` (modify) | HTML → **trafilatura** main-content markdown before storing (clean default; `raw=True` opt-out; fall back to capped raw text if extraction yields nothing) | trafilatura |
| `tools/documents.py` (new, 2d-docs) | `read_document(source)` — workspace path **or** URL → **Docling** → markdown (with tables) handle. Lazy Docling import | docling |
| `tools/registry.py` (modify) | register `web_search`, `web_extract` (2d-web) and `read_document` (2d-docs) | — |

## Tool signatures

```python
web_search(query: str, max_results: int = 5) -> dict
  # → {"answer": str|None, "results": [{"title","url","content","score"}]}
  # content = Tavily extracted snippet, each clipped to ~500 chars. Small → returned INLINE.

web_extract(url: str) -> dict
  # Tavily /extract → clean markdown → stored as a handle → returns the handle summary.

fetch_url(url, max_bytes=None, raw: bool = False) -> dict   # MODIFIED
  # HTML → trafilatura markdown before storing (clean default; raw=True = raw HTML).

read_document(source: str) -> dict                          # 2d-docs
  # path-under-root OR URL → Docling → markdown (with tables) handle → summary.
```

## Data flow (research task)

1. `web_search("OpenAI API pricing per million tokens")` → ranked results + extracted
   snippets (+ often a direct `answer`). The "discover the right source" move.
2. Need more? `web_extract(url)` or `fetch_url(url)` → a **clean-markdown handle**.
3. A PDF/spreadsheet link? `read_document(url)` → Docling markdown **with tables** → handle.
4. Existing loop: `inspect_handle`/`search`/`read_file`/`run_python` over the clean handle →
   compute/integrate/verify → answer with sources.

**Context-safety invariant kept:** `web_search` returns a small bounded list; everything
else produces clean-markdown **handles** (far smaller than raw HTML/PDF), so big content
never floods context and analysis happens in the sandbox as today.

## Error handling

All new tools follow the existing **structured-error convention** (`{error, status, url}`),
never raise into the agent loop. Missing `TAVILY_API_KEY` → a clear "set TAVILY_API_KEY in
.env" message. Docling/unsupported-format/corrupt-file failures → structured error.

## Testing (keeps the no-network/API-in-CI rule)

- `web_search`/`web_extract`: `httpx.MockTransport` canned Tavily JSON → assert `{answer,
  results}` parsing, snippet clipping, structured-error paths, missing-key message.
- `fetch_url` + trafilatura: canned messy HTML → markdown keeps content, drops boilerplate,
  smaller than raw; `raw=True` returns raw.
- `read_document`: unit-test routing (path vs URL) + error handling via an **injected
  converter** (no real Docling). A real Docling smoke test (tiny fixture) is gated behind an
  opt-in env flag (Docling needs model downloads — not a CI dependency).
- One opt-in live test (`HARNESS_LIVE=1`, needs `TAVILY_API_KEY`): re-run the pricing task;
  success = real per-token numbers in the answer.
- Regression guard from the last eval still applies: exercise every tool through
  `build_tools`, not just its impl.

## Out of scope (note for later)
- Headless-browser / JS rendering.
- A full search-provider abstraction (swap Tavily by editing one module).
- Mistral/LlamaParse document backends (Docling is the chosen one; re-evaluate if needed).
