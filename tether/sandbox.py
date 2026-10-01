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
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import SandboxConfig
from .handles import HandleStore
from .paths import safe_path

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_RUNNER = _RUNTIME_DIR / "_runner.py"
_SCRIPTS_DIR = ".scripts"


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    result: Any | None
    error: str | None
    exit_code: int
    new_handles: list[str] = field(default_factory=list)
    killed_by: str | None = None


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


class _OrchestratedSandbox:
    """Shared control-file orchestration. Subclasses implement ``_launch``.

    Sequential and NOT re-entrant per root: each run uses per-run-unique control files
    (keyed by pid + run counter), so sequential runs never clobber each other.
    """

    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self._run_counter = 0

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        scripts = self.root / _SCRIPTS_DIR
        scripts.mkdir(exist_ok=True)
        # Collision-free across instances/processes; scripts persist as debuggable artifacts.
        fd, abspath = tempfile.mkstemp(prefix="inline_", suffix=".py", dir=scripts)
        os.close(fd)
        Path(abspath).write_text(code, encoding="utf-8")
        rel = str(Path(abspath).relative_to(self.root))
        return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside
        argv = [str(a) for a in (args or [])]  # coerce so non-str args fail clearly, not opaquely

        self._run_counter += 1
        token = f"{os.getpid()}_{self._run_counter}"
        new_handles_file = self.root / f"_new_handles_{token}.jsonl"
        emit_file = self.root / f"_emit_{token}.json"
        registry_file = self.root / f"_registry_{token}.json"

        try:
            new_handles_file.write_text("", encoding="utf-8")
            registry = {hid: {"kind": h.kind, "path": h.path}
                        for hid, h in self.store.manifest_handles().items()}
            registry_file.write_text(json.dumps(registry), encoding="utf-8")

            ctx = _RunContext(
                script_rel=str(script.relative_to(self.root)), argv=argv, root=self.root,
                registry_file=registry_file, new_handles_file=new_handles_file,
                emit_file=emit_file, config=self.config,
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
            base_error = (launched.stderr.strip() or None) if launched.exit_code != 0 else None
            error = "\n".join(p for p in (base_error, emit_error) if p) or None

            return ExecResult(stdout=launched.stdout, stderr=launched.stderr, result=result,
                              error=error, exit_code=launched.exit_code,
                              new_handles=new_handles, killed_by=launched.killed_by)
        finally:
            for f in (new_handles_file, emit_file, registry_file):
                f.unlink(missing_ok=True)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        """Adopt handles the child wrote.

        Records are untrusted: ``adopt`` derives all metadata from the file itself and
        refuses an id that already exists. Tolerant by design -- a rejected or corrupt
        record is skipped, not fatal, so one bad record cannot abort ingestion or leave
        the store inconsistent.
        """
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                handle = self.store.adopt(id=rec["id"], kind=rec["kind"],
                                          path=rec["path"], source=rec.get("source", "run_python"))
                ids.append(handle.id)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                continue
        return ids

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        raise NotImplementedError


class LocalSubprocessSandbox(_OrchestratedSandbox):
    """Runs the script in a scrubbed-env child process with rlimits + a wall-clock timeout."""

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
