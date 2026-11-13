"""Harness Job entrypoint: `python -m app.harness_runner`.

One Job runs one dataset against one agent revision through the agent's
`-preview` Service, judges each case, and writes one result ConfigMap in the
platform namespace. Report mode: the result is published as metrics and Events
and never affects promotion.

Case classification (review B1): an agent reply that is not a usable answer
(failed Task, JSON-RPC error, error payload, HTTP 5xx, timeout) is a FAILED
case, score 0, because that is how the revision answered. A judge error after
retries, a transport error or a 4xx on the preview path is a case ERROR: it is
counted but kept out of the pass ratio. The judge is retried on transient
errors only.

Run deadline (review C4): the Job's activeDeadlineSeconds would kill the pod
with no result. The runner stops DEADLINE_MARGIN_SECONDS before it: every
agent and judge call timeout is clamped to the time left, a case not started
by then is an error "not run: run deadline reached", and the partial result is
written as a completed run over the judged cases, its `error` naming how many
cases did not run.

Exit codes: 0 once a result is written, whether the run completed or errored
(an errored run is a result, and the Job must not be retried: a retry would
drive the agent again). 1 only when the result cannot be written.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .a2a_client import AgentCallError, context_id_for, send_message, wait_until_reachable
from .config import ConfigError
from .datasets import Case, Dataset, DatasetError, load_dataset
from .harness_config import (
    Check,
    RunnerSettings,
    checks_digest,
    load_checks,
    load_runner_settings,
)
from .harness_results import (
    MAX_REASON_CHARS,
    CaseResult,
    RunResult,
    summarize,
    to_configmap_data,
)
from .judge import JudgeError, JudgeVerdict, judge, render_prompt

JUDGE_ATTEMPTS = 3
JUDGE_BACKOFF_SECONDS = (2, 4)
# Time kept back before the Job's deadline to judge the last call and write
# the result ConfigMap.
DEADLINE_MARGIN_SECONDS = 60
NOT_RUN_REASON = "not run: run deadline reached"

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logger = logging.getLogger("agent-evaluation.harness-runner")


def _base(settings: RunnerSettings, judge_key_present: bool, started_at: float) -> dict:
    return {
        "namespace": settings.agent_namespace,
        "agent": settings.agent,
        "revision": settings.revision,
        "dataset": settings.dataset,
        "checks": settings.checks,
        "threshold": settings.threshold,
        "mode": settings.mode,
        "trigger": settings.trigger,
        "judge_model": settings.judge_model,
        "judge_key_present": judge_key_present,
        "started_at": started_at,
    }


def error_result(
    settings: RunnerSettings,
    error: str,
    judge_key_present: bool,
    started_at: float,
    finished_at: float,
) -> RunResult:
    """A run that could not score the revision. Carries no pass ratio."""
    return summarize(
        **_base(settings, judge_key_present, started_at),
        finished_at=finished_at,
        status="error",
        error=error[:MAX_REASON_CHARS],
    )


def utc_now_iso(clock) -> str:
    """The `{{now}}` value: UTC, ISO 8601 to the second, `Z` suffix."""
    return (
        datetime.fromtimestamp(clock(), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def run_deadline(settings: RunnerSettings, started_at: float) -> float:
    return started_at + settings.run_timeout - DEADLINE_MARGIN_SECONDS


def _clamped(case_timeout: float, deadline: float | None, clock) -> tuple[float, bool]:
    """(timeout for one call, whether the run deadline shortened it)."""
    if deadline is None:
        return case_timeout, False
    left = deadline - clock()
    if left < case_timeout:
        return max(left, 0.0), True
    return case_timeout, False


def judge_with_retry(
    judge_fn,
    prompt: str,
    sleep,
    deadline: float | None,
    clock,
    timeout: float = 120,
) -> JudgeVerdict:
    """Call the judge up to JUDGE_ATTEMPTS times, backing off 2 s then 4 s.

    Only retryable errors (transport, 429, 5xx) are retried, and never past the
    run deadline: a retry that would start after it re-raises the last error.
    Each call's timeout is clamped to the time left before the deadline.
    """
    for attempt in range(JUDGE_ATTEMPTS):
        call_timeout, clamped = _clamped(timeout, deadline, clock)
        if clamped and call_timeout <= 0:
            raise JudgeError("run deadline reached before the judge call")
        try:
            return judge_fn(prompt, call_timeout)
        except JudgeError as exc:
            if not exc.retryable or attempt == JUDGE_ATTEMPTS - 1:
                raise
            backoff = JUDGE_BACKOFF_SECONDS[attempt]
            if deadline is not None and clock() + backoff >= deadline:
                if clamped:
                    raise JudgeError(f"run deadline: {exc}") from exc
                raise
            logger.info("judge attempt %d failed (%s); retrying in %ss", attempt + 1, exc, backoff)
            sleep(backoff)
    raise AssertionError("unreachable")  # pragma: no cover


def _run_case(
    case: Case,
    checks: list[Check],
    settings,
    agent_client_fn,
    judge_fn,
    clock,
    sleep=time.sleep,
    deadline: float | None = None,
):
    started = clock()
    if deadline is not None and started >= deadline:
        return CaseResult(
            id=case.id,
            status="error",
            score=None,
            reason=NOT_RUN_REASON,
            latency_ms=0,
            tags=case.tags,
        )
    timeout, clamped = _clamped(settings.case_timeout, deadline, clock)
    try:
        reply = agent_client_fn(
            case.input,
            context_id_for(settings.revision, case.id, settings.run_number),
            timeout,
        )
    except AgentCallError as exc:
        # The revision's own bad reply is a failed case; the path to it failing
        # says nothing about the revision and stays out of the ratio. A timeout
        # the run deadline shortened is not the agent's fault either.
        agent_side = exc.agent_side and not (clamped and exc.kind == "timeout")
        prefix = "run deadline: agent" if clamped and exc.kind == "timeout" else "agent"
        return CaseResult(
            id=case.id,
            status="failed" if agent_side else "error",
            score=0.0 if agent_side else None,
            reason=f"{prefix} {exc}"[:MAX_REASON_CHARS],
            latency_ms=int((clock() - started) * 1000),
            tags=case.tags,
        )
    latency_ms = int((clock() - started) * 1000)

    scores: list[float] = []
    reasons: list[str] = []
    failed = False
    for check in checks:
        # A fresh nonce per judge call: the data cannot know the marker that
        # closes its fence.
        # {{now}} is the time of this judge call, close to when the agent answered.
        prompt = render_prompt(
            check.prompt,
            case.input,
            case.expected,
            reply.text,
            secrets.token_hex(8),
            utc_now_iso(clock),
        )
        try:
            verdict = judge_with_retry(
                judge_fn, prompt, sleep, deadline, clock, settings.case_timeout
            )
        except JudgeError as exc:
            return CaseResult(
                id=case.id,
                status="error",
                score=None,
                reason=f"{check.name}: {exc}"[:MAX_REASON_CHARS],
                latency_ms=latency_ms,
                tags=case.tags,
            )
        scores.append(verdict.score)
        reasons.append(f"{check.name}: {verdict.reasoning}")
        if verdict.score < check.case_pass_score:
            failed = True
    return CaseResult(
        id=case.id,
        status="failed" if failed else "passed",
        score=min(scores),
        reason="; ".join(reasons)[:MAX_REASON_CHARS],
        latency_ms=latency_ms,
        tags=case.tags,
    )


def run_cases(
    dataset: Dataset,
    checks: dict[str, Check],
    settings: RunnerSettings,
    agent_client_fn,
    judge_fn,
    clock=time.time,
    judge_key_present: bool = False,
    started_at: float | None = None,
    sleep=time.sleep,
) -> RunResult:
    """Run every case with bounded concurrency and aggregate. Pure apart from the callables."""
    started_at = clock() if started_at is None else started_at
    deadline = run_deadline(settings, started_at)
    unknown = [name for name in settings.checks if name not in checks]
    if unknown or not settings.checks:
        return error_result(
            settings,
            f"unknown check(s) {unknown or '[]'}; the platform defines {sorted(checks)}",
            judge_key_present,
            started_at,
            clock(),
        )
    selected = [checks[name] for name in settings.checks]

    with ThreadPoolExecutor(max_workers=settings.case_concurrency) as pool:
        case_results = list(
            pool.map(
                lambda case: _run_case(
                    case, selected, settings, agent_client_fn, judge_fn, clock, sleep, deadline
                ),
                dataset.cases,
            )
        )
    not_run = sum(1 for c in case_results if c.reason == NOT_RUN_REASON)
    return summarize(
        **_base(settings, judge_key_present, started_at),
        finished_at=clock(),
        case_results=case_results,
        error=(
            f"run deadline reached: {not_run} of {len(case_results)} cases not run"
            if not_run
            else None
        ),
        expect_tools_ignored=dataset.uses_expect_tools,
        dataset_digest=dataset.digest,
        checks_digest=checks_digest(selected),
    )


def run(
    settings: RunnerSettings,
    checks: dict[str, Check],
    agent_client_fn,
    judge_fn,
    reachable_fn,
    clock=time.time,
    sleep=time.sleep,
) -> RunResult:
    """Load the dataset, wait for the preview, run the cases. Every failure is a result."""
    key_present = bool(settings.judge_virtual_key)
    started_at = clock()
    try:
        dataset = load_dataset(
            Path(settings.datasets_dir) / f"{settings.dataset}.yaml", settings.max_cases
        )
    except DatasetError as exc:
        return error_result(settings, f"dataset: {exc}", key_present, started_at, clock())

    unknown = [name for name in settings.checks if name not in checks]
    if unknown:
        return error_result(
            settings, f"unknown check(s) {unknown}", key_present, started_at, clock()
        )

    # The ready wait shares the run's deadline.
    ready_timeout, _ = _clamped(settings.ready_timeout, run_deadline(settings, started_at), clock)
    if not reachable_fn(ready_timeout):
        return error_result(
            settings,
            f"preview {settings.target_url} was not reachable within "
            f"{int(ready_timeout)}s",
            key_present,
            started_at,
            clock(),
        )
    return run_cases(
        dataset,
        checks,
        settings,
        agent_client_fn,
        judge_fn,
        clock=clock,
        judge_key_present=key_present,
        started_at=started_at,
        sleep=sleep,
    )


def _write_result(settings: RunnerSettings, result: RunResult) -> None:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    k8s_config.load_incluster_config()
    core = k8s_client.CoreV1Api()
    job = settings.result_configmap.removesuffix("-result")
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": settings.result_configmap,
            "namespace": settings.pod_namespace,
            "labels": {
                "harness.oap.io/result": "true",
                "harness.oap.io/job": job,
                "harness.oap.io/agent-namespace": settings.agent_namespace,
                "harness.oap.io/agent": settings.agent,
                "harness.oap.io/revision": settings.revision,
                "harness.oap.io/dataset": settings.dataset,
                "app.kubernetes.io/managed-by": "agent-evaluation-reconciler",
            },
        },
        "data": to_configmap_data(result),
    }
    try:
        core.create_namespaced_config_map(namespace=settings.pod_namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core.replace_namespaced_config_map(
            name=settings.result_configmap, namespace=settings.pod_namespace, body=body
        )


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        stream=sys.stdout,
    )
    try:
        settings = load_runner_settings()
    except ConfigError as exc:
        # Without settings there is no result ConfigMap name to write to.
        logger.error("configuration error: %s", exc)
        return 1

    started_at = time.time()
    with httpx.Client(follow_redirects=False) as client:
        try:
            checks = load_checks(settings.checks_path)
        except ConfigError as exc:
            result = error_result(
                settings,
                f"checks: {exc}",
                bool(settings.judge_virtual_key),
                started_at,
                time.time(),
            )
        else:
            result = run(
                settings,
                checks,
                # Timeouts come from the runner, clamped to the run deadline.
                agent_client_fn=lambda text, context_id, timeout: send_message(
                    client, settings.target_url, text, context_id, timeout
                ),
                judge_fn=lambda prompt, timeout: judge(
                    client,
                    settings.bifrost_url,
                    settings.judge_virtual_key,
                    settings.judge_model,
                    prompt,
                    timeout,
                ),
                reachable_fn=lambda timeout: wait_until_reachable(
                    client, settings.target_url, timeout
                ),
            )

    logger.info(
        "harness run %s/%s revision=%s dataset=%s status=%s cases=%d passed=%d "
        "failed=%d errors=%d pass_ratio=%s threshold=%s judge_key_present=%s "
        "expect_tools_ignored=%s%s",
        settings.agent_namespace,
        settings.agent,
        settings.revision,
        settings.dataset,
        result.status,
        result.cases,
        result.passed,
        result.failed,
        result.errors,
        "n/a" if result.pass_ratio is None else f"{result.pass_ratio:.3f}",
        settings.threshold,
        result.judge_key_present,
        result.expect_tools_ignored,
        f" error={result.error}" if result.error else "",
    )
    try:
        _write_result(settings, result)
    except Exception as exc:  # noqa: BLE001 - any failure to write is exit 1
        logger.error("cannot write result ConfigMap %s: %s", settings.result_configmap, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
