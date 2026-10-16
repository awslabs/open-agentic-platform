"""Per-caller A2A contexts.

StrandsA2AExecutor caches one agent per client-supplied `context_id`, and its own
docstring says that id "is not an authentication boundary". With an agent_factory
the cached agent carries the first caller's memory actor and MCP connections, so a
second caller who sends the same `contextId` would get the first caller's
conversation and long-term memory. Verified live on oap-dev before this fix.

The fix scopes the executor's context map itself: every lookup by context id
(building an agent, reusing it, cooperative cancel) resolves to the entry for
(caller, context_id), using the same caller key as the /chat route
(identity.caller_key: the gateway subject, plus the token hash only when the agent
forwards the caller's token). The factory still receives the plain context id, so
AgentCore session ids are unchanged; AgentCore already separates sessions by actor.

Tasks get the same treatment. a2a-sdk 0.3.x (strands-agents 1.57 requires <0.4) has
an InMemoryTaskStore keyed by task id only, so tasks/get, tasks/cancel,
tasks/resubscribe and push-config calls would hand one caller another's task.
a2a-sdk main scopes tasks by an owner resolved from the call context; CallerScopedTaskStore
is the same idea for the pinned version, owned by identity.task_owner(context): the
gateway subject, read from the ServerCallContext that a2a-sdk passes to every store
call, so it also holds for saves made after the HTTP response.

Both stores are in process memory, so a task or context lives on the pod that served
it. With more than one replica, a follow-up request that lands on another pod gets
"not found", the same answer as another caller's id. Run A2A agents that rely on
tasks/* or context reuse with one replica.

Replaces `_contexts`, a private attribute of strands-agents, and subclasses
InMemoryTaskStore using its `tasks` and `lock` attributes. Both versions are pinned
(pyproject.toml), and tests/unit/test_a2a_isolation.py checks that the SDKs still
use exactly the surface overridden here.
"""

import os
from collections import OrderedDict

from a2a.server.tasks import InMemoryTaskStore

from .agent import agent_is_stale
from .config import config
from .identity import caller_key, is_no_identity_key, task_owner


def _scoped(context_id: str) -> str:
    return f"{caller_key(config.PROPAGATE_CALLER_TOKEN)}\x00{context_id}"


def _unsupported(name):
    def method(self, *args, **kwargs):
        raise NotImplementedError(
            f"CallerScopedContexts.{name} is not caller-scoped; add a scoped override "
            "before the SDK starts using it"
        )

    method.__name__ = name
    return method


class CallerScopedContexts(OrderedDict):
    """OrderedDict whose context-id keys are qualified by the current caller.

    Only the operations the SDK performs with a context id are overridden.
    Eviction (popitem) works on the stored keys and needs no translation.

    Two entries are never kept: one for a request with no identity (its key is
    unique to that request, so storing it would only evict callers who have one),
    and one whose agent's MCP pool was closed (agent.agent_is_stale). The executor
    builds a fresh agent for a miss, so both are rebuilt, not lost mid-request.
    """

    def get(self, context_id, default=None):
        key = _scoped(context_id)
        entry = super().get(key)
        if entry is not None and agent_is_stale(getattr(entry, "agent", None)):
            # The next request gets a new agent and a new per-context lock, so a turn
            # still running on the old agent is no longer serialized with it, and a
            # cancel cannot reach it. That agent's MCP clients are already stopped, so
            # its turn fails on its next tool call either way.
            super().__delitem__(key)
            return default
        return default if entry is None else entry

    def __getitem__(self, context_id):
        return super().__getitem__(_scoped(context_id))

    def __setitem__(self, context_id, value):
        key = _scoped(context_id)
        if is_no_identity_key(key):
            return
        super().__setitem__(key, value)

    def __contains__(self, context_id):
        return super().__contains__(_scoped(context_id))

    def __delitem__(self, context_id):
        super().__delitem__(_scoped(context_id))

    def pop(self, context_id, *default):
        return super().pop(_scoped(context_id), *default)

    def move_to_end(self, context_id, last=True):
        super().move_to_end(_scoped(context_id), last)

    # Methods that take or add keys but are not translated above. Raising beats
    # silently storing or reading an unscoped key if a future SDK starts using one.
    setdefault = _unsupported("setdefault")
    update = _unsupported("update")
    __ior__ = _unsupported("__ior__")
    __or__ = _unsupported("__or__")
    __ror__ = _unsupported("__ror__")
    copy = _unsupported("copy")


def scope_a2a_contexts_to_caller(a2a_server) -> None:
    """Make the A2A server's per-context agent cache per caller."""
    executor = a2a_server.request_handler.agent_executor
    if getattr(executor, "_agent_factory", None) is None or not isinstance(getattr(executor, "_contexts", None), dict):
        raise RuntimeError("A2A executor is not in agent_factory mode; per-caller contexts cannot be enforced")
    if executor._contexts:
        raise RuntimeError("A2A executor already holds contexts; scope it before serving requests")
    executor._contexts = CallerScopedContexts()


class CallerScopedTaskStore(InMemoryTaskStore):
    """InMemoryTaskStore whose entries belong to the caller that created them.

    A task id from another caller is indistinguishable from an unknown id. The
    request handler looks a task up here before every tasks/* and push-config
    operation, so scoping the store covers all of them.

    A task saved by a request with no identity is owned by a key unique to that
    request (identity.task_owner), so no later request can read it. Storing it at all
    only keeps save/get consistent within that request; the SDK's TaskManager holds
    the running task itself, so an evicted entry does not break the request. Only the
    newest MAX_UNOWNED_TASKS (default 100) are kept, so requests that bypass the
    gateway cannot grow the store without bound.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._unowned: "OrderedDict[str, None]" = OrderedDict()
        self._max_unowned = int(os.getenv("MAX_UNOWNED_TASKS", "100"))

    @staticmethod
    def _key(task_id: str, context) -> str:
        return f"{task_owner(context)}\x00{task_id}"

    async def save(self, task, context=None):
        key = self._key(task.id, context)
        async with self.lock:
            self.tasks[key] = task
            if is_no_identity_key(key):
                self._unowned[key] = None
                self._unowned.move_to_end(key)
                while len(self._unowned) > self._max_unowned:
                    self.tasks.pop(self._unowned.popitem(last=False)[0], None)

    async def get(self, task_id, context=None):
        async with self.lock:
            return self.tasks.get(self._key(task_id, context))

    async def delete(self, task_id, context=None):
        key = self._key(task_id, context)
        async with self.lock:
            self.tasks.pop(key, None)
            self._unowned.pop(key, None)
