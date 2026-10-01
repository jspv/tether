"""A Session bundles one run's root directory, handle store, and sandbox."""

from __future__ import annotations

import mimetypes
import os
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import bundles as _bundles
from .config import TetherConfig, SandboxConfig
from .handles import _PREVIEW_CHARS, Handle, HandleStore
from .paths import PathEscapesRootError, safe_filename, safe_path, validate_segment
from .publish import OnPublish, PublishError, validate_publication
from .sandbox import LocalSubprocessSandbox, SandboxExecutor, SandboxRuntimeUnavailable
from .status import StatusBus, StatusEvent, bind_bus

_DELIVER = "deliver"
_INPUTS = "inputs"
_TEXT_PREVIEW_EXTS = {".csv", ".tsv", ".txt", ".json", ".md"}
_PREVIEW_LINES = 5
_PREVIEW_READ_BYTES = 4096


@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: SandboxExecutor
    config: TetherConfig
    on_publish: OnPublish | None = None
    _mcp_connected: list[Any] = field(default_factory=list, init=False, repr=False)
    status_bus: StatusBus = field(default_factory=StatusBus, init=False, repr=False)
    _status_cm: Any = field(default=None, init=False, repr=False)
    # Serializes parent-side file work in the root with sandbox runs (shared with the sandbox).
    io_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

    @classmethod
    def create(cls, config: TetherConfig, *, on_publish: OnPublish | None = None) -> "Session":
        _check_isolation(config.sandbox)   # before touching the filesystem
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = _build_sandbox(root, store, config.sandbox)
        session = cls(root=root, store=store, sandbox=sandbox, config=config,
                      on_publish=on_publish)
        if hasattr(sandbox, "lock"):
            session.io_lock = sandbox.lock        # one lock for sandbox runs + parent file work
        if on_publish is not None and hasattr(sandbox, "publisher"):
            sandbox.publisher = session.publish   # enables publish() inside run_python
        return session

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

    def _unavailable_bundles(self) -> tuple[str, ...]:
        """Bundles that are selected-but-inert: ``deliver`` needs a host publish callback."""
        return () if self.on_publish is not None else (_DELIVER,)

    def tools(self, *bundles: str) -> list:
        """The built-in tool callables for the selected bundles (default: all)."""
        from .tools.registry import build_tools  # local import avoids circular dependency
        wanted = _bundles.tool_names_for(bundles, exclude=self._unavailable_bundles())
        return [t for t in build_tools(self) if t.__name__ in wanted]

    def tether_instructions(self, *bundles: str) -> str:
        """The operating-manual text (core + selected bundles)."""
        return _bundles.instructions_for(bundles, exclude=self._unavailable_bundles())

    def publish(self, path: str, *, name: str | None = None, description: str | None = None,
                source: str) -> dict:
        """Validate a workspace file and deliver it to the host's ``on_publish`` callback.

        Runs synchronously in the calling thread, so host contextvars set before
        ``agent.run`` are visible in the callback (tools run via ``asyncio.to_thread``, which
        copies the context; the callback must therefore be thread-safe). The callback gets a
        private snapshot outside the workspace (``PublishedFile.path``), copied from the
        validated bytes and deleted when the callback returns. Holds the sandbox lock, so no
        sandboxed code runs meanwhile. Never raises for a bad request or a failing callback:
        the result carries ``error`` so the agent loop can continue.
        """
        if self.on_publish is None:
            return {"error": "publishing is not enabled for this session", "path": path}
        with self.io_lock, tempfile.TemporaryDirectory(prefix="tether-publish-") as snap:
            try:
                pf = validate_publication(self.root, path, name=name, description=description,
                                          source=source, max_bytes=self.config.max_publish_bytes,
                                          snapshot_dir=Path(snap))
            except PublishError as e:
                return {"error": str(e), "path": path}
            record = {"name": pf.name, "rel_path": pf.rel_path, "size": pf.size,
                      "sha256": pf.sha256, "content_type": pf.content_type}
            try:
                host = self.on_publish(pf)
            except Exception as e:  # noqa: BLE001 - a host failure is reported, not fatal
                return {**record,
                        "error": f"host rejected the publication: {type(e).__name__}: {e}"}
        self.status_bus.emit(StatusEvent(tool="publish_file",
                                         message=f"published {pf.name} ({pf.size} bytes)"))
        return {**record, "host": host}

    @property
    def inputs(self) -> dict[str, Handle]:
        """User-provided inputs, keyed by host-supplied ``input_id``."""
        return self.store.inputs()

    def add_input(self, source: Path | bytes, *, input_id: str, name: str,
                  content_type: str | None = None, description: str | None = None) -> Handle:
        """Place a user upload at ``inputs/<input_id>/<name>`` and register it as a handle.

        The upload is untrusted and is **never parsed here**: it becomes a ``binary`` handle
        whose preview is built from raw bytes only. Idempotent by ``input_id`` (also across
        restarts, via the manifest). The file is made read-only (``0444``); the container tier
        additionally mounts ``inputs/`` read-only. Not safe to call while a turn is running —
        use ``Conversation.add_input`` / ``aadd_input``, which enforce that. Holds the sandbox
        lock, so no sandboxed code can swap paths under ``inputs/`` while it writes.
        """
        with self.io_lock:
            return self._add_input(source, input_id=input_id, name=name,
                                   content_type=content_type, description=description)

    def _add_input(self, source: Path | bytes, *, input_id: str, name: str,
                   content_type: str | None, description: str | None) -> Handle:
        validate_segment(input_id, what="input id")
        existing = self.store.inputs().get(input_id)
        if existing is not None:
            return existing
        filename = safe_filename(name, fallback="upload.bin")
        size = _input_size(source)
        if size > self.config.max_input_bytes:
            raise ValueError(f"input too large ({size} bytes > {self.config.max_input_bytes})")

        target_dir = self._input_dir(input_id)
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".upload_")
        try:
            with os.fdopen(fd, "wb") as out:
                if isinstance(source, (bytes, bytearray)):
                    out.write(source)
                else:
                    with open(source, "rb") as src:
                        shutil.copyfileobj(src, out)
            os.chmod(tmp, 0o444)
            target = target_dir / filename
            os.replace(tmp, target)   # replaces (never writes through) a planted symlink
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        ctype = content_type or mimetypes.guess_type(filename)[0]
        return self.store.put_input(
            path=f"{_INPUTS}/{input_id}/{filename}", size=size,
            preview=_input_preview(target, filename, size, ctype),
            source=f"upload:{filename}", input_id=input_id, content_type=ctype,
            description=description)

    def _input_dir(self, input_id: str) -> Path:
        """Create and return ``<root>/inputs/<input_id>``, refusing any planted symlink."""
        for rel in (_INPUTS, f"{_INPUTS}/{input_id}"):
            expected = self.root / rel
            try:
                resolved = safe_path(self.root, rel)
            except PathEscapesRootError as e:
                raise ValueError(f"{rel}/ resolves outside the workspace") from e
            if resolved != expected:
                raise ValueError(f"{rel}/ is a link; refusing to write through it")
            expected.mkdir(exist_ok=True)
        return self.root / _INPUTS / input_id

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


def _input_size(source: Path | bytes) -> int:
    if isinstance(source, (bytes, bytearray)):
        return len(source)
    st = os.stat(source)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"input source is not a regular file: {source}")
    return st.st_size


def _input_preview(path: Path, filename: str, size: int, content_type: str | None) -> str:
    """Name/size/type, plus the first lines for text-like files. Raw bytes only, no parsing."""
    head = f"<uploaded file {filename}, {size} bytes, {content_type or 'unknown type'}>"
    if Path(filename).suffix.lower() not in _TEXT_PREVIEW_EXTS:
        return head
    with open(path, "rb") as f:
        raw = f.read(_PREVIEW_READ_BYTES)
    lines = raw.decode("utf-8", errors="replace").splitlines()[:_PREVIEW_LINES]
    return (head + "\n" + "\n".join(lines))[:_PREVIEW_CHARS]


def _check_isolation(sandbox_config: SandboxConfig) -> None:
    """Refuse a backend that cannot provide the isolation the config requires."""
    if sandbox_config.require_isolation and sandbox_config.backend != "container":
        raise SandboxRuntimeUnavailable(
            f"SandboxConfig.require_isolation=True but backend={sandbox_config.backend!r}: the "
            'local tier is not a security boundary. Set backend="container" (podman or docker), '
            "or drop require_isolation for development/trusted use only."
        )


def _build_sandbox(root: Path, store: HandleStore,
                   sandbox_config: SandboxConfig) -> SandboxExecutor:
    """Pick the sandbox backend from config (default: local).

    A missing container runtime is a hard error, never a fallback to the local tier.
    """
    _check_isolation(sandbox_config)
    if sandbox_config.backend == "container":
        from .sandbox_container import ContainerSandbox  # local import: optional backend
        try:
            # ContainerSandbox.__init__ calls detect_runtime itself and raises RuntimeError
            # when no runtime exists, so wrap construction rather than detecting twice.
            return ContainerSandbox(root=root, store=store, config=sandbox_config)
        except RuntimeError as e:
            raise SandboxRuntimeUnavailable(
                f"{e}\nInstall podman or docker, or (without require_isolation) set "
                'TetherConfig.sandbox.backend = "local" to run code with no isolation.'
            ) from e
    return LocalSubprocessSandbox(root=root, store=store, config=sandbox_config)
