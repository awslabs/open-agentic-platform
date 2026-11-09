"""Harness declarations: the ConfigMap the agent component renders."""

from __future__ import annotations

import pytest

from app.harness_declarations import (
    LABEL_SELECTOR,
    HarnessDeclarationError,
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
