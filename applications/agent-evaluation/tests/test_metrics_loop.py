"""The metrics loop: pass ratios, idle agents, batching, and strict parsing."""

from __future__ import annotations

import pytest

from app.cluster_loop import ClusterPlan
from app.langfuse_client import LangfuseError
from app.metrics import ThresholdRow
from app.metrics_loop import (
    TAG_QUERY_BUDGET_BYTES,
    MetricsLoop,
    MetricsSchemaError,
    agent_from_tags,
    chunk_tags,
    row_count_and_avg,
    window_delta,
)
from tests.conftest import FakeLangfuse


def tags_for(cluster, ns, agent, *extra):
    """An observation's tag array, in the shape Langfuse really returns it:
    every tag on the observation, not just the one that matched the filter."""
    return [f"agent:{cluster}/{ns}/{agent}", agent, "source:chat", *extra]


def plan_with(thresholds):
    """thresholds: (ns, agent, catalog_name, evaluator_object, threshold)."""
    return ClusterPlan(
        groups={},
        thresholds=[
            ThresholdRow("dev", ns, agent, catalog, obj, threshold)
            for ns, agent, catalog, obj, threshold in thresholds
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


def is_passed_query(query):
    """A passed query is the one carrying a value threshold filter."""
    return any(f["column"] == "value" for f in query["filters"])


class ScriptedLangfuse(FakeLangfuse):
    """Answers the scored query and the passed queries differently."""

    def __init__(self, scored_rows, passed_rows=None, fail_on=None):
        super().__init__()
        self._scored = scored_rows
        self._passed = passed_rows if passed_rows is not None else []
        self._fail_on = fail_on
        self.queries: list[dict] = []

    def query_metrics(self, query):
        self.queries.append(query)
        if self._fail_on is not None and len(self.queries) >= self._fail_on:
            raise LangfuseError("rate limited", status=429)
        return self._passed if is_passed_query(query) else self._scored

    @property
    def scored_queries(self):
        return [q for q in self.queries if not is_passed_query(q)]

    @property
    def passed_queries(self):
        return [q for q in self.queries if is_passed_query(q)]


SAFETY = ("team-a", "support", "safety", "safety", 0.9)


def test_pass_ratio_uses_the_agents_own_threshold(settings, catalog, metrics):
    langfuse = ScriptedLangfuse(
        [{"tags": tags_for("dev", "team-a", "support"), "name": "safety",
          "count_count": 10, "avg_value": 0.91}],
        [{"tags": tags_for("dev", "team-a", "support"), "count_count": 7}],
    )
    loop = MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY]))

    assert loop.run_once()

    labels = dict(
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_scores", **labels) == 10
    assert sample(metrics.registry, "oap_agent_eval_passed", **labels) == 7
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 0.7
    assert sample(metrics.registry, "oap_agent_eval_score_avg", **labels) == 0.91

    # The threshold went into the query, not into a Langfuse rule.
    value_filter = next(
        f for f in langfuse.passed_queries[0]["filters"] if f["column"] == "value"
    )
    assert value_filter["operator"] == ">="
    assert value_filter["value"] == 0.9


def test_scores_are_matched_by_evaluator_object_name_not_catalog_name(
    settings, catalog, metrics
):
    """The judge suffix bug: an agent on a non-default judge has scores named
    `<evaluator>--<judge>`, and filtering by the catalog name matches nothing.

    Measured on the hub before this was fixed: eval-multidim (judge-haiku) had 3
    scores for each of four evaluators in Langfuse and reported 0 for all of
    them, in both windows, with no error logged anywhere.
    """
    plan = plan_with(
        [("agents", "multidim", "helpfulness", "helpfulness--judge-haiku", 0.7)]
    )
    langfuse = ScriptedLangfuse(
        [{"tags": tags_for("dev", "agents", "multidim"),
          "name": "helpfulness--judge-haiku", "count_count": 3, "avg_value": 0.96}],
        [{"tags": tags_for("dev", "agents", "multidim"), "count_count": 3}],
    )
    assert MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan
    ).run_once()

    # Queried by the OBJECT name...
    name_filter = next(
        f for f in langfuse.scored_queries[0]["filters"] if f["column"] == "name"
    )
    assert name_filter["value"] == ["helpfulness--judge-haiku"]

    # ...but LABELLED with the catalog name, so the series is judge-independent.
    labels = dict(
        cluster="dev", namespace="agents", agent="multidim",
        evaluator="helpfulness", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_scores", **labels) == 3
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0
    # And nothing under the suffixed name.
    assert sample(
        metrics.registry, "oap_agent_eval_scores",
        evaluator="helpfulness--judge-haiku",
    ) is None


def test_one_scored_query_per_window_covers_every_agent(settings, catalog, metrics):
    """Batching: query count tracks windows and thresholds, not agent count."""
    agents = [
        (f"team-{i}", f"agent-{i}", "safety", "safety", 0.9) for i in range(25)
    ]
    scored = [
        {"tags": tags_for("dev", ns, agent), "name": "safety",
         "count_count": 4, "avg_value": 0.95}
        for ns, agent, _c, _o, _t in agents
    ]
    passed = [
        {"tags": tags_for("dev", ns, agent), "count_count": 4}
        for ns, agent, _c, _o, _t in agents
    ]
    langfuse = ScriptedLangfuse(scored, passed)
    assert MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with(agents)
    ).run_once()

    windows = len(settings.metrics_windows)
    # 25 agents sharing one (evaluator, threshold): 1 + 1 queries per window.
    assert len(langfuse.scored_queries) == windows
    assert len(langfuse.passed_queries) == windows
    # The one scored query named all 25 agents.
    tag_filter = next(
        f for f in langfuse.scored_queries[0]["filters"] if f["column"] == "tags"
    )
    assert len(tag_filter["value"]) == 25

    # Every agent still got its own series.
    for ns, agent, _c, _o, _t in agents:
        assert sample(
            metrics.registry, "oap_agent_eval_scores",
            cluster="dev", namespace=ns, agent=agent,
            evaluator="safety", window="24h",
        ) == 4


def test_agents_split_by_threshold_into_separate_passed_queries(
    settings, catalog, metrics
):
    """Two agents, same evaluator, different strictness -> two grouped queries."""
    plan = plan_with([
        ("team-a", "strict", "safety", "safety", 0.97),
        ("team-b", "loose", "safety", "safety", 0.8),
    ])
    scored = [
        {"tags": tags_for("dev", "team-a", "strict"), "name": "safety",
         "count_count": 2, "avg_value": 0.99},
        {"tags": tags_for("dev", "team-b", "loose"), "name": "safety",
         "count_count": 2, "avg_value": 0.85},
    ]
    langfuse = ScriptedLangfuse(scored, [])
    assert MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan).run_once()

    per_window = len(langfuse.passed_queries) // len(settings.metrics_windows)
    assert per_window == 2
    thresholds = {
        next(f for f in q["filters"] if f["column"] == "value")["value"]
        for q in langfuse.passed_queries
    }
    assert thresholds == {0.97, 0.8}


def test_an_idle_agent_has_no_pass_ratio_rather_than_zero(settings, catalog, metrics):
    """0% reads as "everything failed"; absent reads as "no traffic"."""
    langfuse = ScriptedLangfuse([], [])
    loop = MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY]))

    assert loop.run_once()

    labels = dict(
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_scores", **labels) == 0
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) is None


def test_no_passed_query_is_sent_when_nothing_was_scored(settings, catalog, metrics):
    """An all-idle cluster costs exactly one query per window."""
    langfuse = ScriptedLangfuse([], [])
    MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY])
    ).run_once()
    assert langfuse.passed_queries == []
    assert len(langfuse.scored_queries) == len(settings.metrics_windows)


def test_a_throttled_pass_keeps_the_previous_values(settings, catalog, metrics):
    plan = plan_with([SAFETY])
    good = ScriptedLangfuse(
        [{"tags": tags_for("dev", "team-a", "support"), "name": "safety",
          "count_count": 10, "avg_value": 0.95}],
        [{"tags": tags_for("dev", "team-a", "support"), "count_count": 10}],
    )
    assert MetricsLoop(settings, catalog, good, metrics, lambda: plan).run_once()

    labels = dict(
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0
    before = sample(
        metrics.registry, "oap_agent_eval_last_success_timestamp_seconds",
        cluster="dev", loop="metrics",
    )

    throttled = ScriptedLangfuse([], [], fail_on=1)
    assert not MetricsLoop(settings, catalog, throttled, metrics, lambda: plan).run_once()

    # Stale, not wrong: the old ratio stands and the success clock stopped.
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0
    assert sample(
        metrics.registry, "oap_agent_eval_last_success_timestamp_seconds",
        cluster="dev", loop="metrics",
    ) == before
    assert sample(
        metrics.registry, "oap_agent_eval_errors_total",
        cluster="dev", loop="metrics",
    ) == 1


def test_passed_is_never_greater_than_scored(settings, catalog, metrics):
    """The two counts come from separate queries at slightly different times."""
    langfuse = ScriptedLangfuse(
        [{"tags": tags_for("dev", "team-a", "support"), "name": "safety",
          "count_count": 5, "avg_value": 0.95}],
        [{"tags": tags_for("dev", "team-a", "support"), "count_count": 9}],
    )
    MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY])
    ).run_once()

    labels = dict(
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    )
    assert sample(metrics.registry, "oap_agent_eval_passed", **labels) == 5
    assert sample(metrics.registry, "oap_agent_eval_pass_ratio", **labels) == 1.0


def test_queries_are_scoped_to_this_clusters_tag(settings, catalog, metrics):
    langfuse = ScriptedLangfuse(
        [{"tags": tags_for("dev", "team-a", "support"), "name": "safety",
          "count_count": 1, "avg_value": 1.0}],
        [{"tags": tags_for("dev", "team-a", "support"), "count_count": 1}],
    )
    MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY])
    ).run_once()

    for query in langfuse.queries:
        tags = next(f for f in query["filters"] if f["column"] == "tags")
        assert tags["value"] == ["agent:dev/team-a/support"]


def test_another_clusters_scores_in_the_shared_project_are_ignored(
    settings, catalog, metrics
):
    """The project is fleet-wide; a row without THIS cluster's scoped tag is not ours."""
    langfuse = ScriptedLangfuse(
        [
            {"tags": tags_for("dev", "team-a", "support"), "name": "safety",
             "count_count": 3, "avg_value": 0.95},
            {"tags": tags_for("prod", "team-a", "support"), "name": "safety",
             "count_count": 99, "avg_value": 0.1},
        ],
        [{"tags": tags_for("dev", "team-a", "support"), "count_count": 3}],
    )
    MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY])
    ).run_once()

    assert sample(
        metrics.registry, "oap_agent_eval_scores",
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    ) == 3


# ── request-line budget ────────────────────────────────────────────────────


def test_a_large_fleet_is_split_below_the_request_line_limit(
    settings, catalog, metrics
):
    """The query travels in the URL, so a big tag list is an HTTP 431, not a slow query.

    Measured against Langfuse v4.72.0: 339 thirty-character tags (16208 URL
    bytes) succeeded and 340 answered `431 Request Header Fields Too Large`.
    Batching every agent into one query would therefore have capped a cluster at
    ~339 agents -- fewer with long names -- and past it EVERY pass would fail
    rather than degrade.
    """
    import json as _json
    import urllib.parse as _urlparse

    # 600 agents with realistic, long-ish names.
    agents = [
        (f"team-with-a-long-namespace-{i:03d}", f"customer-support-agent-{i:03d}",
         "safety", "safety", 0.9)
        for i in range(600)
    ]
    scored = [
        {"tags": tags_for("dev", ns, agent), "name": "safety",
         "count_count": 2, "avg_value": 0.95}
        for ns, agent, _c, _o, _t in agents
    ]
    passed = [
        {"tags": tags_for("dev", ns, agent), "count_count": 2}
        for ns, agent, _c, _o, _t in agents
    ]
    langfuse = ScriptedLangfuse(scored, passed)
    assert MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: plan_with(agents)
    ).run_once()

    # It took more than one query, and every one fits the budget.
    assert len(langfuse.scored_queries) > len(settings.metrics_windows)
    for query in langfuse.queries:
        tags = next(f for f in query["filters"] if f["column"] == "tags")["value"]
        encoded = len(_urlparse.quote(_json.dumps(tags)))
        assert encoded <= TAG_QUERY_BUDGET_BYTES, (
            f"a query carried {encoded} encoded bytes of tags"
        )

    # Still far cheaper than per-agent, and every agent is still reported.
    assert len(langfuse.queries) < len(agents)
    for ns, agent, _c, _o, _t in agents:
        assert sample(
            metrics.registry, "oap_agent_eval_scores",
            cluster="dev", namespace=ns, agent=agent,
            evaluator="safety", window="24h",
        ) == 2


def test_a_group_split_across_chunks_merges_its_passed_counts():
    """Overwriting instead of merging would report 0 passed for all but the last chunk."""
    tags = [f"agent:dev/ns/agent-{i:04d}" for i in range(400)]
    chunks = chunk_tags(tags, budget=1000)
    assert len(chunks) > 1
    # No tag lost, none duplicated, order preserved.
    flat = [t for chunk in chunks for t in chunk]
    assert flat == tags


def test_chunking_never_drops_an_oversized_tag():
    huge = "agent:dev/" + ("x" * 5000)
    assert chunk_tags([huge], budget=100) == [[huge]]
    assert chunk_tags([], budget=100) == []


# ── strict response parsing ────────────────────────────────────────────────


def test_the_recorded_langfuse_response_parses():
    """Built from a real response, so a rename fails here rather than in prod.

    Captured from Langfuse v4.72.0 on the hub, GET /api/public/metrics over the
    scores-numeric view, dimensions [tags, name], metrics count(count)+avg(value).
    """
    recorded = {
        "avg_value": 0.7272727272727273,
        "count_count": 33,
        "name": "helpfulness",
        "tags": [
            "agent:hub/agents/eval-smoketest",
            "eval-smoketest",
            "source:chat",
        ],
    }
    assert row_count_and_avg(recorded, "recorded") == (33, 0.7272727272727273)
    assert agent_from_tags(recorded["tags"], "hub") == "agents/eval-smoketest"


def test_a_renamed_aggregate_key_is_an_error_not_a_zero(settings, catalog, metrics):
    """The whole point: silence here is indistinguishable from a quality collapse."""
    renamed = {
        "tags": tags_for("dev", "team-a", "support"),
        "name": "safety",
        "countCount": 10,   # the v3-style spelling
        "avgValue": 0.9,
    }
    with pytest.raises(MetricsSchemaError):
        row_count_and_avg(renamed, "renamed")

    langfuse = ScriptedLangfuse([renamed], [])
    loop = MetricsLoop(settings, catalog, langfuse, metrics, lambda: plan_with([SAFETY]))
    assert not loop.run_once()
    # Nothing published, and the error is counted.
    assert sample(
        metrics.registry, "oap_agent_eval_scores",
        cluster="dev", namespace="team-a", agent="support",
        evaluator="safety", window="24h",
    ) is None
    assert sample(
        metrics.registry, "oap_agent_eval_errors_total",
        cluster="dev", loop="metrics",
    ) == 1


def test_a_null_average_is_allowed_but_a_missing_one_is_not():
    present = {"count_count": 0, "avg_value": None}
    assert row_count_and_avg(present, "null avg") == (0, None)
    with pytest.raises(MetricsSchemaError):
        row_count_and_avg({"count_count": 0}, "absent avg")


def test_a_non_list_tags_dimension_is_a_schema_error():
    with pytest.raises(MetricsSchemaError):
        agent_from_tags("agent:hub/agents/x", "hub")


def test_tags_without_this_clusters_scoped_tag_yield_none():
    assert agent_from_tags(["eval-smoketest", "source:chat"], "hub") is None
    assert agent_from_tags(tags_for("other", "ns", "a"), "hub") is None


# ── windows ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("window,hours", [("24h", 24), ("7d", 168), ("30m", 0.5)])
def test_window_parsing(window, hours):
    assert window_delta(window).total_seconds() == pytest.approx(hours * 3600)


def test_bad_window_is_rejected():
    from app.metrics_loop import WindowError

    for bad in ["", "24", "24x", "-1h", "0h"]:
        with pytest.raises(WindowError):
            window_delta(bad)
