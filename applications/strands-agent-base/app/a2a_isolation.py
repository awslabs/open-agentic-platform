"""Per-caller A2A contexts.

StrandsA2AExecutor caches one agent per client-supplied `context_id`, and its own
docstring says that id "is not an authentication boundary". With an agent_factory
the cached agent carries the first caller's memory actor and MCP connections, so a
second caller who sends the same `contextId` would get the first caller's
conversation and long-term memory. Verified live on oap-dev before this fix.

This executor keys the cache on (caller, context_id) instead, using the same caller
key as the /chat route (identity.caller_key). The factory still receives the plain
context id, so AgentCore session ids are unchanged; AgentCore already separates
sessions by actor.

Overrides `_acquire_context_agent`, a private method of strands-agents. It is
pinned (pyproject.toml) and covered by tests/unit/test_a2a_isolation.py, which fails
if the SDK stops calling this method or changes the cache it uses.
"""

import asyncio

from strands.multiagent.a2a.executor import StrandsA2AExecutor, _ContextEntry

from .identity import caller_key


class CallerScopedA2AExecutor(StrandsA2AExecutor):
    async def _acquire_context_agent(self, context_id: str):
        key = f"{caller_key()}\x00{context_id}"
        async with self._contexts_lock:
            entry = self._contexts.get(key)
            if entry is None:
                entry = _ContextEntry(agent=self._agent_factory(context_id), lock=asyncio.Lock())
                self._contexts[key] = entry
                self._evict_excess_contexts()
            else:
                self._contexts.move_to_end(key)
            return entry.agent, entry.lock


def scope_a2a_contexts_to_caller(a2a_server) -> None:
    """Replace the A2A server's executor with the caller-scoped one, same settings."""
    handler = a2a_server.request_handler
    current = handler.agent_executor
    handler.agent_executor = CallerScopedA2AExecutor(
        agent_factory=current._agent_factory,
        enable_a2a_compliant_streaming=current.enable_a2a_compliant_streaming,
        max_contexts=current._max_contexts,
    )
