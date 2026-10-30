"""The metrics loop: pass ratios, idle agents, and all-or-nothing publishing."""

from __future__ import annotations

import json

import pytest

from app.cluster_loop import ClusterPlan
from app.langfuse_client import LangfuseError
from app.metrics import InfoRow, ThresholdRow
from app.metrics_loop import MetricsLoop, window_delta
from tests.conftest import FakeLangfuse


def plan_with(thresholds):
    return ClusterPlan(
        groups={},
        thresholds=[
            ThresholdRow("dev", ns, agent, evaluator, threshold)
            for ns, agent, evaluator, threshold in thresholds
        ],
        info=[],
        declared=1,
        skipped=0,
    )


def sample(registry, name, **labels):
    for metric in registry.collect():
        for s in metric.samples:
            if s.name == name and all(
                s.labels.get(k) == v for k, v in labels.items()
            ):
                return s.value
    return None


class ScriptedLangfuse(FakeLangfuse):
    """Answers the scored query and the passed query differently."""

    def __init__(self, scored_rows, passed_count, fail_on=None):
        super().__init__()
        self._scored = scored_rows
        self._passed = passed_count
        self._fail_on = fail_on
        self.queries: list[dict] = []

    def query_metrics(self, query):
        self.queries.append(query)
        if self._fail_on is not None and len(self.queries) >= self._fail_on:
            raise LangfuseError("rate limited", status=429)
        if query["dimensions"]:
            return self._scored
        return [{"count_count": self._passed}]


def test_pass_ratio_uses_the_agents_own_threshold(settings, catalog, metrics):
    langfuse = ScriptedLangfuse(
        [{"name": "safety", "count_count": 10, "avg_value": 0.91}], passed_count=7
    )
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    loop = MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan)

    assert loop.run_once()

    labels = dict(
        cluster="dev",
        namespace="team-a",
        agent="support",
        evaluator="safety",
        window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_scores", **labels) == 10
    assert sample(metrics.registry, "oap_agent_eval_passed", **labels) == 7
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 0.7
    assert sample(metrics.registry, "oap_agent_eval_score_avg", **labels) == 0.91

    # The threshold went into the query, not into a Langfuse rule.
    passed_query = next(q for q in langfuse.queries if not q["dimensions"])
    value_filter = next(f for f in passed_query["filters"] if f["column"] == "value")
    assert value_filter["operator"] == ">="
    assert value_filter["value"] == 0.9


def test_an_idle_agent_has_no_pass_ratio_rather_than_zero(settings, catalog, metrics):
    """0% reads as "everything failed"; absent reads as "no traffic"."""
    langfuse = ScriptedLangfuse([], passed_count=0)
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    loop = MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan)

    assert loop.run_once()

    labels = dict(
        cluster="dev",
        namespace="team-a",
        agent="support",
        evaluator="safety",
        window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_scores", **labels) == 0
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) is None


def test_no_passed_query_is_sent_when_nothing_was_scored(settings, catalog, metrics):
    """Half the query budget is wasted asking about agents with no traffic."""
    langfuse = ScriptedLangfuse([], passed_count=0)
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan).run_once()
    assert all(q["dimensions"] for q in langfuse.queries)


def test_a_throttled_pass_keeps_the_previous_values(settings, catalog, metrics):
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    good = ScriptedLangfuse(
        [{"name": "safety", "count_count": 10, "avg_value": 0.95}], passed_count=10
    )
    loop = MetricsLoop(settings, catalog, good, metrics, lambda: plan)
    assert loop.run_once()

    labels = dict(
        cluster="dev",
        namespace="team-a",
        agent="support",
        evaluator="safety",
        window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0
    before = sample(
        metrics.registry, "oap_agent_eval_last_success_timestamp_seconds",
        cluster="dev", loop="metrics",
    )

    throttled = ScriptedLangfuse([], passed_count=0, fail_on=1)
    loop = MetricsLoop(settings, catalog, throttled, metrics, lambda: plan)
    assert not loop.run_once()

    # Stale, not wrong: the old ratio stands and the success clock stopped.
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0
    after = sample(
        metrics.registry, "oap_agent_eval_last_success_timestamp_seconds",
        cluster="dev", loop="metrics",
    )
    assert after == before
    assert (
        sample(metrics.registry, "oap_agent_eval_errors_total",
               cluster="dev", loop="metrics") == 1
    )


def test_passed_is_never_greater_than_scored(settings, catalog, metrics):
    """The two counts come from separate queries at slightly different times."""
    langfuse = ScriptedLangfuse(
        [{"name": "safety", "count_count": 5, "avg_value": 0.95}], passed_count=9
    )
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan).run_once()

    labels = dict(
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_passed", **labels) == 5
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0


def test_queries_are_scoped_to_this_clusters_tag(settings, catalog, metrics):
    langfuse = ScriptedLangfuse(
        [{"name": "safety", "count_count": 1, "avg_value": 1.0}], passed_count=1
    )
    plan = plan_with([("team-a", "support", "safety", 0.9)])
    MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan).run_once()

    for query in langfuse.queries:
        tags = next(f for f in query["filters"] if f["column"] == "tags")
        assert tags["value"] == ["agent:dev/team-a/support"]


def test_no_pass_before_the_first_reconcile(settings, catalog, metrics):
    loop = MetricsLoop(settings, catalog, FakeLangfuse(), metrics, lambda: None)
    assert not loop.run_once()


@pytest.mark.parametrize(
    "window,hours", [("24h", 24), ("7d", 168), ("30m", 0.5)]
)
def test_window_parsing(window, hours):
    assert window_delta(window).total_seconds() == pytest.approx(hours * 3600)


def test_bad_window_is_rejected():
    from app.metrics_loop import WindowError

    for bad in ["", "24", "24x", "-1h", "0h"]:
        with pytest.raises(WindowError):
            window_delta(bad)
