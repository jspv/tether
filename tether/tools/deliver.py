"""publish_file / publish_handle: deliver finished workspace files to the host.

Both go through ``Session.publish``, which validates the file and invokes the host callback;
the model only ever sees the returned metadata, never the file bytes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..paths import PathEscapesRootError, safe_filename, safe_path
from ..session import Session

_OUTPUTS = "outputs"
_DATAFRAME_FORMATS = ("csv", "xlsx", "parquet", "json")


def publish_file(session: Session, path: str, name: str | None = None,
                 description: str | None = None) -> dict:
    """Deliver a finished workspace file to the user; returns its metadata + the host reply."""
    return session.publish(path, name=name, description=description, source="tool:publish_file")


def publish_handle(session: Session, handle_id: str, format: str | None = None,
                   name: str | None = None, description: str | None = None) -> dict:
    """Write a handle to ``outputs/`` (converting a dataframe to ``format``) and publish it.

    Holds the session's sandbox lock throughout, so no sandboxed code can swap ``outputs/``
    between the containment check and the write.
    """
    with session.io_lock:
        return _publish_handle(session, handle_id, format, name, description)


def _publish_handle(session: Session, handle_id: str, format: str | None, name: str | None,
                    description: str | None) -> dict:
    handle = session.store.manifest_handles().get(handle_id)
    if handle is None:
        return {"error": f"no handle with id {handle_id!r}"}
    try:
        if handle.kind == "dataframe":
            fmt = (format or "csv").lower()
            if fmt not in _DATAFRAME_FORMATS:
                return {"error": f"unsupported format {format!r}; use one of {_DATAFRAME_FORMATS}"}
            filename = safe_filename(name, fallback=f"{handle_id}.{fmt}")
            if not filename.lower().endswith(f".{fmt}"):
                filename += f".{fmt}"
            df = session.store.get(handle_id)
            rel = _write_output(session, filename, lambda p: _write_dataframe(df, fmt, p))
        else:
            backing = safe_path(session.root, handle.path)
            ext = backing.suffix.lstrip(".").lower()
            if format is not None and format.lower() != ext:
                return {"error": f"a {handle.kind} handle is delivered as-is (.{ext}); "
                                 "format conversion applies to dataframe handles only"}
            filename = safe_filename(name, fallback=backing.name)
            rel = _write_output(session, filename, lambda p: shutil.copyfile(backing, p))
    except (OSError, ValueError, ImportError) as e:
        return {"error": f"could not write {handle_id} for publishing: {e}"}
    return session.publish(rel, name=filename, description=description,
                           source="tool:publish_handle")


def _write_dataframe(df, fmt: str, path: Path) -> None:
    if fmt == "csv":
        df.to_csv(path, index=False)
    elif fmt == "xlsx":
        df.to_excel(path, index=False)     # needs openpyxl; ImportError is reported, not raised
    elif fmt == "parquet":
        df.to_parquet(path)
    else:
        df.to_json(path, orient="records")


def _write_output(session: Session, filename: str, write) -> str:
    """Write ``outputs/<filename>`` without ever following a link sandboxed code planted.

    ``outputs/`` itself is resolved through ``safe_path`` (a symlinked dir that escapes is
    refused), the bytes go to a fresh temp file in it, and ``os.replace`` swaps that in, which
    replaces a planted symlink at the destination instead of writing through it.
    """
    try:
        outputs = safe_path(session.root, _OUTPUTS)
    except PathEscapesRootError as e:
        raise ValueError("outputs/ resolves outside the workspace") from e
    outputs.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=outputs, prefix=".publish_")
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, outputs / filename)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return f"{_OUTPUTS}/{filename}"
