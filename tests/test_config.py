from tether.config import FetchConfig, TetherConfig, SandboxConfig


def test_defaults_are_sensible():
    cfg = TetherConfig()
    assert cfg.model == "gpt-5-mini"
    assert cfg.spill_threshold_bytes == 8192
    assert cfg.max_spill_bytes == 100 * 1024 * 1024
    assert cfg.max_spill_bytes > cfg.spill_threshold_bytes  # a real spill-over zone
    assert isinstance(cfg.sandbox, SandboxConfig)
    assert isinstance(cfg.fetch, FetchConfig)
    assert cfg.sandbox.timeout_s == 30.0
    assert "pandas" in cfg.sandbox.preinstalled
    assert cfg.fetch.allowed_schemes == ("http", "https")
    assert cfg.documents.ocr is False        # OCR off by default (born-digital docs)


def test_sandbox_backend_defaults_and_container_fields():
    cfg = TetherConfig()
    assert cfg.sandbox.network is False              # network off by default
    assert cfg.sandbox.pip_packages == ()
    assert cfg.sandbox.container_runtime is None      # auto-detect
    assert cfg.sandbox.max_cpus == 2.0
    assert not hasattr(cfg.sandbox, "confine_os")     # removed


def test_nested_configs_are_independent_between_instances():
    a = TetherConfig()
    b = TetherConfig()
    assert a.sandbox is not b.sandbox  # field(default_factory=...) not shared


def test_control_plane_caps_have_defaults():
    cfg = TetherConfig()
    assert cfg.max_emit_bytes == 1024 * 1024
    assert cfg.max_control_bytes == 8 * 1024 * 1024
    assert cfg.max_new_handles == 256


def test_container_is_the_default_backend(monkeypatch):
    """Real isolation by default.

    The suite's autouse fixture pins ``local`` via TETHER_SANDBOX_BACKEND, which would
    otherwise hide a regression in the shipped default -- so this test deletes the
    variable and asserts what a user actually gets.
    """
    monkeypatch.delenv("TETHER_SANDBOX_BACKEND", raising=False)
    assert SandboxConfig().backend == "container"
    assert TetherConfig().sandbox.backend == "container"


def test_sandbox_backend_env_override(monkeypatch):
    monkeypatch.setenv("TETHER_SANDBOX_BACKEND", "local")
    assert SandboxConfig().backend == "local"
    assert SandboxConfig(backend="container").backend == "container"   # explicit arg wins
