"""Typed handles: large data lives on disk; only a lightweight summary enters context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import PathEscapesRootError, safe_path

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


@dataclass
class Handle:
    id: str
    kind: str  # "json" | "text" | "dataframe" | "binary"
    path: str  # POSIX path relative to the session root
    source: str
    bytes: int
    preview: str
    schema: dict[str, str] | None = None
    n_rows: int | None = None
    n_cols: int | None = None
    input_id: str | None = None       # host-supplied id of an uploaded input
    content_type: str | None = None   # advisory type of an uploaded input
    description: str | None = None    # host-supplied description of an uploaded input

    def summary(self) -> dict[str, Any]:
        """Context-facing view: drop None fields to keep it compact."""
        return {k: v for k, v in asdict(self).items() if v is not None}


class HandleStore:
    """Persists objects under ``<root>/handles`` and tracks them by id."""

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
        (self.root / rel).write_bytes(bytes(data))
        return Handle(
            id=hid, kind="binary", path=rel, source=source,
            bytes=len(data), preview=f"<binary file, {len(data)} bytes, {ext or '.bin'}>",
        )

    def _write_dataframe(self, hid: str, df: Any, source: str) -> Handle:
        rel = f"handles/{hid}.parquet"
        path = self.root / rel
        df.to_parquet(path)
        preview = df.head(_PREVIEW_ROWS).to_csv(index=False)
        preview += f"... ({_PREVIEW_ROWS} of {len(df)} rows)" if len(df) > _PREVIEW_ROWS else ""
        return Handle(
            id=hid, kind="dataframe", path=rel, source=source,
            bytes=path.stat().st_size, preview=preview,
            schema={c: str(t) for c, t in df.dtypes.items()},
            n_rows=int(len(df)), n_cols=int(df.shape[1]),
        )

    def _write_json(self, hid: str, obj: Any, source: str) -> Handle:
        # ``default=str`` keeps non-JSON-native types (datetime, Decimal, ...) from
        # crashing serialization, but they round-trip back as strings via get().
        rel = f"handles/{hid}.json"
        path = self.root / rel
        text = json.dumps(obj, default=str)
        path.write_text(text, encoding="utf-8")
        return Handle(
            id=hid, kind="json", path=rel, source=source,
            bytes=len(text.encode()), preview=text[:_PREVIEW_CHARS],
        )

    def _write_text(self, hid: str, obj: str, source: str) -> Handle:
        rel = f"handles/{hid}.txt"
        path = self.root / rel
        path.write_text(obj, encoding="utf-8")
        return Handle(
            id=hid, kind="text", path=rel, source=source,
            bytes=len(obj.encode()), preview=obj[:_PREVIEW_CHARS],
        )

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

    def put_input(self, *, path: str, size: int, preview: str, source: str, input_id: str,
                  content_type: str | None = None, description: str | None = None) -> Handle:
        """Register a host-ingested upload whose file already exists under ``inputs/``.

        Parent-authored (the host wrote the bytes), so it does not go through the sandbox
        adoption path. Idempotent by ``input_id``: an existing input is returned unchanged.
        """
        existing = self.inputs().get(input_id)
        if existing is not None:
            return existing
        try:
            safe_path(self.root, path)
        except PathEscapesRootError as e:
            raise ValueError(f"input path escapes root: {path!r}") from e
        handle = Handle(id=self._new_id(), kind="binary", path=path, source=source, bytes=size,
                        preview=preview, input_id=input_id, content_type=content_type,
                        description=description)
        self._handles[handle.id] = handle
        self._save_manifest()
        return handle

    def inputs(self) -> dict[str, Handle]:
        """Uploaded inputs, keyed by host-supplied ``input_id``."""
        return {h.input_id: h for h in self._handles.values() if h.input_id is not None}

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
