"""The per-cluster loop: grouping, idempotence, and scoped deletion."""

from __future__ import annotations

import dataclasses

import pytest

from app.cluster_loop import ClusterLoop
from app.declarations import AgentDeclaration, RequestedEvaluator
from app.events import (
    REASON_EVALUATOR_NOT_PUBLISHED,
    REASON_UNKNOWN_EVALUATOR,
    REASON_UNKNOWN_JUDGE,
)
from app.naming import rule_name
from tests.conftest import FakeEvents, FakeLangfuse, FakeReader


def declaration(
    agent="support",
    namespace="team-a",
    evaluators=("goal-completion",),
    strictness="medium",
    coverage="medium",
    sampling=None,
    judge=None,
    model="claude-sonnet",
):
    return AgentDeclaration(
        agent=agent,
        namespace=namespace,
        configmap=f"{agent}-evaluation",
        backend="centralized",
        model=model,
        strictness=strictness,
        coverage=coverage,
        evaluators=tuple(
            e if isinstance(e, RequestedEvaluator) else RequestedEvaluator(e)
            for e in evaluators
        ),
        sampling=sampling,
        judge=judge,
    )


def build(settings, catalog, metrics, declarations, published=None, rules=None):
    langfuse = FakeLangfuse(allow_project_writes=settings.is_hub)
    langfuse.evaluators = [
        {"name": n, "version": 1} for n in (published or ["goal-completion"])
    ]
    langfuse.rules = list(rules or [])
    recorder = FakeEvents()
    loop = ClusterLoop(
        settings,
        catalog,
        langfuse,
        metrics,
        FakeReader(declarations),
        recorder,
    )
    return loop, langfuse, recorder


def test_two_agents_with_the_same_settings_share_one_rule(
    settings, catalog, metrics
):
    decls = [
        declaration(agent="a", namespace="team-a"),
        declaration(agent="b", namespace="team-b"),
    ]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()

    assert len(langfuse.rules) == 1
    rule = langfuse.rules[0]
    tags = next(f for f in rule["filter"] if f["column"] == "tags")["value"]
    assert sorted(tags) == [
        "agent:dev/team-a/a",
        "agent:dev/team-b/b",
    ]


def test_differing_strictness_still_shares_one_rule(settings, catalog, metrics):
    """strictness is not part of the group: it is applied when reports run."""
    decls = [
        declaration(agent="a", strictness="low"),
        declaration(agent="b", namespace="team-b", strictness="high"),
    ]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    assert len(langfuse.rules) == 1

    # ...but the two agents get different thresholds for the report.
    thresholds = {(t.agent, t.evaluator): t.threshold for t in loop.last_plan.thresholds}
    assert thresholds[("a", "goal-completion")] == 0.5
    assert thresholds[("b", "goal-completion")] == 0.85


def test_differing_sampling_produces_two_rules(settings, catalog, metrics):
    decls = [
        declaration(agent="a", coverage="low"),
        declaration(agent="b", namespace="team-b", coverage="high"),
    ]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    assert len(langfuse.rules) == 2
    assert sorted(r["sampling"] for r in langfuse.rules) == [0.01, 0.5]


def test_second_pass_is_a_no_op(settings, catalog, metrics):
    decls = [declaration()]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    created = [c for c in langfuse.calls if c.startswith("create_rule")]
    langfuse.calls.clear()

    assert loop.run_once()
    assert len(created) == 1
    assert not [c for c in langfuse.calls if c.startswith(("create_rule", "update_rule"))]


def test_removing_an_agent_updates_the_rule_then_deletes_it(
    settings, catalog, metrics
):
    two = [declaration(agent="a"), declaration(agent="b", namespace="team-b")]
    loop, langfuse, recorder = build(settings, catalog, metrics, two)
    assert loop.run_once()

    # One agent's Application is deleted: the rule survives, minus its tag.
    loop._reader = FakeReader([declaration(agent="a")])
    assert loop.run_once()
    assert len(langfuse.rules) == 1
    tags = next(f for f in langfuse.rules[0]["filter"] if f["column"] == "tags")["value"]
    assert tags == ["agent:dev/team-a/a"]

    # The last agent goes: so does the rule.
    loop._reader = FakeReader([])
    assert loop.run_once()
    assert langfuse.rules == []


def test_another_clusters_rules_are_never_deleted(settings, catalog, metrics):
    """The ownership guarantee, with the ambiguous-prefix case included."""
    foreign = [
        {"id": "r1", "name": rule_name("prod", "safety", "judge-default", 0.1)},
        # A cluster whose name has `dev` as a prefix — the case that would break
        # a prefix-matched delete.
        {"id": "r2", "name": rule_name("dev-2", "safety", "judge-default", 0.1)},
        {"id": "r3", "name": "someone-elses-rule"},
    ]
    loop, langfuse, _ = build(
        settings, catalog, metrics, [], rules=list(foreign)
    )
    assert loop.run_once()

    assert {r["id"] for r in langfuse.rules} == {"r1", "r2", "r3"}
    assert not [c for c in langfuse.calls if c.startswith("delete_rule")]


def test_unknown_evaluator_is_evented_and_skipped(settings, catalog, metrics):
    decls = [declaration(evaluators=("goal-completion", "does-not-exist"))]
    loop, langfuse, recorder = build(settings, catalog, metrics, decls)
    assert loop.run_once()

    assert REASON_UNKNOWN_EVALUATOR in recorder.reasons()
    # The valid evaluator still gets its rule: one bad name does not take the
    # agent's whole declaration down.
    assert len(langfuse.rules) == 1


def test_evaluator_in_catalog_but_not_published_waits_for_the_hub(
    settings, catalog, metrics
):
    """A cluster upgraded ahead of the hub: Event and skip, never a write."""
    decls = [declaration(evaluators=("safety",))]
    loop, langfuse, recorder = build(
        settings, catalog, metrics, decls, published=["goal-completion"]
    )
    assert loop.run_once()

    assert REASON_EVALUATOR_NOT_PUBLISHED in recorder.reasons()
    assert langfuse.rules == []
    # The critical half: a spoke never tried to create the evaluator itself.
    assert not [c for c in langfuse.calls if c.startswith("create_evaluator")]

    # The hub lands it; the next pass creates the rule with no action here.
    langfuse.evaluators.append({"name": "safety", "version": 1})
    assert loop.run_once()
    assert len(langfuse.rules) == 1


def test_unknown_judge_is_evented_and_skipped(settings, catalog, metrics):
    decls = [declaration(judge="judge-not-on-the-list")]
    loop, langfuse, recorder = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    assert REASON_UNKNOWN_JUDGE in recorder.reasons()
    assert langfuse.rules == []


def test_non_default_judge_uses_the_judge_specific_evaluator(
    settings, catalog, metrics
):
    decls = [declaration(judge="judge-strong")]
    loop, langfuse, _ = build(
        settings,
        catalog,
        metrics,
        decls,
        published=["goal-completion", "goal-completion--judge-strong"],
    )
    assert loop.run_once()
    assert langfuse.rules[0]["evaluator"] == "goal-completion--judge-strong"


def test_explicit_sampling_overrides_coverage(settings, catalog, metrics):
    decls = [declaration(coverage="low", sampling=0.42)]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    assert langfuse.rules[0]["sampling"] == 0.42
    assert loop.last_plan.info[0].coverage == "explicit"


def test_rule_cap_is_reported_rather_than_left_to_a_409(settings, catalog, metrics):
    tight = dataclasses.replace(settings, max_active_rules=1)
    decls = [
        declaration(agent="a", coverage="low"),
        declaration(agent="b", namespace="team-b", coverage="high"),
    ]
    loop, langfuse, _ = build(tight, catalog, metrics, decls)
    assert not loop.run_once()
    assert len(langfuse.rules) == 1


def test_rule_targets_the_root_observation(settings, catalog, metrics):
    loop, langfuse, _ = build(settings, catalog, metrics, [declaration()])
    assert loop.run_once()
    rule = langfuse.rules[0]
    assert rule["target"] == "observation"
    assert any(
        f["column"] == "parentObservationId" and f["operator"] == "is null"
        for f in rule["filter"]
    )
