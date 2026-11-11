"""The Job's run logic: aggregation, run-level errors and the result contract."""

from __future__ import annotations

import json

import pytest

from app.a2a_client import AgentCallError, AgentReply
from app.datasets import Case, Dataset
from app.harness_config import Check, RunnerSettings, load_checks
from app.harness_results import (
    RESULT_SCHEMA,
    ResultSchemaError,
    parse_result,
    to_configmap_data,
)
from app.harness_runner import run, run_cases
from app.judge import JudgeError, JudgeVerdict

CHECKS = {"expected-answer": Check("expected-answer", "I={{input}} O={{output}}", 0.7)}


def runner_settings(datasets_dir="/nonexistent", **overrides) -> RunnerSettings:
    values = dict(
        target_url="http://assistant-preview.team-a.svc.cluster.local:8083/",
        agent_namespace="team-a",
        agent="assistant",
        revision="abc123",
        dataset="example-smoke-v1",
        checks=("expected-answer",),
        threshold=0.5,
        mode="report",
        trigger="first-revision",
        result_configmap="oap-harness-0123456789ab-result",
        pod_namespace="agent-evaluation",
        cluster="dev",
        bifrost_url="http://bifrost.bifrost.svc.cluster.local:8080/v1",
        judge_model="us.amazon.nova-pro-v1:0",
        case_concurrency=2,
        case_timeout=5,
        ready_timeout=5,
        max_cases=50,
        checks_path="/nonexistent/checks.yaml",
        datasets_dir=str(datasets_dir),
        credentials_dir="/nonexistent",
        judge_virtual_key="sk-bf-test",
    )
    values.update(overrides)
    return RunnerSettings(**values)


def dataset(*ids) -> Dataset:
    return Dataset(
        name="example-smoke-v1",
        description="",
        cases=tuple(Case(i, f"input {i}", f"expected {i}", None, ("t",)) for i in ids),
    )


def agent_fn(behaviour: dict):
    """behaviour: case input -> reply text, or an AgentCallError to raise."""
    seen = []

    def fn(text, context_id):
        seen.append(context_id)
        outcome = behaviour[text]
        if isinstance(outcome, Exception):
            raise outcome
        return AgentReply(text=outcome, raw_state="completed")

    fn.seen = seen
    return fn


def judge_fn(scores: dict):
    """scores: reply text appearing in the prompt -> score, or a JudgeError."""

    def fn(prompt):
        for text, outcome in scores.items():
            if f"O={text}" in prompt:
                if isinstance(outcome, Exception):
                    raise outcome
                return JudgeVerdict(score=outcome, reasoning="r")
        raise AssertionError(prompt)

    return fn


def test_pass_fail_error_aggregation():
    agent = agent_fn(
        {
            "input a": "good",
            "input b": "bad",
            "input c": AgentCallError("jsonrpc-error", "code -32603"),
            "input d": "judge-broke",
        }
    )
    judge = judge_fn({"good": 0.9, "bad": 0.2, "judge-broke": JudgeError("no JSON")})
    result = run_cases(dataset("a", "b", "c", "d"), CHECKS, runner_settings(), agent, judge)

    assert result.status == "completed"
    assert [c.status for c in result.case_results] == ["passed", "failed", "error", "error"]
    assert (result.cases, result.passed, result.failed, result.errors) == (4, 1, 1, 2)
    # Errors count against the ratio: a JSON-RPC error never reads as a pass.
    assert result.pass_ratio == 0.25
    assert result.threshold_met is False
    assert "jsonrpc-error" in result.case_results[2].reason
    assert all(c.startswith("oap-harness-abc123-") for c in agent.seen)


def test_every_case_errored_is_a_run_error():
    # Live finding (campaign S1): with the judge failing every call the run was
    # published as completed with pass_ratio 0, so an outage read as a 0% revision.
    agent = agent_fn({"input a": "good", "input b": "bad"})
    judge = judge_fn({"good": JudgeError("judge returned HTTP 400"), "bad": JudgeError("judge returned HTTP 400")})
    result = run_cases(dataset("a", "b"), CHECKS, runner_settings(), agent, judge)
    assert result.status == "error"
    assert "every case errored (2 of 2)" in result.error
    assert "judge returned HTTP 400" in result.error
    assert result.pass_ratio is None
    assert result.threshold_met is None
    assert [c.status for c in result.case_results] == ["error", "error"]
    # It survives the ConfigMap round trip as an errored run.
    assert parse_result(to_configmap_data(result)).status == "error"


def test_threshold_met_when_ratio_reaches_it():
    agent = agent_fn({"input a": "good", "input b": "bad"})
    judge = judge_fn({"good": 0.9, "bad": 0.1})
    result = run_cases(dataset("a", "b"), CHECKS, runner_settings(threshold=0.5), agent, judge)
    assert result.pass_ratio == 0.5
    assert result.threshold_met is True


def test_zero_case_dataset_is_a_run_error(tmp_path):
    (tmp_path / "example-smoke-v1.yaml").write_text("name: example-smoke-v1\ncases: []\n")
    result = run(
        runner_settings(datasets_dir=tmp_path),
        CHECKS,
        agent_fn({}),
        judge_fn({}),
        reachable_fn=lambda: True,
    )
    assert result.status == "error"
    assert "no cases" in result.error
    assert result.pass_ratio is None
    assert result.threshold_met is None


def test_unreachable_preview_is_a_run_error(harness_dirs):
    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets),
        CHECKS,
        agent_fn({}),
        judge_fn({}),
        reachable_fn=lambda: False,
    )
    assert result.status == "error"
    assert "not reachable" in result.error
    assert result.pass_ratio is None


def test_unknown_check_is_a_run_error(harness_dirs):
    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets, checks=("tool-use",)),
        CHECKS,
        agent_fn({}),
        judge_fn({}),
        reachable_fn=lambda: True,
    )
    assert result.status == "error"
    assert result.pass_ratio is None


def test_end_to_end_with_the_mounted_files(harness_dirs):
    checks = load_checks(str(harness_dirs.checks))
    seen_prompts = []

    def judge(prompt):
        seen_prompts.append(prompt)
        return JudgeVerdict(0.8, "fine")

    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets),
        checks,
        lambda text, ctx: AgentReply("an answer", "completed"),
        judge,
        reachable_fn=lambda: True,
    )
    assert result.status == "completed"
    assert result.passed == 2
    assert "Expected: A current UTC time." in seen_prompts[0] or "Expected: A current UTC time." in seen_prompts[1]
    assert result.judge_key_present is True


def test_result_round_trips_through_the_configmap():
    agent = agent_fn({"input a": "good", "input b": "bad"})
    judge = judge_fn({"good": 0.9, "bad": 0.1})
    result = run_cases(dataset("a", "b"), CHECKS, runner_settings(), agent, judge)
    data = to_configmap_data(result)
    raw = json.loads(data["result.json"])
    assert raw["schema"] == RESULT_SCHEMA
    assert "passRatio" in raw and "expectToolsIgnored" in raw
    assert parse_result(data) == result


def test_parse_result_rejects_a_wrong_schema():
    agent = agent_fn({"input a": "good"})
    result = run_cases(dataset("a"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    raw = json.loads(to_configmap_data(result)["result.json"])
    raw["schema"] = "oap.harness.result/v0"
    with pytest.raises(ResultSchemaError):
        parse_result({"result.json": json.dumps(raw)})
    del raw["schema"]
    with pytest.raises(ResultSchemaError):
        parse_result({"result.json": json.dumps(raw)})
    raw["schema"] = RESULT_SCHEMA
    del raw["passed"]
    with pytest.raises(ResultSchemaError):
        parse_result({"result.json": json.dumps(raw)})
    with pytest.raises(ResultSchemaError):
        parse_result({})
