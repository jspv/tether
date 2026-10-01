"""Public entry point: Tether / solve() returning a Result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

from .config import TetherConfig
from .status import StatusEvent

if TYPE_CHECKING:
    from .conversation import Conversation

_StatusSink = Callable[[StatusEvent], None]


@dataclass
class Result:
    final_text: str
    handles: dict[str, Any]
    files: list[str]
    session_dir: Path
    error: str | None = None


@dataclass(frozen=True)
class ToolFactory:
    """A deferred tool: ``build()`` is called once per conversation.

    Marked explicitly rather than detected, because a plain python tool is itself a
    callable -- sniffing would silently misclassify a user's zero-argument tool as a
    factory and call it at setup time.
    """
    build: Callable[[], Any]


def tool_factory(build: Callable[[], Any]) -> ToolFactory:
    """Wrap a zero-argument callable so each conversation gets its own tool instance.

    Use this for anything stateful or connected -- an MCP server above all. A single live
    MCPTool shared across conversations is owned and closed by whichever session finishes
    first, which is both a lifecycle bug and a cross-conversation leak.
    """
    if not callable(build):
        raise TypeError(f"tool_factory expects a zero-argument callable, got {type(build)!r}")
    return ToolFactory(build)


def expand_tools(tools: list | None) -> list:
    """Resolve ToolFactory entries into fresh instances; pass everything else through."""
    return [t.build() if isinstance(t, ToolFactory) else t for t in (tools or [])]


class Tether:
    """Reusable tether: builds a Session per run via the composable create_agent path."""

    def __init__(self, config: TetherConfig | None = None, client: Any | None = None,
                 *, tools: list | None = None,
                 bundles: tuple[str, ...] = ("code", "files", "web"),
                 on_status: _StatusSink | None = None) -> None:
        self.config = config or TetherConfig()
        self._client = client
        self._tools = tools or []
        self._bundles = bundles
        self._on_status = on_status
        self._manager = None
        if self.config.search.api_key is None:
            import os

            from dotenv import load_dotenv
            load_dotenv()
            self.config.search.api_key = os.environ.get("TAVILY_API_KEY")

    def _make_client(self):
        if self._client is not None:
            return self._client
        from agent_framework.openai import OpenAIChatClient
        return OpenAIChatClient(model=self.config.model, env_file_path=".env")

    def _sessions(self):
        if self._manager is None:
            from .manager import SessionManager
            self._manager = SessionManager(self, idle_ttl_s=self.config.idle_ttl_s)
        return self._manager

    async def aopen(self, session_id: str | None = None, *,
                    tools: list | None = None) -> Conversation:
        """Open (or resume) a persistent continuous Conversation by id.

        Lazy manager init is single-event-loop safe (the None-check/assign has no ``await``); it is
        not safe to first-touch ``aopen`` from multiple OS threads — drive continuous sessions from
        one loop, the v1 contract for both AG-UI and the async terminal loop.
        """
        return await self._sessions().aopen(session_id, tools=tools)

    async def aclose_sessions(self) -> None:
        """Close every live continuous Conversation (host shutdown)."""
        if self._manager is not None:
            await self._manager.aclose()

    async def sweep_sessions(self) -> None:
        """Reap idle continuous Conversations past their TTL."""
        if self._manager is not None:
            await self._manager.sweep()

    async def asolve(self, problem: str, tools: list | None = None, *,
                     on_status: _StatusSink | None = None, keep: bool = False) -> Result:
        """Run one ephemeral one-shot: open a Conversation, ask, reap the workspace (unless ``keep``).

        The one-shot uses ``config.root_dir`` verbatim. With ``root_dir=None`` each call gets its own
        auto-allocated dir, so parallel ``solve``/``asolve`` calls are isolated. With a **pinned**
        ``root_dir`` they share that dir — and since the default reaps it on completion, concurrent
        one-shots on the same pinned root are unsafe (the first to finish deletes it mid-run). For
        concurrent one-shots either leave ``root_dir`` unset or use a separate Tether per call.
        """
        from .conversation import Conversation

        sink = on_status if on_status is not None else self._on_status
        conv = await Conversation.acreate(
            id="oneshot", config=self.config, client=self._make_client(),
            tools=expand_tools(self._tools) + expand_tools(tools),
            bundles=self._bundles, reap_on_close=not keep)
        if sink is not None:
            conv.session.subscribe(sink)
        try:
            return await conv.aask(problem)
        finally:
            await conv.aclose()

    async def agui_stream(self, input_data: dict, *, tools: list | None = None,
                          **agui_kwargs: Any) -> AsyncIterator[Any]:
        """Yield AG-UI events for one request, reusing a persistent workspace per ``threadId``.

        The workspace (handles, sandbox files) persists across turns; conversation history stays
        with the official AG-UI wrapper (CopilotKit replays ``messages``). The thread's workspace
        is reaped only by ``close``/TTL, never at end-of-request. Requires the ``agui`` extra.
        """
        from .agui import agui_event_stream

        thread_id = input_data.get("threadId") or input_data.get("thread_id")
        if not thread_id:
            # The threadId IS the persistence key; without it aopen(None) would mint a fresh,
            # unreachable Conversation per request (an unbounded leak). Fail loud instead.
            raise ValueError("agui_stream requires input_data['threadId']")
        # No single-flight lock here: AG-UI hosts (e.g. CopilotKit) serialize runs per thread, so
        # one threadId has at most one in-flight request. A non-serializing host would need the lock.
        conv = await self._sessions().aopen(thread_id, tools=tools)
        async for event in agui_event_stream(conv.agent, conv.session.status_bus, input_data,
                                             **agui_kwargs):
            yield event

    def solve(self, problem: str, tools: list | None = None, *,
              on_status: _StatusSink | None = None, keep: bool = False) -> Result:
        return asyncio.run(self.asolve(problem, tools=tools, on_status=on_status, keep=keep))


def solve(problem: str, *, tools: list | None = None,
          config: TetherConfig | None = None, client: Any | None = None,
          on_status: _StatusSink | None = None, keep: bool = False) -> Result:
    """One-shot convenience: build a Tether and solve a single problem (ephemeral unless keep)."""
    return Tether(config, client=client, tools=tools, on_status=on_status).solve(problem, keep=keep)
