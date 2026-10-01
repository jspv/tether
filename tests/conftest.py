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
