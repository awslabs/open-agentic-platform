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
HARNESS_LABELS = ("cluster", "namespace", "agent", "dataset")
HARNESS_INFO_LABELS = HARNESS_LABELS + (
    "revision",
    "mode",
    "judge_model",
    "trigger",
    "status",
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

        # ── harness (issue #137, report mode) ─────────────────────────────
        # Always registered, absent until a run of that agent has finished: an
        # agent that has never been run must not read as 0% on a dashboard.
        def harness_gauge(name: str, help_text: str) -> Gauge:
            return Gauge(
                name,
                f"{help_text} Report mode: never blocks promotion.",
                HARNESS_LABELS,
                registry=r,
            )

        self.harness_cases = harness_gauge(
            "oap_agent_harness_cases", "Cases in the agent's latest finished harness run."
        )
        self.harness_passed = harness_gauge(
            "oap_agent_harness_passed", "Cases that passed every check in the latest run."
        )
        self.harness_failed = harness_gauge(
            "oap_agent_harness_failed", "Cases a check scored below its pass score."
        )
        self.harness_case_errors = harness_gauge(
            "oap_agent_harness_case_errors",
            "Cases nothing could be learned from (judge error after retries, "
            "transport or 4xx on the preview path, run deadline); they are "
            "outside the pass ratio.",
        )
        self.harness_pass_ratio = harness_gauge(
            "oap_agent_harness_pass_ratio",
            "passed / (passed + failed) of the latest completed run. Absent when "
            "no run has completed or the latest run errored.",
        )
        self.harness_threshold = harness_gauge(
            "oap_agent_harness_threshold", "The pass ratio the agent declared."
        )
        self.harness_threshold_met = harness_gauge(
            "oap_agent_harness_threshold_met",
            "1 when pass_ratio >= threshold, 0 otherwise. Informational.",
        )
        self.harness_last_run = harness_gauge(
            "oap_agent_harness_last_run_timestamp_seconds",
            "Unix time the latest harness run finished.",
        )
        self.harness_info = Gauge(
            "oap_agent_harness_info",
            "Always 1. Revision, mode, judge model, trigger and status of the "
            "latest finished harness run. Report mode: never blocks promotion.",
            HARNESS_INFO_LABELS,
            registry=r,
        )
        self.harness_judge_key_present = Gauge(
            "oap_agent_harness_judge_key_present",
            "1 when the harness judge virtual key is available, 0 when it is not "
            "(no new harness run starts while it is required). Report mode: never "
            "blocks promotion.",
            ("cluster",),
            registry=r,
        )
        self.harness_runs = Counter(
            "oap_agent_harness_runs_total",
            "Finished or superseded harness runs, by result "
            "(completed|error|superseded). Report mode: never blocks promotion.",
            ("cluster", "result"),
            registry=r,
        )

    # ── publishing ────────────────────────────────────────────────────────

    def publish_harness(self, rows: list["HarnessRow"]) -> None:
        """Replace every harness series with the latest finished run per agent."""
        for gauge in (
            self.harness_cases,
            self.harness_passed,
            self.harness_failed,
            self.harness_case_errors,
            self.harness_pass_ratio,
            self.harness_threshold,
            self.harness_threshold_met,
            self.harness_last_run,
            self.harness_info,
        ):
            gauge.clear()
        for row in rows:
            labels = (row.cluster, row.namespace, row.agent, row.dataset)
            self.harness_info.labels(
                *labels,
                row.revision,
                row.mode,
                row.judge_model,
                row.trigger,
                row.status,
            ).set(1)
            self.harness_threshold.labels(*labels).set(row.threshold)
            self.harness_last_run.labels(*labels).set(row.last_run_timestamp)
            for gauge, value in (
                (self.harness_cases, row.cases),
                (self.harness_passed, row.passed),
                (self.harness_failed, row.failed),
                (self.harness_case_errors, row.errors),
            ):
                if value is not None:
                    gauge.labels(*labels).set(value)
            if row.pass_ratio is not None:
                self.harness_pass_ratio.labels(*labels).set(row.pass_ratio)
            if row.threshold_met is not None:
                self.harness_threshold_met.labels(*labels).set(
                    1 if row.threshold_met else 0
                )

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
    # CATALOG name (`helpfulness`). This is the metric label, deliberately
    # judge-independent: which judge scored an agent is carried by
    # oap_agent_eval_info, so two agents on different judges stay one series per
    # evaluator and a dashboard query does not have to know the judge.
    evaluator: str
    # EVALUATOR OBJECT name (`helpfulness`, or `helpfulness--judge-haiku` for a
    # non-default judge). This is what Langfuse names the SCORE, so it is what
    # the metrics loop must filter on.
    #
    # The two are not interchangeable and conflating them is a silent zero: the
    # loop filtered scores by the catalog name, which matches nothing for any
    # agent on a non-default judge. Measured on the hub: eval-multidim
    # (judge-haiku) had 3 scores for each of its four evaluators in Langfuse and
    # reported 0 for all of them, in both windows, with no error anywhere.
    evaluator_object: str
    threshold: float


@dataclass(frozen=True)
class HarnessRow:
    """One agent's latest finished harness run. Counts are None for an errored run."""

    cluster: str
    namespace: str
    agent: str
    dataset: str
    revision: str
    mode: str
    judge_model: str
    trigger: str
    status: str
    threshold: float
    last_run_timestamp: float
    cases: int | None
    passed: int | None
    failed: int | None
    errors: int | None
    pass_ratio: float | None
    threshold_met: bool | None


@dataclass(frozen=True)
class InfoRow:
    cluster: str
    namespace: str
    agent: str
    agent_model: str
    judge_model: str
    strictness: str
    coverage: str
