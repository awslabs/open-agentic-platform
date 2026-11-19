"""The Job's run logic: aggregation, run-level errors and the result contract."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.a2a_client import AgentCallError, AgentReply
from app.datasets import Case, Dataset
from app.harness_config import Check, RunnerSettings, checks_digest, load_checks
from app.harness_results import (
    RESULT_KEY,
    RESULT_SCHEMA,
    CaseResult,
    ResultSchemaError,
    parse_result,
    summarize,
    to_configmap_data,
)
from app.harness_runner import judge_with_retry, result_body, run, run_cases
from app.judge import JudgeError, JudgeVerdict, judge

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
        # Older tests have errors among few cases; the floor has its own tests.
        min_judged_ratio=0.5,
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

    def fn(text, context_id, timeout):
        seen.append(context_id)
        outcome = behaviour[text]
        if isinstance(outcome, Exception):
            raise outcome
        return AgentReply(text=outcome, raw_state="completed")

    fn.seen = seen
    return fn


def judge_fn(scores: dict):
    """scores: reply text appearing in the prompt -> score, or a JudgeError."""

    def fn(prompt, timeout):
        for text, outcome in scores.items():
            if f"O={text}" in prompt:
                if isinstance(outcome, Exception):
                    raise outcome
                return JudgeVerdict(score=outcome, reasoning="r")
        raise AssertionError(prompt)

    return fn


def no_sleep(_seconds):
    pass


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
    assert [c.status for c in result.case_results] == ["passed", "failed", "failed", "error"]
    assert (result.cases, result.passed, result.failed, result.errors) == (4, 1, 2, 1)
    # The agent's JSON-RPC error is a failed case; the judge error is outside
    # the denominator: 1 passed of 3 judged.
    assert result.pass_ratio == 1 / 3
    assert result.threshold_met is False
    assert "jsonrpc-error" in result.case_results[2].reason
    assert result.case_results[2].score == 0.0
    assert all(c.startswith("oap-harness-0123456789ab-") for c in agent.seen)


TASK_FAILED = AgentCallError("task-not-completed", "task state 'failed'")


def test_agent_failing_every_case_is_a_zero_ratio_not_an_outage():
    # Review B1 repro: an agent whose Task fails on every case is a 0% revision.
    agent = agent_fn({f"input {i}": TASK_FAILED for i in "abcd"})
    result = run_cases(dataset(*"abcd"), CHECKS, runner_settings(), agent, judge_fn({}))
    assert result.status == "completed"
    assert (result.passed, result.failed, result.errors) == (0, 4, 0)
    assert result.pass_ratio == 0.0
    assert result.threshold_met is False


def test_agent_failing_three_of_four_cases():
    agent = agent_fn(
        {"input a": "good", "input b": TASK_FAILED, "input c": TASK_FAILED, "input d": TASK_FAILED}
    )
    result = run_cases(dataset(*"abcd"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    assert (result.passed, result.failed, result.errors) == (1, 3, 0)
    assert result.pass_ratio == 0.25


@pytest.mark.parametrize(
    "exc,status",
    [
        (AgentCallError("http", "HTTP 503", agent_side=True), "failed"),
        (AgentCallError("timeout", "ReadTimeout", agent_side=True), "failed"),
        (AgentCallError("transport", "ConnectError", agent_side=False), "error"),
    ],
)
def test_agent_side_errors_fail_and_infra_errors_are_errors(exc, status):
    agent = agent_fn({"input a": exc, "input b": "good"})
    result = run_cases(dataset("a", "b"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    assert result.case_results[0].status == status
    assert result.pass_ratio == (0.5 if status == "failed" else 1.0)


def test_404_on_every_case_is_a_zero_ratio_not_an_outage():
    # Review round 2 blocking 2, through the real client: status was "error".
    ids = "abcd"
    with respx.mock:
        respx.post("http://preview.test/").mock(return_value=httpx.Response(404))
        with httpx.Client() as client:
            from app.a2a_client import send_message

            def agent(text, context_id, timeout):
                return send_message(client, "http://preview.test/", text, context_id, timeout)

            result = run_cases(
                dataset(*ids), CHECKS, runner_settings(), agent, judge_fn({}), sleep=no_sleep
            )
    assert result.status == "completed"
    assert (result.passed, result.failed, result.errors) == (0, 4, 0)
    assert result.pass_ratio == 0.0


def test_crashes_count_against_the_ratio():
    # Review round 2 blocking 2: 10 crashes + 5 of 10 correct gave 0.5, not 0.25.
    ids = [f"c{i}" for i in range(20)]
    dropped = AgentCallError("dropped", "ReadError", agent_side=True)
    behaviour = {}
    for n, i in enumerate(ids):
        behaviour[f"input {i}"] = dropped if n < 10 else ("RIGHT" if n < 15 else "WRONG")

    result = run_cases(
        dataset(*ids),
        CHECKS,
        runner_settings(),
        agent_fn(behaviour),
        lambda prompt, timeout: JudgeVerdict(1.0 if "O=RIGHT" in prompt else 0.0, "r"),
    )
    assert (result.passed, result.failed, result.errors) == (5, 15, 0)
    assert result.pass_ratio == 0.25


def flaky_judge(failures: dict, error=lambda: JudgeError("judge returned HTTP 429", retryable=True)):
    """failures: reply text -> how many calls fail before the judge answers 1.0."""
    calls = {}

    def fn(prompt, timeout):
        for text, count in failures.items():
            if f"O={text}" in prompt:
                calls[text] = calls.get(text, 0) + 1
                if calls[text] <= count:
                    raise error()
                return JudgeVerdict(1.0, "ok")
        raise AssertionError(prompt)

    fn.calls = calls
    return fn


def test_one_judge_429_is_retried():
    # Review B1 repro: 4 correct answers, one judge 429 then 200 -> ratio 1.0.
    agent = agent_fn({f"input {i}": f"right {i}" for i in "abcd"})
    judge = flaky_judge({"right a": 1, "right b": 0, "right c": 0, "right d": 0})
    slept = []
    result = run_cases(
        dataset(*"abcd"), CHECKS, runner_settings(), agent, judge, sleep=slept.append
    )
    assert (result.passed, result.failed, result.errors) == (4, 0, 0)
    assert result.pass_ratio == 1.0
    assert slept == [2]
    assert judge.calls["right a"] == 2


def test_persistent_judge_error_is_excluded_from_the_ratio():
    agent = agent_fn({f"input {i}": f"right {i}" for i in "abcd"})
    judge = flaky_judge({"right a": 99, "right b": 0, "right c": 0, "right d": 0})
    slept = []
    result = run_cases(
        dataset(*"abcd"), CHECKS, runner_settings(), agent, judge, sleep=slept.append
    )
    assert (result.passed, result.failed, result.errors) == (3, 0, 1)
    assert result.pass_ratio == 1.0
    assert judge.calls["right a"] == 3
    assert slept == [2, 4]


def test_one_judged_case_among_judge_errors_is_insufficient_not_a_full_pass():
    # Review round 2 blocking 1: 1 passed + 19 judge errors read as 100%.
    ids = [f"c{i}" for i in range(20)]
    agent = agent_fn({f"input {i}": f"right {i}" for i in ids})
    judge = flaky_judge({f"right {i}": (0 if i == "c0" else 99) for i in ids})
    result = run_cases(
        dataset(*ids),
        CHECKS,
        runner_settings(min_judged_ratio=0.8),
        agent,
        judge,
        sleep=no_sleep,
    )
    assert result.status == "insufficient"
    assert (result.cases, result.passed, result.failed, result.errors) == (20, 1, 0, 19)
    assert result.pass_ratio is None
    assert result.threshold_met is None
    assert "1 of 20 cases judged" in result.error
    assert parse_result(to_configmap_data(result)) == result


def test_deadline_cut_run_with_two_of_fifty_judged_is_insufficient():
    now = [0.0]
    ids = [f"c{i}" for i in range(50)]

    def agent(text, context_id, timeout):
        now[0] += 150
        return AgentReply("good", "completed")

    settings = runner_settings(
        case_concurrency=1, case_timeout=120, run_timeout=400, min_judged_ratio=0.8
    )
    result = run_cases(
        dataset(*ids),
        CHECKS,
        settings,
        agent,
        judge_fn({"good": 1.0}),
        clock=lambda: now[0],
        started_at=0.0,
        sleep=no_sleep,
    )
    assert result.status == "insufficient"
    assert result.passed == 2 and result.cases == 50
    assert result.pass_ratio is None and result.threshold_met is None
    assert "run deadline reached" in result.error


def test_judged_cases_exactly_at_the_floor_publish_a_ratio():
    # 4 of 5 judged is 80%: the floor is reached, so the ratio is published.
    cases = [
        CaseResult(id=i, status=s, score=None, reason="r", latency_ms=1, tags=())
        for i, s in zip("abcde", ["passed", "passed", "failed", "passed", "error"])
    ]
    result = summarize(
        **_summary_args(), case_results=cases, min_judged_ratio=0.8
    )
    assert result.status == "completed"
    assert result.pass_ratio == 0.75


def test_every_judge_call_failing_is_a_run_error():
    agent = agent_fn({f"input {i}": f"right {i}" for i in "ab"})
    judge = flaky_judge({"right a": 99, "right b": 99})
    result = run_cases(dataset("a", "b"), CHECKS, runner_settings(), agent, judge, sleep=no_sleep)
    assert result.status == "error"
    assert result.pass_ratio is None
    assert result.errors == 2


def test_non_retryable_judge_error_is_not_retried():
    calls = []

    def judge(prompt, timeout):
        calls.append(prompt)
        raise JudgeError("judge returned HTTP 400", retryable=False)

    with pytest.raises(JudgeError):
        judge_with_retry(judge, "p", sleep=no_sleep, deadline=None, clock=lambda: 0.0)
    assert len(calls) == 1


def test_judge_retry_never_sleeps_past_the_deadline():
    calls = []

    def judge(prompt, timeout):
        calls.append(prompt)
        raise JudgeError("judge returned HTTP 503", retryable=True)

    with pytest.raises(JudgeError):
        judge_with_retry(judge, "p", sleep=no_sleep, deadline=1.0, clock=lambda: 0.0)
    assert len(calls) == 1


@respx.mock
@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (401, False)])
def test_judge_http_errors_say_whether_they_are_retryable(status, retryable):
    url = "http://bifrost.example/v1"
    respx.post(f"{url}/chat/completions").mock(return_value=httpx.Response(status, json={}))
    with httpx.Client() as client, pytest.raises(JudgeError) as exc:
        judge(client, url, "", "m", "p", 5)
    assert exc.value.retryable is retryable


def test_cases_send_job_name_context_ids():
    agent = agent_fn({"input a": "good", "input b": "good"})
    run_cases(
        dataset("a", "b"),
        CHECKS,
        runner_settings(result_configmap="oap-harness-fedcba987654-result"),
        agent,
        judge_fn({"good": 0.9}),
    )
    assert sorted(agent.seen) == ["oap-harness-fedcba987654-a", "oap-harness-fedcba987654-b"]


def test_runner_settings_job_name_drops_the_result_suffix():
    assert runner_settings().job_name == "oap-harness-0123456789ab"


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


def test_now_time_case_passes_the_right_time_and_fails_an_invented_one():
    # Review B2a: the reference carries {{now}}, rendered from the runner clock
    # at judge time, so a correct time passes and an invented one fails.
    clock = lambda: 1_767_355_200.0  # 2026-01-02T12:00:00Z  # noqa: E731
    checks = {"expected-answer": Check("expected-answer", "E={{expected}} O={{output}}", 0.7)}
    cases = (
        Case("right", "time?", "The current UTC time is {{now}}.", None, ("time",)),
        Case("invented", "time? ", "The current UTC time is {{now}}.", None, ("time",)),
        Case("literal", "time?  ", "The current UTC time is {{now}}.", None, ("time",)),
    )
    replies = {
        "time?": "It is 2026-01-02T12:00:00Z.",
        "time? ": "It is 03:17 UTC.",
        "time?  ": "{{now}}",
    }
    prompts = []

    def judge(prompt, timeout):
        prompts.append(prompt)
        reference, answer = prompt.split(" O=", 1)
        ok = "2026-01-02T12:00:00Z" in reference and "2026-01-02T12:00:00Z" in answer
        return JudgeVerdict(1.0 if ok else 0.0, "r")

    result = run_cases(
        Dataset("t", "", cases),
        checks,
        runner_settings(case_concurrency=1),
        lambda text, ctx, timeout: AgentReply(replies[text], "completed"),
        judge,
        clock=clock,
    )
    assert [c.status for c in result.case_results] == ["passed", "failed", "failed"]
    # The agent's literal {{now}} was not substituted.
    assert prompts[2].endswith("O={{now}}")


def test_run_deadline_stops_the_run_and_writes_a_partial_result():
    # Review C4: the Job's activeDeadlineSeconds would kill the pod with no
    # result. With run_timeout 400 the runner stops at 400 - 60 = 340 s.
    now = [0.0]
    timeouts = []

    def agent(text, context_id, timeout):
        timeouts.append(timeout)
        if timeout < 100:
            # The clamped call runs out: the agent was not given its full time.
            now[0] += timeout
            raise AgentCallError("timeout", "ReadTimeout", agent_side=True)
        now[0] += 100
        return AgentReply("good", "completed")

    settings = runner_settings(case_concurrency=1, case_timeout=120, run_timeout=400)
    result = run_cases(
        dataset(*"abcdef"),
        CHECKS,
        settings,
        agent,
        judge_fn({"good": 1.0}),
        clock=lambda: now[0],
        started_at=0.0,
        sleep=no_sleep,
    )
    assert result.status == "completed"
    assert [c.status for c in result.case_results] == [
        "passed", "passed", "passed", "error", "error", "error"
    ]
    # The fourth call got only the 40 s left, and its timeout is not the agent's fault.
    assert timeouts == [120, 120, 120, 40]
    assert result.case_results[3].reason.startswith("run deadline: agent timeout")
    assert result.case_results[4].reason == "not run: run deadline reached"
    assert result.pass_ratio == 1.0
    assert result.error == "run deadline reached: 2 of 6 cases not run"
    assert parse_result(to_configmap_data(result)) == result


def test_ready_wait_is_clamped_to_the_run_deadline(harness_dirs):
    waited = []
    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets, ready_timeout=600, run_timeout=300),
        CHECKS,
        agent_fn({}),
        judge_fn({}),
        reachable_fn=lambda timeout: waited.append(timeout) or False,
        clock=lambda: 0.0,
    )
    assert waited == [240]
    assert result.status == "error"


def _summary_args():
    return dict(
        namespace="team-a", agent="assistant", revision="r", dataset="d", checks=("c",),
        threshold=0.5, mode="report", trigger="t", judge_model="m",
        judge_key_present=True, started_at=0.0, finished_at=1.0,
    )


def test_min_judged_ratio_is_read_and_validated(monkeypatch):
    from app.harness_config import ConfigError, load_runner_settings

    monkeypatch.setenv("HARNESS_TARGET_URL", "http://x/")
    for k, v in dict(
        HARNESS_AGENT_NAMESPACE="n", HARNESS_AGENT="a", HARNESS_REVISION="r",
        HARNESS_DATASET="d", HARNESS_RESULT_CONFIGMAP="oap-harness-x-result",
        POD_NAMESPACE="p", HARNESS_JUDGE_MODEL="m", CREDENTIALS_DIR="/nonexistent",
    ).items():
        monkeypatch.setenv(k, v)
    assert load_runner_settings().min_judged_ratio == 0.8
    monkeypatch.setenv("HARNESS_MIN_JUDGED_RATIO", "0.5")
    assert load_runner_settings().min_judged_ratio == 0.5
    monkeypatch.setenv("HARNESS_MIN_JUDGED_RATIO", "0")
    with pytest.raises(ConfigError):
        load_runner_settings()


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
        reachable_fn=lambda timeout: True,
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
        reachable_fn=lambda timeout: False,
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
        reachable_fn=lambda timeout: True,
    )
    assert result.status == "error"
    assert result.pass_ratio is None


def test_end_to_end_with_the_mounted_files(harness_dirs):
    checks = load_checks(str(harness_dirs.checks))
    seen_prompts = []

    def judge(prompt, timeout):
        seen_prompts.append(prompt)
        return JudgeVerdict(0.8, "fine")

    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets),
        checks,
        lambda text, ctx, timeout: AgentReply("an answer", "completed"),
        judge,
        reachable_fn=lambda timeout: True,
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


def test_result_configmap_is_owned_by_the_job_uid():
    agent = agent_fn({"input a": "good"})
    result = run_cases(dataset("a"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    body = result_body(runner_settings(job_uid="1234-abcd"), result)
    assert body["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": "oap-harness-0123456789ab",
            "uid": "1234-abcd",
        }
    ]
    # Without a uid there is nothing to own it: the reconciler ignores it.
    assert "ownerReferences" not in result_body(runner_settings(), result)["metadata"]


def test_write_result_never_replaces_an_existing_result(monkeypatch):
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    from app import harness_runner

    calls = []

    class Core:
        def create_namespaced_config_map(self, namespace, body):
            calls.append("create")
            raise ApiException(status=409, reason="AlreadyExists")

        def replace_namespaced_config_map(self, *args, **kwargs):  # pragma: no cover
            calls.append("replace")

    monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(k8s_client, "CoreV1Api", Core)
    agent = agent_fn({"input a": "good"})
    result = run_cases(dataset("a"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    with pytest.raises(ApiException):
        harness_runner._write_result(runner_settings(job_uid="u1"), result)
    assert calls == ["create"]


def test_runner_settings_read_the_job_uid(monkeypatch, tmp_path):
    from app.harness_config import load_runner_settings

    for name, value in {
        "HARNESS_TARGET_URL": "http://assistant-preview.team-a.svc.cluster.local:8083",
        "HARNESS_AGENT_NAMESPACE": "team-a",
        "HARNESS_AGENT": "assistant",
        "HARNESS_REVISION": "abc123",
        "HARNESS_DATASET": "example-smoke-v1",
        "HARNESS_RESULT_CONFIGMAP": "oap-harness-0123456789ab-result",
        "POD_NAMESPACE": "agent-evaluation",
        "HARNESS_JUDGE_MODEL": "m",
        "CREDENTIALS_DIR": str(tmp_path),
        "HARNESS_JOB_UID": "1234-abcd",
    }.items():
        monkeypatch.setenv(name, value)
    assert load_runner_settings().job_uid == "1234-abcd"
    monkeypatch.delenv("HARNESS_JOB_UID")
    assert load_runner_settings().job_uid == ""


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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["caseResults"][0].update(tags=5),
        lambda raw: raw["caseResults"][0].update(latencyMs="x"),
        lambda raw: raw.update(checks=7),
    ],
    ids=["tags-int", "latency-str", "checks-int"],
)
def test_parse_result_reports_wrong_typed_fields_as_a_schema_error(mutate):
    # Review S1: a TypeError/ValueError from a conversion is a result not
    # understood, never an exception that escapes the reconciler pass.
    agent = agent_fn({"input a": "good"})
    result = run_cases(dataset("a"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    raw = json.loads(to_configmap_data(result)["result.json"])
    mutate(raw)
    with pytest.raises(ResultSchemaError):
        parse_result({"result.json": json.dumps(raw)})


def test_run_records_the_dataset_and_checks_digests(harness_dirs):
    result = run(
        runner_settings(datasets_dir=harness_dirs.datasets),
        CHECKS,
        agent_fn({"What time is it in UTC?": "good", "Define latency in one sentence.": "good"}),
        judge_fn({"good": 1.0}),
        reachable_fn=lambda timeout: True,
    )
    assert result.status == "completed"
    assert len(result.dataset_digest) == 12
    assert result.checks_digest == checks_digest([CHECKS["expected-answer"]])
    raw = json.loads(to_configmap_data(result)["result.json"])
    assert raw["datasetDigest"] == result.dataset_digest
    assert raw["checksDigest"] == result.checks_digest
    assert parse_result(to_configmap_data(result)) == result


def test_checks_digest_follows_prompt_and_pass_score():
    base = checks_digest([Check("expected-answer", "p", 0.7)])
    assert base == checks_digest([Check("expected-answer", "p", 0.7)])
    assert base != checks_digest([Check("expected-answer", "p!", 0.7)])
    assert base != checks_digest([Check("expected-answer", "p", 0.8)])


def test_a_result_without_digests_still_parses():
    agent = agent_fn({"input a": "good"})
    result = run_cases(dataset("a"), CHECKS, runner_settings(), agent, judge_fn({"good": 1.0}))
    raw = json.loads(to_configmap_data(result)["result.json"])
    del raw["datasetDigest"], raw["checksDigest"]
    parsed = parse_result({"result.json": json.dumps(raw)})
    assert (parsed.dataset_digest, parsed.checks_digest) == ("", "")
def test_run_deadline_uses_the_absolute_deadline_from_the_reconciler():
    # Review round 2: the Job's clock started 90 s before the runner did
    # (scheduling, image pull), so the runner's 400 s count would overrun it.
    from app.harness_runner import run_deadline
    late = runner_settings(run_timeout=400, run_deadline_epoch=1_400.0)
    assert run_deadline(late, started_at=1_090.0) == 1_340.0
    assert run_deadline(runner_settings(run_timeout=400), started_at=1_090.0) == 1_430.0
def test_runner_stops_at_the_absolute_deadline_not_its_own_start():
    now = [1_090.0]  # the runner started 90 s after the Job
    def agent(text, context_id, timeout):
        now[0] += 100
        return AgentReply("good", "completed")
    settings = runner_settings(
        case_concurrency=1, case_timeout=120, run_timeout=400, run_deadline_epoch=1_400.0
    )
    result = run_cases(
        dataset(*"abcdef"), CHECKS, settings, agent, judge_fn({"good": 1.0}),
        clock=lambda: now[0], started_at=1_090.0, sleep=no_sleep,
    )
    # Deadline 1340: calls end at 1190, 1290, then the third gets 50 s and the rest do not run.
    assert [c.status for c in result.case_results].count("passed") == 2
    assert result.error and "not run" in result.error
@pytest.mark.parametrize("key", ["dataset", "agent", "revision", "namespace", "trigger"])
def test_parse_result_rejects_a_non_string_string_field(key):
    # Review round 2: "dataset": ["x"] raised TypeError in _publish on every pass
    # and _prune never ran. It is now a result not understood (a run error).
    good = to_configmap_data(run_cases(dataset("a"), CHECKS, runner_settings(), agent_fn({"input a": "good"}), judge_fn({"good": 1.0})))
    raw = json.loads(good[RESULT_KEY])
    raw[key] = ["x"]
    with pytest.raises(ResultSchemaError, match="not a string"):
        parse_result({RESULT_KEY: json.dumps(raw)})
@pytest.mark.parametrize(
    "run_timeout,case_timeout,ok",
    [("60", "120", False), ("180", "120", False), ("181", "120", True), ("1800", "120", True)],
)
def test_run_timeout_must_exceed_the_margin_plus_a_case_timeout(
    monkeypatch, run_timeout, case_timeout, ok
):
    # Review round 2: a runTimeoutSeconds of 60 or less errored every case.
    from app.harness_config import ConfigError, load_harness_settings
    monkeypatch.setenv("HARNESS_ENABLED", "true")
    monkeypatch.setenv("HARNESS_JOB_IMAGE", "img")
    monkeypatch.setenv("HARNESS_RUN_TIMEOUT_SECONDS", run_timeout)
    monkeypatch.setenv("HARNESS_CASE_TIMEOUT_SECONDS", case_timeout)
    if ok:
        assert load_harness_settings().run_timeout == int(run_timeout)
    else:
        with pytest.raises(ConfigError, match="HARNESS_RUN_TIMEOUT_SECONDS"):
            load_harness_settings()
