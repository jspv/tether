import shutil
import uuid
from pathlib import Path

import pytest

from tether.config import SandboxConfig
from tether.container_runtime import require_usable_runtime
from tether.handles import HandleStore
from tether.sandbox import LocalSubprocessSandbox
from tether.sandbox_container import ContainerSandbox



def _probe_runtime() -> tuple[str | None, str]:
    # Liveness, not presence: podman can be on PATH with its VM stopped, in which case these
    # tests would run and fail rather than skip.
    try:
        return require_usable_runtime(None), ""
    except RuntimeError as e:
        return None, str(e)


_RUNTIME, _SKIP_REASON = _probe_runtime()
pytestmark = pytest.mark.skipif(
    _RUNTIME is None, reason=f"no usable container runtime: {_SKIP_REASON}")


@pytest.fixture
def croot():
    # The container runtime (on macOS) shares $HOME but not the system temp, so the bind-mounted
    # session root must live under $HOME. Fresh dir per test; cleaned up after.
    d = Path.home() / ".tether" / "_ctest" / uuid.uuid4().hex
    d.mkdir(parents=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _container(root, **cfg):
    store = HandleStore(root)
    return ContainerSandbox(root=root, store=store,
                            config=SandboxConfig(backend="container", **cfg)), store


def test_runs_code_and_captures_result(croot):
    sb, _ = _container(croot)
    res = sb.run_code("from tether_sandbox import emit\nemit(6 * 7)\n")
    assert res.error is None, res.error
    assert res.result == 42


def test_network_is_blocked_by_default(croot):
    sb, _ = _container(croot)
    res = sb.run_code(
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "from tether_sandbox import emit\nemit('reached')\n"
    )
    assert res.result != "reached"          # the connection must fail
    assert res.exit_code != 0


def test_network_can_be_enabled(croot):
    sb, _ = _container(croot, network=True)
    res = sb.run_code("from tether_sandbox import emit\nemit('ok')\n")
    assert res.result == "ok"


def test_saved_handle_is_ingested(croot):
    sb, _ = _container(croot)
    res = sb.run_code("from tether_sandbox import save\nsave('h1', {'x': 1})\n")
    assert "h1" in res.new_handles


def test_pip_packages_are_importable(croot):
    sb, _ = _container(croot, pip_packages=("six",))   # tiny, pure-python
    res = sb.run_code("import six\nfrom tether_sandbox import emit\nemit(six.__name__)\n")
    assert res.error is None, res.error
    assert res.result == "six"


def test_local_and_container_parity(croot):
    code = "x = sum(range(10))\nfrom tether_sandbox import emit\nemit(x)\n"
    (croot / "c").mkdir()
    (croot / "l").mkdir()
    cont = ContainerSandbox(root=croot / "c", store=HandleStore(croot / "c"),
                            config=SandboxConfig(backend="container"))
    loc = LocalSubprocessSandbox(root=croot / "l", store=HandleStore(croot / "l"),
                                 config=SandboxConfig())
    assert cont.run_code(code).result == loc.run_code(code).result == 45
