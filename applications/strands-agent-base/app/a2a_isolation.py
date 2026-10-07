"""Per-caller A2A contexts.

StrandsA2AExecutor caches one agent per client-supplied `context_id`, and its own
docstring says that id "is not an authentication boundary". With an agent_factory
the cached agent carries the first caller's memory actor and MCP connections, so a
second caller who sends the same `contextId` would get the first caller's
conversation and long-term memory. Verified live on oap-dev before this fix.

The fix scopes the executor's context map itself: every lookup by context id
(building an agent, reusing it, cooperative cancel) resolves to the entry for
(caller, context_id), using the same caller key as the /chat route
(identity.caller_key). The factory still receives the plain context id, so
AgentCore session ids are unchanged; AgentCore already separates sessions by actor.

Tasks get the same treatment. a2a-sdk 0.3.x (strands-agents 1.57 requires <0.4) has
an InMemoryTaskStore keyed by task id only, so tasks/get, tasks/cancel,
tasks/resubscribe and push-config calls would hand one caller another's task.
a2a-sdk main scopes tasks by an owner resolved from the call context; CallerScopedTaskStore
is the same idea for the pinned version, owned by caller_key().

Replaces `_contexts`, a private attribute of strands-agents. The version is pinned
(pyproject.toml) and tests/unit/test_a2a_isolation.py checks the SDK still keeps
its per-context agents there and looks them up by context id.
"""

from collections import OrderedDict

from a2a.server.tasks import InMemoryTaskStore

from .identity import caller_key


def _scoped(context_id: str) -> str:
    return f"{caller_key()}\x00{context_id}"


class CallerScopedContexts(OrderedDict):
    """OrderedDict whose context-id keys are qualified by the current caller.

    Only the operations the SDK performs with a context id are overridden.
    Eviction (popitem) works on the stored keys and needs no translation.
    """

    def get(self, context_id, default=None):
        return super().get(_scoped(context_id), default)

    def __getitem__(self, context_id):
        return super().__getitem__(_scoped(context_id))

    def __setitem__(self, context_id, value):
        super().__setitem__(_scoped(context_id), value)

    def __contains__(self, context_id):
        return super().__contains__(_scoped(context_id))

    def __delitem__(self, context_id):
        super().__delitem__(_scoped(context_id))

    def pop(self, context_id, *default):
        return super().pop(_scoped(context_id), *default)

    def move_to_end(self, context_id, last=True):
        super().move_to_end(_scoped(context_id), last)


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
    """

    @staticmethod
    def _key(task_id: str) -> str:
        return f"{caller_key()}\x00{task_id}"

    async def save(self, task, context=None):
        async with self.lock:
            self.tasks[self._key(task.id)] = task

    async def get(self, task_id, context=None):
        async with self.lock:
            return self.tasks.get(self._key(task_id))

    async def delete(self, task_id, context=None):
        async with self.lock:
            self.tasks.pop(self._key(task_id), None)
