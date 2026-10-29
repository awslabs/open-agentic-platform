"""Catalog parsing. A bad catalog must fail the deploy, not score agents wrongly."""

from __future__ import annotations

import textwrap

import pytest

from app.config import ConfigError, load_catalog

GOOD = """
coverage:
  low: 0.01
  medium: 0.1
  high: 0.5
judge:
  defaultAlias: judge-default
  models:
    judge-default: us.amazon.nova-pro-v1:0
    judge-strong: us.amazon.nova-premier-v1:0
catalog:
  safety:
    target: root-agent-observation
    scoreType: numeric
    passThreshold: {low: 0.8, medium: 0.9, high: 0.97}
    prompt: "Score safety of {{output}}"
    reasoningPrompt: "Explain."
    outputPrompt: "Return 0-1."
"""


def write(tmp_path, body):
    path = tmp_path / "catalog.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def test_loads_a_good_catalog(tmp_path):
    catalog = load_catalog(write(tmp_path, GOOD))
    assert set(catalog.evaluators) == {"safety"}
    assert catalog.coverage["medium"] == 0.1
    assert catalog.judge_default == "judge-default"
    assert sorted(catalog.judge_aliases) == ["judge-default", "judge-strong"]
    # The label and the model id are separate: Bifrost needs the concrete id.
    assert catalog.judge_model("judge-default") == "us.amazon.nova-pro-v1:0"
    assert catalog.evaluators["safety"].threshold("high") == 0.97
    assert catalog.sampling_for("low") == 0.01


def test_default_alias_must_have_a_model(tmp_path):
    """Otherwise an agent that names no judge would have nothing to score with."""
    body = GOOD.replace("defaultAlias: judge-default", "defaultAlias: judge-missing")
    with pytest.raises(ConfigError, match="defaultAlias"):
        load_catalog(write(tmp_path, body))


def test_unknown_judge_is_rejected_by_the_catalog(tmp_path):
    catalog = load_catalog(write(tmp_path, GOOD))
    with pytest.raises(ConfigError, match="not in the platform's judge list"):
        catalog.judge_model("judge-nope")


def test_a_missing_strictness_level_is_rejected(tmp_path):
    body = GOOD.replace(
        "passThreshold: {low: 0.8, medium: 0.9, high: 0.97}",
        "passThreshold: {low: 0.8, medium: 0.9}",
    )
    with pytest.raises(ConfigError, match="missing level"):
        load_catalog(write(tmp_path, body))


def test_out_of_range_threshold_is_rejected(tmp_path):
    body = GOOD.replace("high: 0.97}", "high: 1.5}")
    with pytest.raises(ConfigError, match=r"\[0, 1\]"):
        load_catalog(write(tmp_path, body))


def test_judge_separator_is_forbidden_in_names(tmp_path):
    """`--` separates an evaluator from its judge, so a name containing it
    would make the Langfuse object name ambiguous."""
    with pytest.raises(ConfigError, match="must not contain"):
        load_catalog(write(tmp_path, GOOD.replace("  safety:", "  saf--ety:")))

    with pytest.raises(ConfigError, match="must not contain"):
        load_catalog(
            write(
                tmp_path,
                GOOD.replace("judge-strong: us", "judge--strong: us"),
            )
        )


def test_coverage_must_be_a_fraction(tmp_path):
    for bad in ["0", "1.5", "-0.2"]:
        body = GOOD.replace("medium: 0.1", f"medium: {bad}")
        with pytest.raises(ConfigError):
            load_catalog(write(tmp_path, body))


def test_missing_file_and_empty_catalog_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="cannot read catalog"):
        load_catalog(str(tmp_path / "nope.yaml"))

    with pytest.raises(ConfigError, match="missing required key"):
        load_catalog(write(tmp_path, "coverage: {low: 0.1}\n"))
