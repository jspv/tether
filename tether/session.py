"""A Session bundles one run's root directory, handle store, and sandbox."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import bundles as _bundles
from .config import TetherConfig, SandboxConfig
from .handles import HandleStore
from .sandbox import (
    ControlPlaneLimits, LocalSubprocessSandbox, SandboxExecutor, SandboxRuntimeUnavailable,
)
from .status import StatusBus, StatusEvent, bind_bus


@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: SandboxExecutor
    config: TetherConfig
    _mcp_connected: list[Any] = field(default_factory=list, init=False, repr=False)
    status_bus: StatusBus = field(default_factory=StatusBus, init=False, repr=False)
    _status_cm: Any = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, config: TetherConfig) -> "Session":
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        limits = ControlPlaneLimits(max_emit_bytes=config.max_emit_bytes,
                                    max_control_bytes=config.max_control_bytes,
                                    max_new_handles=config.max_new_handles)
        sandbox = _build_sandbox(root, store, config.sandbox, limits)
        return cls(root=root, store=store, sandbox=sandbox, config=config)

    @property
    def handles(self) -> dict[str, Any]:
        """Handle summaries produced during the run, by id."""
        return self.store.manifest()

    @property
    def artifacts(self) -> list[str]:
        """User-meaningful files under root, excluding handle storage and scratch."""
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            top = rel.parts[0]
            if top in ("handles", ".scripts") or top.startswith("_"):
                continue
            out.append(rel.as_posix())
        return out

    def subscribe(self, callback: Callable[[StatusEvent], None]) -> Callable[[], None]:
        """Register a status subscriber; returns a zero-arg unsubscribe handle."""
        return self.status_bus.subscribe(callback)

    async def aclose(self) -> None:
        """Close every connected MCP server, then honor the cleanup policy."""
        for tool in self._mcp_connected:
            try:
                await tool.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._mcp_connected.clear()
        if self.config.cleanup and self.root.exists():
            shutil.rmtree(self.root)

    def tools(self, *bundles: str) -> list:
        """The built-in tool callables for the selected bundles (default: all)."""
        from .tools.registry import build_tools  # local import avoids circular dependency
        wanted = _bundles.tool_names_for(bundles)
        return [t for t in build_tools(self) if t.__name__ in wanted]

    def tether_instructions(self, *bundles: str) -> str:
        """The operating-manual text (core + selected bundles)."""
        return _bundles.instructions_for(bundles)

    async def create_agent(
        self,
        client: Any,
        *,
        agent_instructions: str | None = None,
        tools: list | None = None,
        bundles: tuple[str, ...] = ("code", "files", "web"),
        name: str = "data-integrator",
        **maf_kwargs: Any,
    ):
        """Build a MAF agent over the selected bundles plus developer tools/MCP.

        Plain callables are spill-wrapped; MCP servers are connected and their tools get
        the spill parser (Task 5). Operational instructions ride in ``tether_instructions``.
        """
        from agent_framework import create_harness_agent  # local: heavy dep, imported at call time

        from .spill import looks_like_mcp, spill_tool  # local: spill imports Session -> circular at module level

        builtin = self.tools(*bundles)
        external: list = []
        for tool in tools or []:
            if looks_like_mcp(tool):
                external.extend(await self._attach_mcp(tool))
            else:
                external.append(spill_tool(self, tool))

        maf_kwargs.setdefault("max_context_window_tokens", self.config.max_context_window_tokens)
        maf_kwargs.setdefault("max_output_tokens", self.config.max_output_tokens)
        return create_harness_agent(
            client,
            name=name,
            harness_instructions=self.tether_instructions(*bundles),
            agent_instructions=agent_instructions,
            tools=builtin + external,
            disable_todo=True,
            disable_mode=True,
            disable_memory=True,
            disable_web_search=True,
            **maf_kwargs,
        )

    async def _attach_mcp(self, tool: Any) -> list:
        """Connect an MCP server, attach the spill parser, capture its status, own its lifecycle."""
        from .mcp_status import inject_progress_tokens, install_status_wrappers
        from .spill import make_spill_parser

        server = getattr(tool, "name", None) or repr(tool)
        # Install before connect so the wrappers reach the underlying MCP ClientSession.
        token_map = install_status_wrappers(self.status_bus, tool, server)
        try:
            await tool.connect()
        except Exception as e:  # noqa: BLE001 - add context naming the server, then re-raise
            raise RuntimeError(f"failed to connect MCP server {tool!r}: {e}") from e
        # Register before the parser loop so aclose() still closes this server if the loop raises.
        self._mcp_connected.append(tool)
        inject_progress_tokens(tool, server, token_map)   # after connect: tools are now loaded
        functions = list(tool.functions)
        for ft in functions:
            ft.result_parser = make_spill_parser(self, ft.name)
        return functions

    async def __aenter__(self) -> "Session":
        self._status_cm = bind_bus(self.status_bus)
        self._status_cm.__enter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self.aclose()
        finally:
            if self._status_cm is not None:
                self._status_cm.__exit__(None, None, None)
                self._status_cm = None

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


def _resolve_root(config: TetherConfig) -> Path:
    if config.root_dir is not None:
        return Path(config.root_dir).resolve()
    base = Path.cwd() / ".tether" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    existing = [int(p.name) for p in base.iterdir() if p.name.isdigit()]
    next_id = (max(existing) + 1) if existing else 1
    return (base / str(next_id)).resolve()


def _build_sandbox(root: Path, store: HandleStore, sandbox_config: SandboxConfig,
                   limits: ControlPlaneLimits | None = None) -> SandboxExecutor:
    """Pick the sandbox backend from config (default: container).

    A missing container runtime is a hard error, never a fallback: silently dropping to the
    local tier would hand back a no-isolation sandbox while the caller believes the code is
    contained. The opt-out has to be explicit.
    """
    if sandbox_config.backend == "container":
        from . import container_runtime
        from .sandbox_container import ContainerSandbox  # local import: optional backend
        try:
            # Probe LIVENESS, not mere presence. `detect_runtime` only checks shutil.which,
            # so a machine with podman installed but its VM not started passes detection and
            # then fails much later with an opaque "failed to build sandbox image" error.
            # That is the most common failure mode -- especially on macOS, where the runtime
            # lives in a Linux VM -- and it is exactly the case this message exists to serve.
            container_runtime.require_usable_runtime(sandbox_config.container_runtime)
            return ContainerSandbox(root=root, store=store, config=sandbox_config,
                                    limits=limits)
        except RuntimeError as e:
            raise SandboxRuntimeUnavailable(
                f"{e}\nThe container backend is the default because run_python executes "
                f'model-authored code. Install podman or docker, or set '
                f'TetherConfig.sandbox.backend = "local" to run it with no isolation.'
            ) from e
    return LocalSubprocessSandbox(root=root, store=store, config=sandbox_config, limits=limits)
