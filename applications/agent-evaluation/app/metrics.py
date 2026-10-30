"""The metrics this reconciler publishes, exactly as D5 specifies them.

Two rules shape this module.

**Gauges are replaced, not accumulated.** Every value here is a rolling-window
aggregate read back from Langfuse, so a pass publishes a complete picture or none
of it. `publish_scores` clears the score gauges and re-sets them from one pass's
results, which is what makes a deleted agent's series disappear instead of
freezing at its last value.

**An idle agent is absent, not zero.** `oap_agent_eval_pass_ratio` is not
emitted when nothing was scored in the window. A zero there would read as "every
response failed" on the dashboard, which is the opposite of "no traffic yet".
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

SCORE_LABELS = ("cluster", "namespace", "agent", "evaluator", "window")
THRESHOLD_LABELS = ("cluster", "namespace", "agent", "evaluator")
INFO_LABELS = (
    "cluster",
    "namespace",
    "agent",
    "gen_ai_request_model",
    "eval_ai_request_model",
    "strictness",
    "coverage",
)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()
        r = self.registry

        self.scores = Gauge(
            "oap_agent_eval_scores",
            "Scored traces for this agent and evaluator in the window.",
            SCORE_LABELS,
            registry=r,
        )
        self.passed = Gauge(
            "oap_agent_eval_passed",
            "Scores at or above the resolved pass threshold in the window.",
            SCORE_LABELS,
            registry=r,
        )
        self.pass_ratio = Gauge(
            "oap_agent_eval_pass_ratio",
            "passed / scores. Absent when nothing was scored, so an idle agent "
            "is not reported as 0%.",
            SCORE_LABELS,
            registry=r,
        )
        self.score_avg = Gauge(
            "oap_agent_eval_score_avg",
            "Mean score in the window.",
            SCORE_LABELS,
            registry=r,
        )
        self.pass_threshold = Gauge(
            "oap_agent_eval_pass_threshold",
            "Resolved pass threshold. No window label on purpose: the threshold "
            "comes from strictness, not from a window.",
            THRESHOLD_LABELS,
            registry=r,
        )
        self.info = Gauge(
            "oap_agent_eval_info",
            "Always 1. Carries the agent and judge models and the agent's levels, "
            "joined in PromQL so they do not multiply every other series.",
            INFO_LABELS,
            registry=r,
        )
        self.rules_active = Gauge(
            "oap_agent_eval_rules_active",
            "Langfuse evaluation rules this cluster owns. The cap is per project, "
            "so compare sum() across clusters against it.",
            ("cluster",),
            registry=r,
        )
        self.errors = Counter(
            "oap_agent_eval_errors_total",
            "Failed Langfuse calls, by loop.",
            ("cluster", "loop"),
            registry=r,
        )
        self.last_success = Gauge(
            "oap_agent_eval_last_success_timestamp_seconds",
            "Unix time of the last fully successful pass of each loop.",
            ("cluster", "loop"),
            registry=r,
        )
        # Not in D5's table: operational facts that turned out to be needed to
        # tell "working" from "silently degraded" without reading the logs.
        self.cluster_info = Gauge(
            "oap_agent_eval_cluster_info",
            "Always 1. Maps a cluster to the owner token used in its Langfuse "
            "object names, so an operator can read the Langfuse UI.",
            ("cluster", "owner_token", "role"),
            registry=r,
        )
        self.judge_key_present = Gauge(
            "oap_agent_eval_judge_key_present",
            "1 when the judge Bifrost virtual key is mounted, 0 when it is "
            "absent and judge calls therefore run un-budgeted.",
            ("cluster",),
            registry=r,
        )
        self.agents_declared = Gauge(
            "oap_agent_eval_agents_declared",
            "Agents on this cluster with evaluation turned on.",
            ("cluster",),
            registry=r,
        )
        self.agents_skipped = Gauge(
            "oap_agent_eval_agents_skipped",
            "Agents whose declaration could not be fully reconciled this pass "
            "(unknown evaluator, unknown judge, invalid declaration).",
            ("cluster",),
            registry=r,
        )

    # ── publishing ────────────────────────────────────────────────────────

    def publish_scores(self, rows: list["ScoreRow"]) -> None:
        """Replace every score series with one pass's results."""
        for gauge in (self.scores, self.passed, self.pass_ratio, self.score_avg):
            gauge.clear()
        for row in rows:
            labels = (row.cluster, row.namespace, row.agent, row.evaluator, row.window)
            self.scores.labels(*labels).set(row.scored)
            self.passed.labels(*labels).set(row.passed)
            if row.scored > 0:
                self.pass_ratio.labels(*labels).set(row.passed / row.scored)
                if row.average is not None:
                    self.score_avg.labels(*labels).set(row.average)

    def publish_thresholds(self, rows: list["ThresholdRow"]) -> None:
        self.pass_threshold.clear()
        for row in rows:
            self.pass_threshold.labels(
                row.cluster, row.namespace, row.agent, row.evaluator
            ).set(row.threshold)

    def publish_info(self, rows: list["InfoRow"]) -> None:
        self.info.clear()
        for row in rows:
            self.info.labels(
                row.cluster,
                row.namespace,
                row.agent,
                row.agent_model,
                row.judge_model,
                row.strictness,
                row.coverage,
            ).set(1)


# Row types kept as plain dataclasses so the loops can build a full pass before
# anything is published — a partially-written gauge set is worse than stale data.
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class ScoreRow:
    cluster: str
    namespace: str
    agent: str
    evaluator: str
    window: str
    scored: int
    passed: int
    average: float | None


@dataclass(frozen=True)
class ThresholdRow:
    cluster: str
    namespace: str
    agent: str
    evaluator: str
    threshold: float


@dataclass(frozen=True)
class InfoRow:
    cluster: str
    namespace: str
    agent: str
    agent_model: str
    judge_model: str
    strictness: str
    coverage: str
