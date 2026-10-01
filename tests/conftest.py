"""Suite-wide defaults.

Tests must never touch the network. Wiring the egress guard into fetch_url means any
call now resolves hostnames for real unless something intervenes, so resolution is
stubbed here for the whole suite: no individual test can reach DNS by accident.

A test that needs a specific resolution result injects its own ``resolve=`` (the egress
tests do), and a test asserting denial can use a literal IP, which skips resolution
entirely.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    def _stub(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr("tether.egress._resolve", _stub)


@pytest.fixture(autouse=True)
def _disabled_local_sandbox(monkeypatch):
    """Pin the sandbox backend to ``local`` for every test.

    The shipped default is ``container`` (real isolation). The suite must stay offline,
    fast, and runnable without podman or docker, so every test gets ``local`` unless it
    asks otherwise. Tests of the container tier build their own SandboxConfig and are
    gated on a usable runtime.

    This pins the backend through the environment variable the field's default_factory
    reads. Do NOT use ``monkeypatch.setattr(SandboxConfig, "backend", "local")`` -- that is
    a silent no-op, because a dataclass bakes its defaults into ``__init__.__defaults__``
    at class-creation time. ``test_config.py`` asserts the *shipped* default and therefore
    deletes the variable rather than relying on this fixture.
    """
    monkeypatch.setenv("TETHER_SANDBOX_BACKEND", "local")


@pytest.fixture(autouse=True)
def _quiet_no_isolation_warning():
    """The suite deliberately runs the local tier; keep its warning out of the output.

    ``pytest.warns`` installs its own filter, so the test asserting the warning is emitted
    is unaffected by this.
    """
    import warnings

    from tether.sandbox import NoSandboxIsolationWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NoSandboxIsolationWarning)
        yield
