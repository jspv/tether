"""Container runtime helpers: detect podman/docker, build the sandbox image, provision a
package layer. Driven via the runtime CLI (subprocess) -- no Python container SDK dependency.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import SandboxConfig

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_CONTAINERFILE = _RUNTIME_DIR / "Containerfile"


def detect_runtime(override: str | None, which: Callable[[str], str | None] = shutil.which) -> str:
    """Return the container runtime binary name. Prefers ``override``, then podman, then docker."""
    if override:
        if which(override):
            return override
        raise RuntimeError(f"container runtime {override!r} not found on PATH")
    for candidate in ("podman", "docker"):
        if which(candidate):
            return candidate
    # Facts only. The caller that selected the container backend owns the remediation --
    # see Session._build_sandbox, which wraps this. Repeating "install podman or docker, or
    # set backend='local'" here made the user read the same advice twice, in two different
    # spellings, in one error.
    raise RuntimeError("no container runtime found: neither podman nor docker is on PATH")


_usable_runtime_cache: dict[str | None, str] = {}


def require_usable_runtime(override: str | None, run: Callable = subprocess.run) -> str:
    """Return a runtime that is actually usable, else raise RuntimeError.

    ``detect_runtime`` only checks PATH. A machine with podman installed but its VM not
    started passes that check and then fails at first use with an opaque image-build error.

    **Successes are cached per process, keyed on the override; failures never are.** The
    probe shells out to ``<runtime> info`` with a 30 s timeout, and since the container
    backend became the default this runs on the event loop once per ``Session.create`` --
    that is once per *conversation* on a multi-conversation host, which would pay for the
    same answer over and over. A runtime that answered once is not going to stop being
    installed. The reverse is not true: a user who starts their VM after a failure must get
    a working harness on the next try, not be told to restart the process.
    """
    if override in _usable_runtime_cache:
        return _usable_runtime_cache[override]
    runtime = detect_runtime(override)
    try:
        proc = run([runtime, "info"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"container runtime {runtime!r} is installed but not usable: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="replace").strip().splitlines()
        hint = detail[0] if detail else f"exit {proc.returncode}"
        raise RuntimeError(
            f"container runtime {runtime!r} is installed but not responding ({hint}). "
            f"On macOS this usually means the VM is not started -- try `{runtime} machine start`."
        )
    _usable_runtime_cache[override] = runtime
    return runtime


def _py_tag() -> str:
    return f"py{sys.version_info.major}{sys.version_info.minor}"


def image_tag(preinstalled: tuple[str, ...]) -> str:
    """Stable image tag keyed by the Python version + the (order-independent) preinstalled set."""
    digest = hashlib.sha256((_py_tag() + "|" + ",".join(sorted(preinstalled))).encode()).hexdigest()
    return f"tether-sandbox:{digest[:12]}"


def image_exists(runtime: str, tag: str, run: Callable = subprocess.run) -> bool:
    # `image inspect` works on both podman and docker (unlike podman-only `image exists`).
    return run([runtime, "image", "inspect", tag], capture_output=True).returncode == 0


def ensure_image(runtime: str, tag: str, config: SandboxConfig,
                 run: Callable = subprocess.run) -> None:
    """Build the sandbox image if it isn't present. Raises with the build stderr on failure."""
    if image_exists(runtime, tag, run):
        return
    build = [runtime, "build", "-t", tag,
             "--build-arg", f"PREINSTALLED={' '.join(config.preinstalled)}",
             "-f", str(_CONTAINERFILE), str(_RUNTIME_DIR)]
    proc = run(build, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to build sandbox image {tag}:\n{proc.stderr}")


def layer_dir(config: SandboxConfig, base: Path | None = None) -> Path:
    """Host cache dir for the provisioned package layer, keyed by packages + Python version."""
    base = base or (Path.home() / ".tether" / "pkgcache")
    digest = hashlib.sha256(
        (_py_tag() + "|" + ",".join(sorted(config.pip_packages))).encode()
    ).hexdigest()
    return base / digest[:12]


def ensure_layer(runtime: str, config: SandboxConfig, base: Path | None = None,
                 run: Callable = subprocess.run) -> Path:
    """Provision ``config.pip_packages`` into a mounted layer (network ON, provisioning only).

    Cached by package set; provisions once. A sibling ``<dir>.complete`` sentinel marks success,
    so a crashed/partial provision is re-run rather than served as a broken layer. Returns the
    host layer dir.
    """
    target = layer_dir(config, base)
    sentinel = target.parent / f"{target.name}.complete"
    if sentinel.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    tag = image_tag(config.preinstalled)
    ensure_image(runtime, tag, config, run)
    cmd = [runtime, "run", "--rm"]
    # See ContainerSandbox._build_run_argv: rootless podman maps the host user to container-root,
    # so the host-owned /layer bind mount is unwritable to the hardening --user. keep-id maps the
    # host uid through so --user owns the mount. (podman-only; docker rootless rejects it.)
    if runtime == "podman":
        cmd += ["--userns=keep-id"]
    cmd += ["--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{target}:/layer:rw", tag,
            "pip", "install", "--no-cache-dir", "--target", "/layer", *config.pip_packages]
    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to provision pip_packages into {target}:\n{proc.stderr}")
    sentinel.write_text("ok", encoding="utf-8")   # only after a successful install
    return target


def _build_sandbox_main() -> None:
    """Console-script entry (``tether-build-sandbox``): pre-build the sandbox image."""
    from .config import SandboxConfig

    config = SandboxConfig()
    runtime = detect_runtime(config.container_runtime)
    tag = image_tag(config.preinstalled)
    ensure_image(runtime, tag, config)
    print(f"sandbox image ready: {tag} (via {runtime})")
