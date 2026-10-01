"""Typed configuration for the tether."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class SandboxConfig:
    timeout_s: float = 30.0
    max_memory_mb: int = 1024
    max_file_size_mb: int = 512        # enforced by the local tier only (no container equivalent)
    backend: Literal["local", "container"] = "local"
    container_runtime: str | None = None   # None -> auto-detect podman, then docker
    network: bool = False                  # sandbox network off by default; opt-in to enable
    pip_packages: tuple[str, ...] = ()     # provisioned into a mounted layer (network only there)
    max_cpus: float = 2.0
    preinstalled: tuple[str, ...] = ("pandas", "pyarrow", "numpy", "httpx")


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
    # Control-plane bounds (parent <-> sandbox channel). These live here rather than on
    # SandboxConfig because they bound the orchestration channel, not a backend's behavior.
    max_emit_bytes: int = 1024 * 1024          # emit payload cap, checked before parsing
    max_control_bytes: int = 8 * 1024 * 1024   # new-handles file read cap
    max_new_handles: int = 256                 # records adopted per run
    root_dir: Path | None = None  # None -> a session dir is created under ./.tether/sessions/
    cleanup: bool = False  # delete the root on async-context exit (throwaway runs)
    idle_ttl_s: float | None = None  # continuous-session idle TTL (None = never expire)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    documents: DocumentConfig = field(default_factory=DocumentConfig)
