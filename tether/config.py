"""Typed configuration for the tether."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class SandboxConfig:
    """How ``run_python`` code is executed.

    ``backend="local"`` runs code in a scrubbed-env subprocess with rlimits. It is **not a
    security boundary**: the code runs as the host user with the host user's file and network
    access. Use it only for development or fully trusted input. ``backend="container"`` runs
    code in a hardened OCI container (no network by default, read-only root filesystem, all
    capabilities dropped, ``no-new-privileges``, pids/memory/cpu limits, only the session root
    and the tether runtime mounted, no host environment).

    ``require_isolation=True`` makes the boundary mandatory: a local backend is refused at
    ``Session.create`` and a missing runtime or image raises, never falling back.
    ``build_on_demand`` controls whether a missing image or pip layer is built on first use;
    ``None`` means "build unless isolation is required" (a host requiring isolation should
    pre-build with ``tether-build-sandbox`` and gate startup on ``sandbox_preflight``).
    """

    timeout_s: float = 30.0
    max_memory_mb: int = 1024
    max_file_size_mb: int = 512        # enforced by the local tier only (no container equivalent)
    backend: Literal["local", "container"] = "local"
    container_runtime: str | None = None   # None -> auto-detect podman, then docker
    network: bool = False                  # sandbox network off by default; opt-in to enable
    pip_packages: tuple[str, ...] = ()     # provisioned into a mounted layer (network only there)
    max_cpus: float = 2.0
    preinstalled: tuple[str, ...] = ("pandas", "pyarrow", "numpy", "httpx")
    require_isolation: bool = False        # refuse to run without a real boundary
    build_on_demand: bool | None = None    # None -> auto: build unless require_isolation
    build_timeout_s: float = 900.0         # bound on image build + pip layer provisioning

    @property
    def effective_build_on_demand(self) -> bool:
        if self.build_on_demand is not None:
            return self.build_on_demand
        return not self.require_isolation


@dataclass
class FetchConfig:
    max_bytes: int = 10_000_000
    timeout_s: float = 30.0
    allowed_schemes: tuple[str, ...] = ("http", "https")


@dataclass
class SearchConfig:
    provider: str = "tavily"
    api_key: str | None = None
    max_results: int = 5
    timeout_s: float = 20.0


@dataclass
class DocumentConfig:
    # OCR is off by default: born-digital PDFs/Office files get tables and structure from
    # the layout/TableFormer models, so OCR only adds latency + model downloads. Turn it on
    # for scanned/image documents.
    ocr: bool = False


@dataclass
class TetherConfig:
    model: str = "gpt-5-mini"  # only used by the built-in OpenAI client; ignored when you inject a client
    spill_threshold_bytes: int = 8192          # lower edge: tool returns over this become handles
    max_spill_bytes: int = 100 * 1024 * 1024   # upper edge: a return over this is rejected, not stored
    max_context_window_tokens: int = 128_000
    max_output_tokens: int = 4096
    max_publish_bytes: int = 100 * 1024 * 1024  # largest file publish_file/publish() will deliver
    max_input_bytes: int = 100 * 1024 * 1024    # largest user upload add_input() will accept
    root_dir: Path | None = None  # None -> a session dir is created under ./.tether/sessions/
    cleanup: bool = False  # delete the root on async-context exit (throwaway runs)
    idle_ttl_s: float | None = None  # continuous-session idle TTL (None = never expire)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    documents: DocumentConfig = field(default_factory=DocumentConfig)
