"""Harness declarations: the ConfigMap the agent component renders."""

from __future__ import annotations

import pytest

from app.harness_declarations import (
    LABEL_SELECTOR,
    HarnessDeclarationError,
    HarnessDeclarationReader,
    parse_harness_declaration,
)
from tests.conftest import make_configmap

BASE = {
    "agent": "assistant",
    "dataset": "example-smoke-v1",
    "checks": '["expected-answer"]',
    "threshold": "0.8",
    "mode": "report",
    "previewService": "assistant-preview",
    "previewPort": "8083",
}


def cm(**overrides):
    data = {**BASE, **overrides}
    data = {k: v for k, v in data.items() if v is not None}
    return make_configmap(
        "team-a", "assistant-harness", data, labels={LABEL_SELECTOR: "assistant"}
    )


def test_valid_declaration():
    d = parse_harness_declaration(cm())
    assert d.key == "team-a/assistant"
    assert d.dataset == "example-smoke-v1"
    assert d.checks == ("expected-answer",)
    assert d.threshold == 0.8
    assert d.mode == "report"
    assert (d.preview_service, d.preview_port) == ("assistant-preview", 8083)


def test_gate_mode_is_rejected():
    with pytest.raises(HarnessDeclarationError, match="mode"):
        parse_harness_declaration(cm(mode="gate"))


@pytest.mark.parametrize("threshold", ["0", "1.5", "x"])
def test_bad_threshold(threshold):
    with pytest.raises(HarnessDeclarationError, match="threshold"):
        parse_harness_declaration(cm(threshold=threshold))


@pytest.mark.parametrize("checks", ["[]", '["helpfulness"]', "nope"])
def test_bad_checks(checks):
    with pytest.raises(HarnessDeclarationError, match="check"):
        parse_harness_declaration(cm(checks=checks))


def test_missing_dataset():
    with pytest.raises(HarnessDeclarationError, match="dataset"):
        parse_harness_declaration(cm(dataset=None))


def test_preview_defaults():
    d = parse_harness_declaration(cm(previewService=None, previewPort=None))
    assert d.preview_service == "assistant-preview"
    assert d.preview_port == 8083


@pytest.mark.parametrize(
    "service",
    [
        "evil.example.com/steal?",
        "other.team-b",
        "http://x",
        "Assistant-preview",
        "1-preview",
        "a" * 64,
    ],
)
def test_hostile_or_invalid_preview_service_is_rejected(service):
    with pytest.raises(HarnessDeclarationError, match="previewService"):
        parse_harness_declaration(cm(previewService=service))


@pytest.mark.parametrize(
    "agent",
    ["a" * 64, "Assistant", "team.assistant", "bad/agent", "-assistant", "assistant_1"],
)
def test_invalid_agent_is_rejected(agent):
    with pytest.raises(HarnessDeclarationError, match="agent"):
        parse_harness_declaration(cm(agent=agent, previewService="assistant-preview"))


def test_derived_preview_service_too_long_is_rejected():
    # 60 characters is a valid agent name, but `<agent>-preview` is not a Service name.
    with pytest.raises(HarnessDeclarationError, match="previewService"):
        parse_harness_declaration(cm(agent="a" * 60, previewService=None))


def _selects(labels: dict, selector: str) -> bool:
    """Evaluate the selector forms the reader uses: `key`, `!key` and `key=value`."""
    for clause in filter(None, selector.split(",")):
        if clause.startswith("!"):
            if clause[1:] in labels:
                return False
        elif "=" in clause:
            key, _, value = clause.partition("=")
            if labels.get(key) != value:
                return False
        elif clause not in labels:
            return False
    return True


def test_result_configmaps_are_not_declarations():
    declaration = cm()
    # Labelled the way harness_runner._write_result labels its result ConfigMap.
    result = make_configmap(
        "agent-evaluation",
        "oap-harness-0123456789ab-result",
        {"passRatio": "1.0"},
        labels={
            "harness.oap.io/result": "true",
            "harness.oap.io/job": "oap-harness-0123456789ab",
            "harness.oap.io/agent-namespace": "team-a",
            "harness.oap.io/agent": "assistant",
            "harness.oap.io/revision": "rev1",
            "harness.oap.io/dataset": "example-smoke-v1",
            "app.kubernetes.io/managed-by": "agent-evaluation-reconciler",
        },
    )

    class FakeCore:
        def list_config_map_for_all_namespaces(self, label_selector):
            class Listing:
                items = [
                    c for c in (declaration, result)
                    if _selects(c.metadata.labels or {}, label_selector)
                ]

            return Listing()

    good, bad = HarnessDeclarationReader(FakeCore()).list()
    assert [d.configmap for d in good] == ["assistant-harness"]
    # No parse error means no InvalidHarnessDeclaration Event for the result.
    assert bad == []
