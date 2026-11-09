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
