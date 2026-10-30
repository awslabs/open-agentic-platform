"""Parsing the component's ConfigMap, including the shapes it should reject."""

from __future__ import annotations

import pytest

from app.declarations import DeclarationError, DeclarationReader, parse_declaration
from tests.conftest import make_configmap


def cm(data, namespace="team-a", name="support-evaluation"):
    return make_configmap(namespace, name, data)


def test_parses_the_components_output():
    decl = parse_declaration(
        cm(
            {
                "agent": "support",
                "backend": "centralized",
                "model": "claude-sonnet",
                "strictness": "high",
                "coverage": "low",
                "evaluators": '["goal-completion", {"name": "safety", "strictness": "low"}]',
            }
        )
    )
    assert decl.agent == "support"
    assert decl.namespace == "team-a"
    assert decl.strictness == "high"
    assert decl.evaluators[0].name == "goal-completion"
    assert decl.evaluators[0].strictness is None
    assert decl.evaluators[1].strictness == "low"
    assert decl.sampling is None
    assert decl.judge is None


def test_trace_tag_is_cluster_scoped():
    decl = parse_declaration(
        cm({"agent": "support", "evaluators": '["safety"]'})
    )
    assert decl.trace_tag("prod") == "agent:prod/team-a/support"


def test_sampling_is_parsed_and_bounded():
    decl = parse_declaration(
        cm({"agent": "a", "evaluators": '["safety"]', "sampling": "0.25"})
    )
    assert decl.sampling == 0.25

    for bad in ["0", "1.5", "-0.1", "abc"]:
        with pytest.raises(DeclarationError):
            parse_declaration(
                cm({"agent": "a", "evaluators": '["safety"]', "sampling": bad})
            )


def test_duplicate_evaluator_is_rejected():
    """Two entries for one evaluator would mean two thresholds for one series."""
    with pytest.raises(DeclarationError, match="more than once"):
        parse_declaration(
            cm({"agent": "a", "evaluators": '["safety", {"name": "safety"}]'})
        )


@pytest.mark.parametrize(
    "data",
    [
        {"agent": "a", "evaluators": "not json"},
        {"agent": "a", "evaluators": "[]"},
        {"agent": "a", "evaluators": '[{"strictness": "low"}]'},
        {"agent": "a", "evaluators": '["safety"]', "strictness": "extreme"},
        {"agent": "a", "evaluators": '[{"name": "s", "strictness": "extreme"}]'},
        {"agent": "a", "evaluators": "[42]"},
    ],
)
def test_malformed_declarations_are_rejected(data):
    with pytest.raises(DeclarationError):
        parse_declaration(cm(data))


def test_missing_agent_name_is_rejected():
    bad = make_configmap("team-a", "x-evaluation", {"evaluators": '["safety"]'}, labels={})
    with pytest.raises(DeclarationError):
        parse_declaration(bad)


def test_one_bad_configmap_does_not_stop_the_others():
    class FakeCore:
        def list_config_map_for_all_namespaces(self, label_selector):
            class Listing:
                items = [
                    make_configmap("a", "good-evaluation", {
                        "agent": "good", "evaluators": '["safety"]'
                    }),
                    make_configmap("b", "bad-evaluation", {
                        "agent": "bad", "evaluators": "nonsense"
                    }),
                ]

            return Listing()

    good, bad = DeclarationReader(FakeCore()).list()
    assert [d.agent for d in good] == ["good"]
    assert len(bad) == 1
    assert bad[0][0].metadata.name == "bad-evaluation"
