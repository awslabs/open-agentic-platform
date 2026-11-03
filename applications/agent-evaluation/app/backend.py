"""The evaluation backend boundary: everything vendor-specific sits behind this.

WHY THIS EXISTS. Customers bring their own observability stack — Langfuse,
AgentCore, Datadog, Arize — and the platform has to be able to score agents on
whichever one a cluster runs. Before this boundary, Langfuse concepts (rule
grouping, owner tokens, variable mapping, metrics query payloads) lived in the
same functions as the generic work (reading declarations, resolving the catalog,
turning strictness into a threshold, publishing gauges), so a second backend
would have meant a branch at every one of those points. With the boundary, a
second backend is one new module implementing three methods.

WHAT IS GENERIC, AND STAYS OUT OF HERE:

  - reading `evaluation.oap.io/agent` ConfigMaps          (declarations.py)
  - resolving names and strictness against the catalog    (config.py)
  - deciding thresholds, sampling and rule grouping KEYS  (cluster_loop.py)
  - publishing `oap_agent_eval_*` gauges                  (metrics.py)
  - Kubernetes Events on an agent's own ConfigMap         (events.py)

WHAT IS BACKEND-SPECIFIC, AND LIVES BEHIND HERE:

  - how a judge prompt is stored and versioned
  - how a judge model is attached, and how it is named
  - how "score these traces with this evaluator" is expressed
  - how scores are read back and aggregated
  - what an object is called, and who is allowed to write it

THE PUBLIC CONTRACT. Two things are contracts rather than implementation
details, because a customer plugging in their own backend has to keep the
dashboard and alerts working:

  1. the declaration ConfigMap schema  (docs/architecture/agent-evaluation.md,
     "Declaration ConfigMap schema")
  2. the `oap_agent_eval_*` metric names and labels  (same doc, "Metrics
     contract")

A backend may name its own objects however it likes; it must not change either
of those.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class BackendError(Exception):
    """The backend could not complete an operation.

    Carries `retryable` so a loop can tell "try again next pass" (throttled,
    transient) from "this will not fix itself" (rejected payload), without
    knowing which backend it is talking to or what its status codes mean.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BackendNotSupported(BackendError):
    """This instance is not permitted to perform this operation.

    Raised by `publish_catalog` on a non-owning instance. A program error rather
    than a runtime condition: the caller should not have asked.
    """


class BackendSchemaError(BackendError):
    """The backend's response did not match the shape this code requires.

    Separate from a transport failure on purpose. A renamed response key must
    never be absorbed into a zero, because a zero here is indistinguishable from
    a real quality collapse on the dashboard.
    """


# ── what the generic layer hands down ──────────────────────────────────────


@dataclass(frozen=True)
class EvaluatorRequest:
    """One (agent, evaluator) pairing, already resolved against the catalog.

    Everything here is backend-neutral: a prompt, a judge model id, a sampling
    rate and a threshold. How any of it is represented in the backend is the
    implementation's business.
    """

    # Catalog name, e.g. `helpfulness`. Stable across judges, and the label the
    # metrics contract uses.
    evaluator: str
    # Judge ALIAS as the developer wrote it, e.g. `judge-default`.
    judge: str
    # The gateway model id that alias resolves to, e.g. `us.amazon.nova-pro-v1:0`.
    judge_model: str
    prompt: str
    score_type: str
    reasoning_prompt: str
    output_prompt: str
    # Prompt variable -> observation field.
    mapping: dict[str, str]
    # Fraction of traces to score, in (0, 1].
    sampling: float
    # Minimum score that counts as a pass. Applied when the pass ratio is
    # computed, NOT by the backend, so two agents sharing one backend rule can
    # still be held to different bars.
    threshold: float


@dataclass(frozen=True)
class AgentTarget:
    """Which agent's traces an evaluator applies to."""

    namespace: str
    agent: str
    # The cluster-scoped trace tag the agent stamps on its own traces. The one
    # piece of coupling that is deliberately shared: the agent image emits it and
    # the backend selects on it.
    trace_tag: str

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.agent}"


@dataclass(frozen=True)
class AgentEvaluationSpec:
    """"Score this agent with these evaluators." One per declared agent."""

    target: AgentTarget
    requests: tuple[EvaluatorRequest, ...]


# ── what the backend hands back ────────────────────────────────────────────


@dataclass
class ReconcileOutcome:
    """The result of one `reconcile_agents` pass."""

    # How many backend-side rules this cluster now owns, for the budget metric.
    rules_active: int = 0
    # Human-readable description of each change made, for logging. Empty on a
    # no-op pass, which is the steady state of a level-triggered reconciler.
    changes: list[str] = field(default_factory=list)
    # agent key -> why that agent could not be fully reconciled. Surfaced by the
    # caller as a Kubernetes Event on the agent's own ConfigMap, so the backend
    # never needs to know what an Event is.
    unsatisfied: dict[str, str] = field(default_factory=dict)
    # True when the backend refused further rules because a capacity limit was
    # reached, so the caller can report it distinctly from a generic failure.
    at_capacity: bool = False


@dataclass(frozen=True)
class ScoreReading:
    """Aggregated scores for one (agent, evaluator) over one window."""

    target_key: str
    # Catalog name, so the caller can label the gauge without un-mapping.
    evaluator: str
    scored: int
    passed: int
    average: float | None


# ── the boundary itself ────────────────────────────────────────────────────


class EvaluationBackend(ABC):
    """Three operations. Everything vendor-specific is behind them."""

    #: Short name for logs and the `backend` label on diagnostics.
    name: str = "unknown"

    @abstractmethod
    def publish_catalog(self, requests: list[EvaluatorRequest]) -> list[str]:
        """Make every distinct (evaluator, judge) available for use, idempotently.

        Called ONLY on the instance that owns the project-wide objects (the hub),
        because every cluster shares one backend project and the last writer
        would otherwise decide how the whole fleet is scored. Implementations must
        raise `BackendNotSupported` if asked on a non-owning instance, so that
        guarantee is a property of the program rather than a convention.

        Returns a description of each change made, empty when already correct.
        """

    @abstractmethod
    def reconcile_agents(self, specs: list[AgentEvaluationSpec]) -> ReconcileOutcome:
        """Make the backend score exactly these agents, and no others this cluster owns.

        Level-triggered and idempotent: the argument is the complete desired
        state for THIS cluster. An agent that disappears from it must have its
        backend-side configuration removed — which is what makes deletion work
        with no finalizer. An implementation must never touch another cluster's
        objects, and owning-cluster identification is its own concern.
        """

    @abstractmethod
    def read_scores(
        self,
        specs: list[AgentEvaluationSpec],
        start: datetime,
        end: datetime,
    ) -> list[ScoreReading]:
        """Return score aggregates for these agents over one window.

        Must apply each request's own `threshold` when counting `passed`, since
        the threshold comes from the agent's strictness and the backend has no
        concept of it.

        An agent with no scores in the window must come back with `scored=0`
        rather than be omitted, so the caller can tell "idle" from "not asked
        about". Raise `BackendSchemaError` rather than returning zeroes if the
        response shape is not understood.
        """
