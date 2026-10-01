"""ContainerSandbox: run agent code in a hardened OCI container (podman/docker).

Reuses the shared orchestration in ``_OrchestratedSandbox`` (control files, ExecResult parsing)
and only differs in ``_launch``: it bind-mounts the session root at /workspace and the tether
runtime/ at /runtime (read-only), translates the control-file paths into the container, and runs
``python /runtime/_runner.py <script-rel> <args>`` with network off (by default), a read-only
root filesystem, dropped capabilities, and memory/cpu/pid limits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import SandboxConfig
from .sandbox import (
    _RUNTIME_DIR,
    ControlPlaneLimits,
    _LaunchResult,
    _OrchestratedSandbox,
    _RunContext,
    _as_text,
)

_PIDS_LIMIT = 256


class ContainerSandbox(_OrchestratedSandbox):
    def __init__(self, root: Path | str, store, config: SandboxConfig | None = None,
                 runtime: str | None = None,
                 limits: ControlPlaneLimits | None = None) -> None:
        super().__init__(root, store, config, limits)
        if runtime is not None:
            self._runtime = runtime
        else:
            from .container_runtime import detect_runtime
            self._runtime = detect_runtime(self.config.container_runtime)

    def _build_run_argv(self, ctx: _RunContext, tag: str, layer: Path | None) -> list[str]:
        cfg = self.config
        argv = [
            self._runtime, "run", "--rm",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(_PIDS_LIMIT),
            "--memory", f"{cfg.max_memory_mb}m", "--cpus", str(cfg.max_cpus),
        ]
        # Rootless podman maps the host user to container-root by default, so a bind-mounted
        # session root (owned by the host uid, 0600 scripts) is unreadable/unwritable to the
        # hardening --user below. keep-id maps the host uid straight through, so ownership of the
        # mounted workspace lines up with the --user we run as. (podman-only flag; docker rootless
        # uses a different model and rejects it.)
        if self._runtime == "podman":
            argv += ["--userns=keep-id"]
        argv += [
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{ctx.root}:/workspace:rw",
            "-v", f"{_RUNTIME_DIR}:/runtime:ro",
            "-w", "/workspace",
        ]
        if not cfg.network:
            argv += ["--network", "none"]
        pythonpath = "/runtime"
        if layer is not None:
            argv += ["-v", f"{layer}:/pkgs:ro"]
            pythonpath = "/runtime:/pkgs"
        env = {
            "TETHER_ROOT": "/workspace",
            "TETHER_REGISTRY": f"/workspace/{ctx.registry_file.name}",
            "TETHER_NEW_HANDLES": f"/workspace/{ctx.new_handles_file.name}",
            "TETHER_EMIT": f"/workspace/{ctx.emit_file.name}",
            "PYTHONPATH": pythonpath,
            "HOME": "/workspace",
            "TMPDIR": "/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [tag, "python", "/runtime/_runner.py", ctx.script_rel, *ctx.argv]
        return argv

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        from .container_runtime import ensure_image, ensure_layer, image_tag

        tag = image_tag(self.config.preinstalled)
        ensure_image(self._runtime, tag, self.config)
        layer = ensure_layer(self._runtime, self.config) if self.config.pip_packages else None
        argv = self._build_run_argv(ctx, tag, layer)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=ctx.config.timeout_s)
            # 137 == 128 + SIGKILL: the runtime killed the container, typically the memory cap.
            killed_by = "killed" if proc.returncode == 137 else None
            return _LaunchResult(proc.stdout, proc.stderr, proc.returncode, killed_by=killed_by)
        except subprocess.TimeoutExpired as e:
            return _LaunchResult(_as_text(e.stdout),
                                 _as_text(e.stderr) + "\ntether: killed (timeout)",
                                 -1, killed_by="timeout")
