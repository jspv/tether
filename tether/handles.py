"""Typed handles: large data lives on disk; only a lightweight summary enters context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import PathEscapesRootError, safe_path

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


class HandleIdReuseError(ValueError):
    """Raised when adopting an id that already exists. Handles are immutable.

    Subclasses ValueError so every existing `except ValueError` caller keeps working;
    ingestion catches it by type to report it rather than silently skipping.
    """


@dataclass
class Handle:
    id: str
    kind: str  # "json" | "text" | "dataframe"
    path: str  # POSIX path relative to the session root
    source: str
    bytes: int
    preview: str
    schema: dict[str, str] | None = None
    n_rows: int | None = None
    n_cols: int | None = None

    def summary(self) -> dict[str, Any]:
        """Context-facing view: drop None fields to keep it compact."""
        return {k: v for k, v in asdict(self).items() if v is not None}


class HandleStore:
    """Persists objects under ``<root>/handles`` and tracks them by id."""

    # json and text describe identically, so both kinds dispatch to the one shared body.
    _DESCRIBERS = {"dataframe": "_describe_dataframe", "json": "_describe_textual",
                   "text": "_describe_textual", "binary": "_describe_binary"}

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / "handles"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, Handle] = {}
        self._counter = 0
        self._load_manifest()

    def _new_id(self) -> str:
        self._counter += 1
        return f"h{self._counter}"

    @staticmethod
    def _detect_kind(obj: Any) -> str:
        import pandas as pd

        if isinstance(obj, pd.DataFrame):
            return "dataframe"
        if isinstance(obj, (dict, list)):
            return "json"
        if isinstance(obj, str):
            return "text"
        if isinstance(obj, (bytes, bytearray)):
            return "binary"
        raise TypeError(f"unsupported handle object type: {type(obj)!r}")

    def put(self, obj: Any, source: str, *, id: str | None = None,
            kind: str | None = None, ext: str | None = None) -> Handle:
        hid = id or self._new_id()
        if id is not None:
            self._advance_counter(hid)  # keep auto-ids from colliding with an explicit id
        kind = kind or self._detect_kind(obj)
        if kind == "dataframe":
            handle = self._write_dataframe(hid, obj, source)
        elif kind == "json":
            handle = self._write_json(hid, obj, source)
        elif kind == "text":
            handle = self._write_text(hid, obj, source)
        elif kind == "binary":
            handle = self._write_binary(hid, obj, source, ext)
        else:
            raise ValueError(f"unknown handle kind: {kind!r}")
        self._handles[hid] = handle
        self._save_manifest()
        return handle

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

    def _advance_counter(self, hid: str) -> None:
        """Keep the auto-id counter ahead of an externally-supplied ``h<N>`` id."""
        if hid.startswith("h") and hid[1:].isdigit():
            self._counter = max(self._counter, int(hid[1:]))

    def _register_record(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists (sandbox child, or manifest rehydration).

        The ``path`` is supplied by lower-trust input, so it is run through ``safe_path``: a
        record pointing outside the root is rejected here rather than read later.
        """
        try:
            handle = Handle(**record)
        except TypeError as e:  # contract boundary — give a useful message
            raise ValueError(f"invalid handle record {record!r}: {e}") from e
        try:
            safe_path(self.root, handle.path)
        except PathEscapesRootError as e:
            raise ValueError(f"handle record path escapes root: {record!r}") from e
        self._handles[handle.id] = handle
        self._advance_counter(handle.id)
        return handle

    def register(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists; persists the manifest."""
        handle = self._register_record(record)
        self._save_manifest()
        return handle

    def adopt(self, *, id: str, kind: str, path: str, source: str) -> Handle:
        """Register a file written by sandboxed code, deriving its metadata here.

        The child reports only what it alone knows -- which file it wrote, and why. Every
        field that reaches model context (preview, bytes, schema, n_rows, n_cols) is
        computed from the bytes on disk, so a hostile child cannot describe its output
        falsely. Handles are immutable: adopting an id that already exists is refused, so
        the record for a handle the child did not create cannot be repointed.
        """
        if id in self._handles:
            raise HandleIdReuseError(f"handle id {id!r} already exists; handles are immutable")
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

    @property
    def _manifest_file(self) -> Path:
        return self.dir / "_manifest.json"

    def _save_manifest(self) -> None:
        """Persist {id: summary} atomically so a new HandleStore on this root can rehydrate.

        Rewrites the whole manifest on every put/register -- fine at handle frequency (handles are
        coarse), and the atomic tmp+replace is worth it. Summaries drop None fields, which
        re-default on ``Handle(**record)`` load, so the round-trip is lossless.
        """
        tmp = self.dir / "_manifest.json.tmp"
        tmp.write_text(json.dumps(self.manifest()), encoding="utf-8")
        tmp.replace(self._manifest_file)

    def _load_manifest(self) -> None:
        """Restore handles + the id counter from a prior session on this root. Tolerant:
        a corrupt record is skipped, not fatal."""
        if not self._manifest_file.exists():
            return
        try:
            records = json.loads(self._manifest_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for record in records.values():
            try:
                self._register_record(record)
            except (ValueError, KeyError):
                continue

    def get(self, handle_id: str) -> Any:
        try:
            handle = self._handles[handle_id]
        except KeyError:
            raise KeyError(f"no handle with id {handle_id!r}") from None
        path = safe_path(self.root, handle.path)  # defense-in-depth before any read
        if handle.kind == "dataframe":
            import pandas as pd
            return pd.read_parquet(path)
        if handle.kind == "json":
            return json.loads(path.read_text(encoding="utf-8"))
        if handle.kind == "binary":
            return str(path)  # binary content is opened by a library; hand back the path
        return path.read_text(encoding="utf-8")

    def summary(self, handle_id: str) -> dict[str, Any]:
        return self._handles[handle_id].summary()

    def manifest(self) -> dict[str, Any]:
        return {hid: h.summary() for hid, h in self._handles.items()}

    def manifest_handles(self) -> dict[str, Handle]:
        return dict(self._handles)
