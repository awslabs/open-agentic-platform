"""A2A contexts are scoped to the caller, not to the client-supplied context id.

Without this, StrandsA2AExecutor hands the agent built for caller A (A's memory
actor, A's MCP connections) to any caller B who sends A's contextId. Reproduced live
on oap-dev before the fix (matrix row A2).
"""

import asyncio
import inspect

from strands.multiagent.a2a.executor import StrandsA2AExecutor

from app import a2a_isolation
from app.identity import inbound_auth


def _executor():
    built = []

    def factory(context_id):
        agent = object()
        built.append((context_id, agent))
        return agent

    return a2a_isolation.CallerScopedA2AExecutor(agent_factory=factory), built


def _acquire(executor, auth, context_id):
    token = inbound_auth.set(auth)
    try:
        return asyncio.run(executor._acquire_context_agent(context_id))[0]
    finally:
        inbound_auth.reset(token)


def test_same_context_id_different_callers_get_different_agents():
    ex, built = _executor()
    a = _acquire(ex, "Bearer user-one", "shared-ctx")
    b = _acquire(ex, "Bearer user-two", "shared-ctx")
    assert a is not b
    assert [cid for cid, _ in built] == ["shared-ctx", "shared-ctx"]  # factory sees the plain id


def test_same_caller_same_context_reuses_the_agent():
    ex, built = _executor()
    assert _acquire(ex, "Bearer user-one", "ctx") is _acquire(ex, "Bearer user-one", "ctx")
    assert len(built) == 1


def test_scope_replaces_the_executor_and_keeps_settings():
    base = StrandsA2AExecutor(agent_factory=lambda cid: object(), enable_a2a_compliant_streaming=True, max_contexts=7)

    class _Handler:
        agent_executor = base

    class _Server:
        request_handler = _Handler()

    server = _Server()
    a2a_isolation.scope_a2a_contexts_to_caller(server)
    new = server.request_handler.agent_executor
    assert isinstance(new, a2a_isolation.CallerScopedA2AExecutor)
    assert new.enable_a2a_compliant_streaming is True and new._max_contexts == 7
    assert new._agent_factory is base._agent_factory


def test_sdk_still_routes_requests_through_the_overridden_method():
    # Guards the private-API override: fails if a strands-agents upgrade stops
    # calling _acquire_context_agent, which would silently disable the isolation.
    assert "self._acquire_context_agent(" in inspect.getsource(StrandsA2AExecutor._run_with_context_agent)
