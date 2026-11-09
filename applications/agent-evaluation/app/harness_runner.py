"""Harness Job entrypoint: `python -m app.harness_runner`.

One Job runs one dataset against one agent revision through the agent's
`-preview` Service, judges each case, and writes one result ConfigMap in the
platform namespace. Report mode: the result is published as metrics and Events
and never affects promotion.

Exit codes: 0 once a result is written, whether the run completed or errored
(an errored run is a result, and the Job must not be retried: a retry would
drive the agent again). 1 only when the result cannot be written.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .a2a_client import AgentCallError, context_id_for, send_message, wait_until_reachable
from .config import ConfigError
from .datasets import Case, Dataset, DatasetError, load_dataset
from .harness_config import Check, RunnerSettings, load_checks, load_runner_settings
from .harness_results import (
    MAX_REASON_CHARS,
    CaseResult,
    RunResult,
    summarize,
    to_configmap_data,
)
from .judge import JudgeError, judge, render_prompt

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


def _run_case(case: Case, checks: list[Check], settings, agent_client_fn, judge_fn, clock):
    started = clock()
    try:
        reply = agent_client_fn(case.input, context_id_for(settings.revision, case.id))
    except AgentCallError as exc:
        return CaseResult(
            id=case.id,
            status="error",
            score=None,
            reason=f"agent {exc}"[:MAX_REASON_CHARS],
            latency_ms=int((clock() - started) * 1000),
            tags=case.tags,
        )
    latency_ms = int((clock() - started) * 1000)

    scores: list[float] = []
    reasons: list[str] = []
    failed = False
    for check in checks:
        prompt = render_prompt(check.prompt, case.input, case.expected, reply.text)
        try:
            verdict = judge_fn(prompt)
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
) -> RunResult:
    """Run every case with bounded concurrency and aggregate. Pure apart from the callables."""
    started_at = clock() if started_at is None else started_at
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
                    case, selected, settings, agent_client_fn, judge_fn, clock
                ),
                dataset.cases,
            )
        )
    return summarize(
        **_base(settings, judge_key_present, started_at),
        finished_at=clock(),
        case_results=case_results,
        expect_tools_ignored=dataset.uses_expect_tools,
    )


def run(
    settings: RunnerSettings,
    checks: dict[str, Check],
    agent_client_fn,
    judge_fn,
    reachable_fn,
    clock=time.time,
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

    if not reachable_fn():
        return error_result(
            settings,
            f"preview {settings.target_url} was not reachable within "
            f"{settings.ready_timeout}s",
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
                agent_client_fn=lambda text, context_id: send_message(
                    client, settings.target_url, text, context_id, settings.case_timeout
                ),
                judge_fn=lambda prompt: judge(
                    client,
                    settings.bifrost_url,
                    settings.judge_virtual_key,
                    settings.judge_model,
                    prompt,
                    settings.case_timeout,
                ),
                reachable_fn=lambda: wait_until_reachable(
                    client, settings.target_url, settings.ready_timeout
                ),
            )

    logger.info(
        "harness run %s/%s revision=%s dataset=%s status=%s cases=%d passed=%d "
        "failed=%d errors=%d pass_ratio=%s threshold=%s judge_key_present=%s%s",
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
