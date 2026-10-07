"""A2A contexts are scoped to the caller, not to the client-supplied context id.

Without this, StrandsA2AExecutor hands the agent built for caller A (A's memory
actor, A's MCP connections) to any caller B who sends A's contextId. Reproduced live
on oap-dev before the fix (matrix row A2).

These tests drive the SDK's own executor methods, so they also fail if a
strands-agents upgrade stops keeping per-context agents in `_contexts`.
"""

import asyncio
import inspect

import pytest
from strands.multiagent.a2a.executor import StrandsA2AExecutor

from app import a2a_isolation
from app.identity import inbound_auth


class _Server:
    def __init__(self, executor):
        self.request_handler = type("H", (), {"agent_executor": executor})()


def _scoped_executor(max_contexts=8):
    built = []

    def factory(context_id):
        agent = type("A", (), {"cancelled": 0, "cancel": lambda self: setattr(self, "cancelled", self.cancelled + 1)})()
        built.append((context_id, agent))
        return agent

    ex = StrandsA2AExecutor(agent_factory=factory, max_contexts=max_contexts)
    a2a_isolation.scope_a2a_contexts_to_caller(_Server(ex))
    return ex, built


def _as(auth, fn):
    token = inbound_auth.set(auth)
    try:
        return fn()
    finally:
        inbound_auth.reset(token)


def _acquire(ex, auth, cid):
    return _as(auth, lambda: asyncio.run(ex._acquire_context_agent(cid))[0])


def test_same_context_id_different_callers_get_different_agents():
    ex, built = _scoped_executor()
    a = _acquire(ex, "Bearer user-one", "shared-ctx")
    b = _acquire(ex, "Bearer user-two", "shared-ctx")
    assert a is not b
    assert [cid for cid, _ in built] == ["shared-ctx", "shared-ctx"]  # factory sees the plain id


def test_same_caller_same_context_reuses_the_agent():
    ex, built = _scoped_executor()
    assert _acquire(ex, "Bearer user-one", "ctx") is _acquire(ex, "Bearer user-one", "ctx")
    assert len(built) == 1


def test_cancel_lookup_finds_only_the_callers_own_agent():
    # StrandsA2AExecutor.cancel resolves the agent with self._contexts.get(context_id).
    ex, _ = _scoped_executor()
    mine = _acquire(ex, "Bearer user-one", "ctx")
    assert _as("Bearer user-one", lambda: ex._contexts.get("ctx")).agent is mine
    assert _as("Bearer user-two", lambda: ex._contexts.get("ctx")) is None


def test_eviction_still_bounds_the_cache():
    ex, _ = _scoped_executor(max_contexts=2)
    for i in range(4):
        _acquire(ex, f"Bearer user-{i}", "ctx")
    assert len(ex._contexts) == 2


def test_refuses_single_agent_mode_and_late_scoping():
    ex = StrandsA2AExecutor(agent_factory=lambda cid: object())
    asyncio.run(ex._acquire_context_agent("already-serving"))
    with pytest.raises(RuntimeError):
        a2a_isolation.scope_a2a_contexts_to_caller(_Server(ex))


def test_sdk_still_keys_agents_by_context_id_in_contexts():
    # Guard on the private attribute this module replaces.
    acquire = inspect.getsource(StrandsA2AExecutor._acquire_context_agent)
    cancel = inspect.getsource(StrandsA2AExecutor.cancel)
    assert "self._contexts.get(context_id)" in acquire
    assert "self._contexts[context_id]" in acquire
    assert "self._contexts.get(context.context_id)" in cancel


def test_task_store_hides_a_task_from_other_callers():
    from a2a.types import Task, TaskState, TaskStatus

    store = a2a_isolation.CallerScopedTaskStore()
    task = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.working))

    _as("Bearer alice", lambda: asyncio.run(store.save(task)))
    assert _as("Bearer alice", lambda: asyncio.run(store.get("t1"))) is task
    assert _as("Bearer bob", lambda: asyncio.run(store.get("t1"))) is None

    _as("Bearer bob", lambda: asyncio.run(store.delete("t1")))
    assert _as("Bearer alice", lambda: asyncio.run(store.get("t1"))) is task
    _as("Bearer alice", lambda: asyncio.run(store.delete("t1")))
    assert _as("Bearer alice", lambda: asyncio.run(store.get("t1"))) is None


def test_task_store_is_what_the_request_handler_consults():
    # DefaultRequestHandler must call task_store.get before every tasks/* operation,
    # which is what makes scoping the store sufficient.
    from a2a.server.request_handlers import default_request_handler as h

    src = inspect.getsource(h.DefaultRequestHandler)
    assert src.count("await self.task_store.get(") >= 5
