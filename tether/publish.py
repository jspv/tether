"""Publishing workspace files to the host: types and parent-side validation.

A publication request names a file in the session root, from the ``publish_file`` tool or from
``publish()`` inside sandboxed code. Either way the request is untrusted, so the parent
re-validates it here before the host callback ever sees it, and the model sees only metadata.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import PathEscapesRootError, safe_filename, safe_path

_INTERNAL_DIRS = ("handles", ".scripts")
_CHUNK = 1024 * 1024


class PublishError(ValueError):
    """A publication request was rejected (it never reaches the host callback)."""


@dataclass(frozen=True)
class PublishedFile:
    """What the host callback receives. ``path`` is valid only during the callback."""
    path: Path                 # absolute host path
    rel_path: str              # POSIX path relative to the session root
    name: str                  # sanitized display name
    size: int
    sha256: str
    content_type: str | None   # advisory guess (mimetypes); the host decides
    description: str | None
    source: str                # "tool:publish_file" | "tool:publish_handle" | "run_python"


OnPublish = Callable[[PublishedFile], "dict | None"]


def validate_publication(root: Path | str, path: object, *, name: object, description: object,
                         source: str, max_bytes: int,
                         snapshot_dir: Path | None = None) -> PublishedFile:
    """Validate a publication request and describe the file. Raises ``PublishError``.

    Order matters: ``lstat`` on the *unresolved* path rejects a symlink before anything follows
    it; ``safe_path`` then catches ``..``, absolute paths outside the root, and symlinked parent
    directories that escape; internal trees and control files are refused; finally the file is
    opened with ``O_NOFOLLOW`` and size + digest are taken from that descriptor, so they describe
    the bytes that were actually checked.

    With ``snapshot_dir``, the checked bytes are also copied (from that same descriptor) into a
    file there, and ``PublishedFile.path`` points at the copy: the host then reads a private
    snapshot that sandboxed code cannot swap or modify, never the workspace path itself.
    Any path the OS cannot represent (NUL, lone surrogates) is a ``PublishError`` too.
    """
    if not isinstance(path, str) or not path.strip():
        raise PublishError(f"invalid publication path: {path!r}")
    root = Path(root).resolve()
    candidate = Path(path)
    unresolved = candidate if candidate.is_absolute() else root / candidate
    try:
        st = os.lstat(unresolved)
    except (OSError, ValueError) as e:
        raise PublishError(f"no such file: {path!r}") from e
    if stat.S_ISLNK(st.st_mode):
        raise PublishError(f"refusing to publish a symlink: {path!r}")
    if not stat.S_ISREG(st.st_mode):
        raise PublishError(f"not a regular file: {path!r}")
    try:
        resolved = safe_path(root, path)
    except PathEscapesRootError as e:
        raise PublishError(f"path is outside the workspace: {path!r}") from e
    except (OSError, ValueError) as e:
        raise PublishError(f"invalid publication path: {path!r}") from e
    rel = resolved.relative_to(root)
    if rel.parts[0] in _INTERNAL_DIRS or rel.parts[0].startswith("_"):
        raise PublishError(f"refusing to publish an internal tether file: {rel.as_posix()!r}")

    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError) as e:
        raise PublishError(f"cannot open {path!r}: {e}") from e
    display = safe_filename(name if isinstance(name, str) else None, fallback=resolved.name)
    with os.fdopen(fd, "rb") as f:
        fst = os.fstat(f.fileno())
        if not stat.S_ISREG(fst.st_mode):
            raise PublishError(f"not a regular file: {path!r}")
        if fst.st_size > max_bytes:
            raise PublishError(f"file too large to publish ({fst.st_size} bytes > {max_bytes})")
        snapshot = Path(snapshot_dir) / display if snapshot_dir is not None else None
        out = open(snapshot, "xb") if snapshot is not None else None
        try:
            digest = hashlib.sha256()
            size = 0
            while chunk := f.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise PublishError(f"file too large to publish (> {max_bytes} bytes)")
                digest.update(chunk)
                if out is not None:
                    out.write(chunk)
        finally:
            if out is not None:
                out.close()

    return PublishedFile(
        path=snapshot or resolved, rel_path=rel.as_posix(), name=display, size=size,
        sha256=digest.hexdigest(), content_type=mimetypes.guess_type(display)[0],
        description=description if isinstance(description, str) else None, source=source,
    )
