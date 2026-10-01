import shutil
import uuid
from pathlib import Path

import pytest

from tether.config import SandboxConfig
from tether.handles import HandleStore
from tether.sandbox import LocalSubprocessSandbox
from tether.sandbox_container import ContainerSandbox

_RUNTIME = "podman" if shutil.which("podman") else ("docker" if shutil.which("docker") else None)
pytestmark = pytest.mark.skipif(_RUNTIME is None, reason="no podman/docker runtime available")


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


# --- TETHER-1 isolation guarantees (acceptance) -------------------------------------------

def test_host_file_outside_root_is_not_readable(croot):
    secret = croot.parent / f"{croot.name}_outside.txt"     # a real host file beside the root
    secret.write_text("host-secret", encoding="utf-8")
    try:
        sb, _ = _container(croot)
        res = sb.run_code(
            "from tether_sandbox import emit\n"
            f"try:\n    emit(open({str(secret)!r}).read())\n"
            "except OSError as e:\n    emit('denied:' + type(e).__name__)\n")
        assert res.error is None, res.error
        assert res.result.startswith("denied:")
    finally:
        secret.unlink(missing_ok=True)


def test_parent_process_environ_is_not_the_host(croot, monkeypatch):
    monkeypatch.setenv("TETHER_LIVE_HOST_SENTINEL", "host-sentinel-value")
    sb, _ = _container(croot)
    res = sb.run_code(
        "import os\nfrom tether_sandbox import emit\n"
        "try:\n    data = open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        "except OSError as e:\n    data = b''\n"
        "emit(b'host-sentinel-value' in data)\n")
    assert res.error is None, res.error
    assert res.result is False


def test_host_env_var_is_not_visible(croot, monkeypatch):
    monkeypatch.setenv("TETHER_LIVE_HOST_SENTINEL", "host-sentinel-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret")
    sb, _ = _container(croot)
    res = sb.run_code(
        "import os\nfrom tether_sandbox import emit\n"
        "emit([os.environ.get('TETHER_LIVE_HOST_SENTINEL'), os.environ.get('OPENAI_API_KEY')])\n")
    assert res.error is None, res.error
    assert res.result == [None, None]


def test_outbound_connection_fails(croot):
    sb, _ = _container(croot)
    res = sb.run_code(
        "import socket\nfrom tether_sandbox import emit\n"
        "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=3)\n    emit('connected')\n"
        "except OSError:\n    emit('blocked')\n")
    assert res.result == "blocked"


def test_workspace_write_appears_in_host_root(croot):
    sb, _ = _container(croot)
    res = sb.run_code(
        "import os\nos.makedirs('/workspace/outputs', exist_ok=True)\n"
        "open('/workspace/outputs/a.csv', 'w').write('x,y\\n1,2\\n')\n")
    assert res.error is None, res.error
    assert (croot / "outputs" / "a.csv").read_text() == "x,y\n1,2\n"


def test_inputs_are_read_only_in_container(croot):
    from tether import Session, TetherConfig
    sess = Session.create(TetherConfig(root_dir=croot,
                                       sandbox=SandboxConfig(backend="container")))
    h = sess.add_input(b"a,b\n1,2\n", input_id="f1", name="data.csv")
    res = sess.sandbox.run_code(
        "import os\nfrom tether_sandbox import load, emit\n"
        f"p = load({h.id!r})\ndata = open(p).read()\n"
        "def attempt(fn):\n"
        "    try:\n        fn()\n        return 'allowed'\n"
        "    except OSError:\n        return 'denied'\n"
        "emit([data, attempt(lambda: os.chmod(p, 0o644)),\n"
        "      attempt(lambda: open(p, 'a').write('x')), attempt(lambda: os.remove(p))])\n")
    assert res.error is None, res.error
    assert res.result == ["a,b\n1,2\n", "denied", "denied", "denied"]
    assert (croot / h.path).read_bytes() == b"a,b\n1,2\n"


def test_publish_from_container(croot):
    from tether import Session, TetherConfig
    calls = []
    sess = Session.create(TetherConfig(root_dir=croot,
                                       sandbox=SandboxConfig(backend="container")),
                          on_publish=lambda pf: calls.append(pf) or {"id": 1})
    res = sess.sandbox.run_code(
        "import os\nfrom tether_sandbox import publish\n"
        "os.makedirs('/workspace/outputs', exist_ok=True)\n"
        "open('/workspace/outputs/a.csv', 'w').write('a\\n1\\n')\n"
        "publish('/workspace/outputs/a.csv', name='a.csv')\n")
    assert res.error is None, res.error
    assert [p.rel_path for p in calls] == ["outputs/a.csv"]
    assert res.published[0]["host"] == {"id": 1}


def test_timed_out_container_is_not_left_running(croot):
    import subprocess

    from tether.container_runtime import image_tag

    sb, _ = _container(croot, timeout_s=4)
    res = sb.run_code("import time\ntime.sleep(60)\n")
    assert res.killed_by == "timeout"
    running = subprocess.run(
        [_RUNTIME, "ps", "-q", "--filter", f"ancestor={image_tag(sb.config.preinstalled)}"],
        capture_output=True, text=True).stdout.split()
    assert running == [], "a timed-out sandbox container is still running"
