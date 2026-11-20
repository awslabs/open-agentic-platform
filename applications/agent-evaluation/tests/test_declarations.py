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


def _reader_over(*configmaps):
    class FakeCore:
        def list_config_map_for_all_namespaces(self, label_selector):
            return type("Listing", (), {"items": list(configmaps)})()

    return DeclarationReader(FakeCore())


def test_deeply_nested_evaluators_are_malformed_not_a_crash():
    # Reviewer repro (C2): "[" * 100000 raised RecursionError out of the pass.
    good, bad = _reader_over(
        make_configmap("a", "good-evaluation", {"agent": "good", "evaluators": '["safety"]'}),
        make_configmap("b", "deep-evaluation", {"agent": "deep", "evaluators": "[" * 100000}),
    ).list()
    assert [d.agent for d in good] == ["good"]
    assert [cm.metadata.name for cm, _ in bad] == ["deep-evaluation"]
    assert "evaluators" in bad[0][1]


def test_an_unexpected_parser_crash_is_isolated_per_configmap(monkeypatch):
    from app import declarations

    real = declarations.parse_declaration

    def crashy(cm):
        if cm.metadata.name == "boom-evaluation":
            raise KeyError("surprise")
        return real(cm)

    monkeypatch.setattr(declarations, "parse_declaration", crashy)
    good, bad = _reader_over(
        make_configmap("a", "good-evaluation", {"agent": "good", "evaluators": '["safety"]'}),
        make_configmap("b", "boom-evaluation", {"agent": "boom", "evaluators": '["safety"]'}),
    ).list()
    assert [d.agent for d in good] == ["good"]
    assert bad[0][0].metadata.name == "boom-evaluation" and "KeyError" in bad[0][1]
