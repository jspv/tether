"""Sandbox backends for running agent scripts, root-confined.

``_OrchestratedSandbox`` owns the shared, backend-agnostic control-file orchestration; each
backend implements only ``_launch`` (how the child process is run). ``LocalSubprocessSandbox``
runs a scrubbed-env child with rlimits; the container backend lives in ``sandbox_container``.
See ``_OrchestratedSandbox`` for the (sequential, non-reentrant-per-root) run contract.
"""

from __future__ import annotations

import json
import os
import resource
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import SandboxConfig
from .handles import HandleStore
from .paths import safe_path

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_RUNNER = _RUNTIME_DIR / "_runner.py"
_SCRIPTS_DIR = ".scripts"
# Bounds on the child -> parent publish channel; requests are untrusted input.
_MAX_PUBLISH_CONTROL_BYTES = 1024 * 1024
_MAX_PUBLISH_REQUESTS = 64
# Handle fields only the parent may set: an input record must come from Session.add_input.
_PARENT_ONLY_FIELDS = ("input_id", "content_type", "description")

# publisher(path, *, name, description, source) -> result record (see Session.publish)
Publisher = Callable[..., dict]


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    result: Any | None
    error: str | None
    exit_code: int
    new_handles: list[str] = field(default_factory=list)
    killed_by: str | None = None
    published: list[dict] = field(default_factory=list)


class SandboxRuntimeUnavailable(RuntimeError):
    """Raised when the container backend is selected but no container runtime exists, or when
    isolation is required and the selected backend cannot provide it."""


class SandboxExecutor(Protocol):
    """Swappable execution backend. Local now; container/remote later."""

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult: ...
    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult: ...


@dataclass
class _LaunchResult:
    """What a backend's _launch returns: raw process outcome, pre-parse."""
    stdout: str
    stderr: str
    exit_code: int
    killed_by: str | None = None


@dataclass
class _RunContext:
    """Everything a backend needs to launch one run. Control-file paths are HOST paths
    (the parent reads/writes them); a backend translates them to its own namespace."""
    script_rel: str          # user script path relative to root
    argv: list[str]          # user args (already str-coerced)
    root: Path
    registry_file: Path      # host path
    new_handles_file: Path   # host path
    emit_file: Path          # host path
    config: SandboxConfig
    publish_file: Path | None = None   # host path; None when publishing is disabled


class _OrchestratedSandbox:
    """Shared control-file orchestration. Subclasses implement ``_launch``.

    Runs are serialized by ``self.lock`` (an ``RLock`` the Session also takes for its own file
    work in the root -- publishing, input ingestion), so no sandboxed code is running while the
    parent validates or writes paths under the child-writable root. Each run's control files use
    a random token, so sandboxed code cannot pre-plant a link at a future run's control path.
    """

    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self.publisher: Publisher | None = None   # set by Session when the host can receive files
        self.lock = threading.RLock()             # shared with the Session; see class docstring

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        with self.lock:
            scripts = self.root / _SCRIPTS_DIR
            scripts.mkdir(exist_ok=True)
            # Collision-free across instances/processes; scripts persist as debuggable artifacts.
            fd, abspath = tempfile.mkstemp(prefix="inline_", suffix=".py", dir=scripts)
            os.close(fd)
            Path(abspath).write_text(code, encoding="utf-8")
            rel = str(Path(abspath).relative_to(self.root))
            return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        with self.lock:
            return self._run_script(path, args)

    def _run_script(self, path: str, args: list[str] | None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside
        argv = [str(a) for a in (args or [])]  # coerce so non-str args fail clearly, not opaquely

        token = secrets.token_hex(8)
        new_handles_file = self.root / f"_new_handles_{token}.jsonl"
        emit_file = self.root / f"_emit_{token}.json"
        registry_file = self.root / f"_registry_{token}.json"
        publish_file = self.root / f"_publish_{token}.jsonl" if self.publisher else None

        try:
            new_handles_file.write_text("", encoding="utf-8")
            if publish_file is not None:
                # Exclusive + no-follow: never truncate or adopt something planted at this path.
                os.close(os.open(publish_file,
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            registry = {hid: {"kind": h.kind, "path": h.path}
                        for hid, h in self.store.manifest_handles().items()}
            registry_file.write_text(json.dumps(registry), encoding="utf-8")

            ctx = _RunContext(
                script_rel=str(script.relative_to(self.root)), argv=argv, root=self.root,
                registry_file=registry_file, new_handles_file=new_handles_file,
                emit_file=emit_file, config=self.config, publish_file=publish_file,
            )
            launched = self._launch(ctx)

            result = None
            emit_error = None
            if launched.exit_code == 0 and emit_file.exists():
                try:
                    result = json.loads(emit_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    emit_error = f"tether: malformed emit payload: {e}"

            # Ergonomic fallback: if the script neither emitted nor ended in an expression but
            # printed something, surface that so a model that just print()s an answer still gets one.
            if (result is None and emit_error is None and launched.exit_code == 0
                    and launched.stdout.strip()):
                result = launched.stdout.strip()

            new_handles = self._ingest_new_handles(new_handles_file)
            published, publish_error = self._process_publications(publish_file,
                                                                  launched.exit_code)
            base_error = (launched.stderr.strip() or None) if launched.exit_code != 0 else None
            error = "\n".join(p for p in (base_error, emit_error, publish_error) if p) or None

            return ExecResult(stdout=launched.stdout, stderr=launched.stderr, result=result,
                              error=error, exit_code=launched.exit_code,
                              new_handles=new_handles, killed_by=launched.killed_by,
                              published=published)
        finally:
            for f in (new_handles_file, emit_file, registry_file, publish_file):
                if f is not None:
                    f.unlink(missing_ok=True)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        """Register handles the child wrote. Tolerant: a corrupt line is skipped, not fatal, so
        one bad record can't abort ingestion or leave the store inconsistent."""
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                for key in _PARENT_ONLY_FIELDS:   # sandboxed code cannot mint an input
                    rec.pop(key, None)
                self.store.register(rec)
                ids.append(rec["id"])
            except (json.JSONDecodeError, ValueError, KeyError, AttributeError):
                continue
        return ids

    def _process_publications(self, publish_file: Path | None,
                              exit_code: int) -> tuple[list[dict], str | None]:
        """Hand the child's publish requests to the publisher; return (records, error).

        Runs only after the child has exited, and only on a clean exit, so published files are
        complete. Every request is untrusted: the publisher re-validates the path from scratch.
        The file is read up to ``_MAX_PUBLISH_CONTROL_BYTES`` and at most
        ``_MAX_PUBLISH_REQUESTS`` requests are honored; malformed lines are skipped.
        """
        if publish_file is None or self.publisher is None:
            return [], None
        replaced = ("tether: publish control file was replaced by the script; publication "
                    "requests ignored")
        try:
            # No-follow + non-blocking: a symlink or FIFO planted over the file must neither be
            # followed nor hang the parent.
            fd = os.open(publish_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return [], None
        except OSError:
            return [], replaced
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return [], replaced
            raw = f.read(_MAX_PUBLISH_CONTROL_BYTES + 1)
        lines = [ln for ln in raw[:_MAX_PUBLISH_CONTROL_BYTES].decode(
            "utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            return [], None
        if exit_code != 0:
            return [], (f"tether: {len(lines)} publication request(s) ignored because the "
                        "script did not exit cleanly")
        notes: list[str] = []
        if len(raw) > _MAX_PUBLISH_CONTROL_BYTES:
            notes.append("tether: publish control file truncated (too large)")
        if len(lines) > _MAX_PUBLISH_REQUESTS:
            notes.append(f"tether: too many publication requests (cap {_MAX_PUBLISH_REQUESTS}); "
                         "later requests dropped")
        records: list[dict] = []
        for line in lines[:_MAX_PUBLISH_REQUESTS]:
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(req, dict):
                continue
            path, name, desc = req.get("path"), req.get("name"), req.get("description")
            if not isinstance(path, str):
                records.append({"error": "invalid publication request (path must be a string)"})
                continue
            try:
                records.append(self.publisher(
                    path, name=name if isinstance(name, str) else None,
                    description=desc if isinstance(desc, str) else None, source="run_python"))
            except Exception as e:  # noqa: BLE001 - one bad request must not lose the run
                records.append({"error": f"publication failed: {type(e).__name__}: {e}"})
        return records, "\n".join(notes) or None

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        raise NotImplementedError


class LocalSubprocessSandbox(_OrchestratedSandbox):
    """Runs the script in a scrubbed-env child process with rlimits + a wall-clock timeout.

    **This tier is not a security boundary**: the code runs as the host user, sharing the
    filesystem beyond the root and the network. The rlimits and timeout bound resource use,
    not privilege. Use the container backend for a real boundary.
    """

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        script_abs = ctx.root / ctx.script_rel
        env = {
            "PATH": _minimal_path(),
            "HOME": str(ctx.root),
            "TMPDIR": str(ctx.root),
            "TETHER_ROOT": str(ctx.root),
            "TETHER_REGISTRY": str(ctx.registry_file),
            "TETHER_NEW_HANDLES": str(ctx.new_handles_file),
            "TETHER_EMIT": str(ctx.emit_file),
            "PYTHONPATH": str(_RUNTIME_DIR),
        }
        if ctx.publish_file is not None:
            env["TETHER_PUBLISH"] = str(ctx.publish_file)
        try:
            proc = subprocess.run(
                [sys.executable, str(_RUNNER), str(script_abs), *ctx.argv],
                cwd=ctx.root, env=env, capture_output=True, text=True,
                timeout=ctx.config.timeout_s, preexec_fn=self._limits(),
            )
            return _LaunchResult(proc.stdout, proc.stderr, proc.returncode)
        except subprocess.TimeoutExpired as e:
            return _LaunchResult(_as_text(e.stdout),
                                 _as_text(e.stderr) + "\ntether: killed (timeout)",
                                 -1, killed_by="timeout")

    def _limits(self):
        cfg = self.config

        def set_limits() -> None:
            mem = cfg.max_memory_mb * 1024 * 1024
            fsize = cfg.max_file_size_mb * 1024 * 1024
            for res_id, limit in (
                (resource.RLIMIT_AS, mem),      # may be rejected on macOS — best-effort
                (resource.RLIMIT_FSIZE, fsize),
                (resource.RLIMIT_CORE, 0),
            ):
                try:
                    resource.setrlimit(res_id, (limit, limit))
                except (ValueError, OSError):
                    pass

        return set_limits


def _minimal_path() -> str:
    return "/usr/bin:/bin:/usr/local/bin"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
