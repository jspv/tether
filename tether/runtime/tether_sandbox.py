"""Injected into the sandbox subprocess as the top-level module ``tether_sandbox``.

Communicates with the parent tether only via env vars and files:
  TETHER_ROOT         session root directory
  TETHER_REGISTRY     json file: { handle_id: {kind, path} } for existing handles
  TETHER_NEW_HANDLES  jsonl file this module appends new-handle records to
  TETHER_EMIT         json file this module writes the emit() payload to
  TETHER_PUBLISH      jsonl file publish() appends requests to (absent: publishing disabled)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(os.environ["TETHER_ROOT"])
_REGISTRY = json.loads(Path(os.environ["TETHER_REGISTRY"]).read_text(encoding="utf-8"))
_NEW = Path(os.environ["TETHER_NEW_HANDLES"])
_SAVED_THIS_RUN: set[str] = set()  # ids written by save() in this process
_EMIT = Path(os.environ["TETHER_EMIT"])

def load(handle_id: str) -> Any:
    meta = _REGISTRY[handle_id]
    path = _ROOT / meta["path"]
    kind = meta["kind"]
    if kind == "dataframe":
        import pandas as pd
        return pd.read_parquet(path)
    if kind == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    if kind == "binary":
        return str(path)  # binary file: hand back the path to open with pandas/Docling/etc.
    return path.read_text(encoding="utf-8")


def save(handle_id: str, obj: Any, source: str = "run_python") -> str:
    """Write ``obj`` as a handle file and tell the parent it exists.

    Only id/kind/path/source are reported: the parent derives every field that reaches
    model context from the bytes on disk, so there is nothing to keep in sync here and
    nothing this side can misreport.
    """
    import pandas as pd

    # Ergonomics for honest code only, NOT a security control: a hostile child can skip
    # save() and write the control file directly. The parent's refusal in adopt() is the
    # real boundary.
    if handle_id in _REGISTRY or handle_id in _SAVED_THIS_RUN:
        raise ValueError(
            f"handle id {handle_id!r} already exists and handles are immutable; "
            f"save under a new id"
        )

    if isinstance(obj, pd.DataFrame):
        kind, rel = "dataframe", f"handles/{handle_id}.parquet"
        obj.to_parquet(_ROOT / rel)
    elif isinstance(obj, (dict, list)):
        kind, rel = "json", f"handles/{handle_id}.json"
        (_ROOT / rel).write_text(json.dumps(obj, default=str), encoding="utf-8")
    else:
        kind, rel = "text", f"handles/{handle_id}.txt"
        (_ROOT / rel).write_text(str(obj), encoding="utf-8")

    with _NEW.open("a") as f:
        f.write(json.dumps({"id": handle_id, "kind": kind, "path": rel,
                            "source": source}) + "\n")
    _SAVED_THIS_RUN.add(handle_id)
    return handle_id


def emit(obj: Any) -> None:
    _EMIT.write_text(json.dumps(obj, default=str), encoding="utf-8")


def publish(path: str, name: str | None = None, description: str | None = None) -> None:
    """Ask the host to deliver a finished workspace file to the user.

    The request is recorded here and processed by the parent after this script exits cleanly
    (the parent re-validates it; this side is not trusted). Raises if the host has not enabled
    publishing for this session.
    """
    target = os.environ.get("TETHER_PUBLISH")
    if not target:
        raise RuntimeError("publishing is not enabled for this session "
                           "(the host has not configured a publish callback)")
    p = os.path.abspath(os.fspath(path))
    root = os.path.abspath(str(_ROOT))
    if os.path.commonpath([p, root]) == root:
        p = os.path.relpath(p, root)   # convenience: /workspace/x -> x (not a trust step)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps({"path": p, "name": name, "description": description}) + "\n")
