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
from app.naming import JUDGE_PROVIDER, rule_name
from app.project_loop import evaluator_payload
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
    # Evaluators carry the platform judge provider, which is how the loop tells
    # ours from one a human made in the UI, and a stable id, which is what a v4
    # rule is built from.
    langfuse.evaluators = [
        {
            "name": n,
            "id": f"ev-{i}",
            "version": 1,
            "modelConfig": {"provider": JUDGE_PROVIDER, "model": "m"},
        }
        for i, n in enumerate(published or ["goal-completion"])
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


def test_an_empty_evaluator_catalog_never_deletes_rules(
    settings, catalog, metrics
):
    """A successful-but-empty list_evaluators() must not tear down rules.

    An error aborts the pass, but an empty `{"data": []}` used to look exactly
    like "the project has no catalog": every evaluator resolved to
    not-published, `desired` came out empty, and the reconcile deleted every
    rule this cluster owns, recreating them next pass. Because a Langfuse rule
    only judges observations arriving AFTER it exists, the traffic in that
    window is permanently unscored.
    """
    decls = [declaration(agent="a")]
    loop, langfuse, _ = build(settings, catalog, metrics, decls)
    assert loop.run_once()
    assert len(langfuse.rules) == 1
    before = [dict(r) for r in langfuse.rules]

    # Same declarations, same rules — only the catalog read comes back empty.
    langfuse.evaluators = []
    langfuse.calls.clear()

    # The pass fails, so readiness reports it rather than going fake-green...
    assert not loop.run_once()
    # ...and the rules are left exactly as they were.
    assert langfuse.rules == before
    assert not [c for c in langfuse.calls if c.startswith("delete_rule")]


def test_the_real_teardown_path_still_deletes(settings, catalog, metrics):
    """The guard above must not block a legitimate full teardown.

    Every agent dropping its `evaluation` block also yields an empty `desired`,
    but there the catalog is readable — so that deletion must still happen.
    """
    loop, langfuse, _ = build(settings, catalog, metrics, [declaration(agent="a")])
    assert loop.run_once()
    assert len(langfuse.rules) == 1

    loop._reader = FakeReader([])
    assert loop.run_once()
    assert langfuse.rules == []


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
    langfuse.evaluators.append(
        {
            "name": "safety",
            "id": "ev-safety",
            "version": 1,
            "modelConfig": {"provider": JUDGE_PROVIDER, "model": "m"},
        }
    )
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
    # Attached by stable id, not by name: v4 names are not identifiers. The
    # judge-specific evaluator object is the one whose id is assigned.
    strong_id = next(
        e["id"] for e in langfuse.evaluators
        if e["name"] == "goal-completion--judge-strong"
    )
    assert [a["evaluatorId"] for a in langfuse.rules[0]["evaluatorAssignments"]] == [
        strong_id
    ]


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


def test_rule_payload_matches_the_api_contract(settings, catalog, metrics):
    """Shape verified against a live Langfuse at the pin.

    `mapping` is required and every evaluator variable must appear exactly once,
    so a missing one is a 400 missing_variable_mapping at runtime.
    """
    loop, langfuse, _ = build(settings, catalog, metrics, [declaration()])
    assert loop.run_once()
    rule = langfuse.rules[0]

    for required in ("name", "enabled", "evaluatorAssignments"):
        assert required in rule, required
    # Exactly one evaluator per rule here, attached by id.
    assert len(rule["evaluatorAssignments"]) == 1
    assert rule["evaluatorAssignments"][0]["evaluatorId"]
    # variableMapping is null so the rule inherits the evaluator's default,
    # keeping one definition of how each prompt variable is populated.
    assert rule["evaluatorAssignments"][0]["variableMapping"] is None
    # No `target` in v4, and `mapping` moved onto the evaluator.
    for gone in ("target", "mapping", "evaluator"):
        assert gone not in rule


def test_tool_use_maps_its_third_variable(settings, catalog, metrics):
    """An evaluator whose prompt scores tool calls needs tool_calls mapped."""
    import dataclasses as _dc

    tool_use = _dc.replace(
        catalog.evaluators["safety"],
        name="tool-use",
        mapping={"input": "input", "output": "output", "tool_calls": "tool_calls"},
    )
    extended = _dc.replace(
        catalog, evaluators={**catalog.evaluators, "tool-use": tool_use}
    )
    loop, langfuse, _ = build(
        settings,
        extended,
        metrics,
        [declaration(evaluators=("tool-use",))],
        published=["tool-use"],
    )
    assert loop.run_once()
    # The third variable is mapped on the EVALUATOR in v4, which the rule
    # inherits, so the rule itself carries no mapping.
    assert langfuse.rules[0]["evaluatorAssignments"][0]["variableMapping"] is None
    payload = evaluator_payload(
        tool_use, "judge-default", "judge-default", "us.amazon.nova-pro-v1:0"
    )
    assert sorted(m["variable"] for m in payload["variableMapping"]) == [
        "input",
        "output",
        "tool_calls",
    ]


def test_a_catalog_mapping_source_the_api_rejects_is_evented(
    settings, catalog, metrics
):
    """Caught before the call, so one bad catalog entry does not 400 mid-pass."""
    import dataclasses as _dc

    broken = _dc.replace(
        catalog.evaluators["goal-completion"],
        mapping={"input": "input", "output": "toolCalls"},
    )
    bad_catalog = _dc.replace(
        catalog, evaluators={**catalog.evaluators, "goal-completion": broken}
    )
    loop, langfuse, recorder = build(settings, bad_catalog, metrics, [declaration()])
    assert loop.run_once()
    assert langfuse.rules == []
    assert any("toolCalls" in w[3] for w in recorder.warnings)


def test_rule_targets_the_root_observation(settings, catalog, metrics):
    loop, langfuse, _ = build(settings, catalog, metrics, [declaration()])
    assert loop.run_once()
    rule = langfuse.rules[0]
    # v4 rules have no `target` field; an observation rule is the default and
    # experiments are selected with a datasetId filter instead.
    assert "target" not in rule
    # Observation TYPE, not the trace root: the root is the a2a transport span
    # and carries no input or output, so judging it scores nothing meaningful.
    assert any(
        f["column"] == "type"
        and f["operator"] == "any of"
        and f["value"] == ["AGENT"]
        for f in rule["filter"]
    )
    assert not any(
        f["column"] in ("parentObservationId", "isRootObservation")
        for f in rule["filter"]
    )


def test_a_filter_change_is_detected_and_patched(settings, catalog, metrics):
    """The reconciler must converge on a filter change, not just on tags.

    Comparing only the agent tags left every rule already in Langfuse stuck with
    an old, non-matching root-span clause: the tags were identical, so the rule
    looked up to date forever, and a rule whose filter cannot be corrected
    silently scores nothing.
    """
    loop, langfuse, _ = build(settings, catalog, metrics, [declaration()])
    assert loop.run_once()
    rule = langfuse.rules[0]

    # A rule as an older reconciler wrote it: same tags, stale root clause.
    rule["filter"] = [
        {
            "type": "null",
            "column": "parentObservationId",
            "operator": "is null",
            "value": "",
        },
        next(f for f in rule["filter"] if f["column"] == "tags"),
    ]
    langfuse.calls.clear()

    assert loop.run_once()
    assert [
        c for c in langfuse.calls if c.startswith("update_rule")
    ], "a stale filter must be patched"
    assert any(
        f["column"] == "type" for f in langfuse.rules[0]["filter"]
    )
