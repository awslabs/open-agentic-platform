"""Bounded tool executor.

strands-agents' default ``ConcurrentToolExecutor`` runs every tool call of one
model turn at once with no limit. A fleet-wide question can make the model emit
a dozen reads in one turn; against an MCP server that spawns one process per
request (supergateway 2026-07-28 mode) this OOMs the server. This executor keeps
the concurrent behaviour but caps how many tool calls run at the same time.

Cap: ``TOOL_MAX_CONCURRENCY`` (default 4). ``1`` is fully sequential.
"""

import asyncio
from typing import Any

from strands.tools.executors import ConcurrentToolExecutor


class BoundedToolExecutor(ConcurrentToolExecutor):
    """ConcurrentToolExecutor with at most ``limit`` tool calls in flight."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit
        self._loop = None
        self._sem = None

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily inside the running loop; rebuilt if the loop changes.
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._loop, self._sem = loop, asyncio.Semaphore(self.limit)
        return self._sem

    # ``_task`` is a private strands method. It is stable with the pinned
    # strands-agents version in pyproject.toml; on a version bump,
    # tests/unit/test_tool_executor.py::test_parallelism_never_exceeds_cap fails
    # if it is renamed or no longer wraps each tool call.
    async def _task(self, *args: Any, **kwargs: Any) -> None:
        async with self._semaphore():
            await super()._task(*args, **kwargs)
