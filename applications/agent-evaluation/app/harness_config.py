"""Configuration for the agent harness (issue #137, report mode).

Kept apart from `config.Settings` on purpose: Settings is a frozen dataclass
with no defaults that the tests build field by field, and with the harness off
(the default) none of this is read. Two shapes live here:

- `HarnessSettings`: the reconciler side. Read only when HARNESS_ENABLED=true.
- `RunnerSettings`: the Job side (`python -m app.harness_runner`). The Job
  talks to the agent and to Bifrost only, so it must not require any LANGFUSE_*
  variable.

The `expected-answer` check prompt arrives as a mounted YAML file rendered by
the chart (`checks.yaml`), the same way the evaluator catalog does. It is never
published to Langfuse.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ConfigError, _env, _env_int, _read_secret_file

# Mount points inside the Job pod. Two SIBLING directories on purpose: a volume
# mounted inside a ConfigMap volume's directory is not supported cleanly.
JOB_CHECKS_DIR = "/etc/agent-evaluation/harness-checks"
JOB_CHECKS_FILE = "checks.yaml"
JOB_DATASETS_DIR = "/etc/agent-evaluation/harness-datasets"
JOB_CREDENTIALS_DIR = "/etc/agent-evaluation/credentials"
JUDGE_KEY_FILE = "BIFROST_VIRTUAL_KEY"

# The only check the first cut implements. The CUE `#HarnessCheck` accepts the
# same single value, so anything else is rejected at admission already.
KNOWN_CHECKS = ("expected-answer",)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigError(f"{name} must be 'true' or 'false', got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _positive(name: str, value: int) -> int:
    if value < 1:
        raise ConfigError(f"{name} must be >= 1, got {value}")
    return value


@dataclass(frozen=True)
class Check:
    """One harness check: a judge prompt and the score a case needs to pass."""

    name: str
    prompt: str
    case_pass_score: float


@dataclass(frozen=True)
class HarnessSettings:
    enabled: bool = False
    interval: int = 60
    max_concurrent_jobs: int = 2
    history_limit: int = 3
    checks_path: str = f"{JOB_CHECKS_DIR}/{JOB_CHECKS_FILE}"
    datasets_dir: str = JOB_DATASETS_DIR
    catalog_configmap: str = "agent-evaluation-harness-catalog"
    datasets_configmap: str = "agent-evaluation-harness-datasets"
    job_image: str = ""
    job_image_pull_policy: str = "IfNotPresent"
    runner_service_account: str = "agent-evaluation-harness-runner"
    runner_resources: dict = field(default_factory=dict)
    case_concurrency: int = 2
    case_timeout: int = 120
    ready_timeout: int = 600
    run_timeout: int = 1800
    max_cases: int = 50
    # '' means the catalog's judge default alias.
    judge_alias: str = ""
    judge_key_mint: bool = True
    judge_key_required: bool = True
    judge_key_secret: str = "agent-evaluation-harness-judge-key"
    bifrost_admin_url: str = "http://bifrost.bifrost.svc.cluster.local:8080"
    judge_budget_usd: float = 20.0
    judge_budget_reset: str = "1M"
    judge_rate_limit_requests: int = 2000
    judge_rate_limit_reset: str = "1h"
    # Empty means [the resolved judge model].
    judge_allowed_models: tuple[str, ...] = ()


def load_harness_settings() -> HarnessSettings:
    """Read the reconciler-side harness settings. Disabled means nothing else is read."""
    enabled = _env_bool("HARNESS_ENABLED", False)
    if not enabled:
        return HarnessSettings(enabled=False)

    job_image = _env("HARNESS_JOB_IMAGE")
    if not job_image:
        raise ConfigError(
            "HARNESS_JOB_IMAGE is required when HARNESS_ENABLED=true: the harness "
            "Job runs the reconciler image with `python -m app.harness_runner`"
        )

    raw_resources = _env("HARNESS_RUNNER_RESOURCES", "{}")
    try:
        resources = json.loads(raw_resources)
    except ValueError as exc:
        raise ConfigError(
            f"HARNESS_RUNNER_RESOURCES must be a JSON object, got {raw_resources!r}"
        ) from exc
    if not isinstance(resources, dict):
        raise ConfigError(
            f"HARNESS_RUNNER_RESOURCES must be a JSON object, got {raw_resources!r}"
        )

    budget = _env_float("HARNESS_JUDGE_BUDGET_USD", 20.0)
    if budget <= 0:
        raise ConfigError(f"HARNESS_JUDGE_BUDGET_USD must be > 0, got {budget}")

    return HarnessSettings(
        enabled=True,
        interval=_positive("HARNESS_INTERVAL_SECONDS", _env_int("HARNESS_INTERVAL_SECONDS", 60)),
        max_concurrent_jobs=_positive(
            "HARNESS_MAX_CONCURRENT_JOBS", _env_int("HARNESS_MAX_CONCURRENT_JOBS", 2)
        ),
        history_limit=_positive(
            "HARNESS_HISTORY_LIMIT", _env_int("HARNESS_HISTORY_LIMIT", 3)
        ),
        checks_path=_env("HARNESS_CHECKS_PATH", f"{JOB_CHECKS_DIR}/{JOB_CHECKS_FILE}"),
        datasets_dir=_env("HARNESS_DATASETS_DIR", JOB_DATASETS_DIR),
        catalog_configmap=_env(
            "HARNESS_CATALOG_CONFIGMAP", "agent-evaluation-harness-catalog"
        ),
        datasets_configmap=_env(
            "HARNESS_DATASETS_CONFIGMAP", "agent-evaluation-harness-datasets"
        ),
        job_image=job_image,
        job_image_pull_policy=_env("HARNESS_JOB_IMAGE_PULL_POLICY", "IfNotPresent"),
        runner_service_account=_env(
            "HARNESS_RUNNER_SERVICE_ACCOUNT", "agent-evaluation-harness-runner"
        ),
        runner_resources=resources,
        case_concurrency=_positive(
            "HARNESS_CASE_CONCURRENCY", _env_int("HARNESS_CASE_CONCURRENCY", 2)
        ),
        case_timeout=_positive(
            "HARNESS_CASE_TIMEOUT_SECONDS", _env_int("HARNESS_CASE_TIMEOUT_SECONDS", 120)
        ),
        ready_timeout=_positive(
            "HARNESS_READY_TIMEOUT_SECONDS", _env_int("HARNESS_READY_TIMEOUT_SECONDS", 600)
        ),
        run_timeout=_positive(
            "HARNESS_RUN_TIMEOUT_SECONDS", _env_int("HARNESS_RUN_TIMEOUT_SECONDS", 1800)
        ),
        max_cases=_positive("HARNESS_MAX_CASES", _env_int("HARNESS_MAX_CASES", 50)),
        judge_alias=_env("HARNESS_JUDGE_ALIAS"),
        judge_key_mint=_env_bool("HARNESS_JUDGE_KEY_MINT", True),
        judge_key_required=_env_bool("HARNESS_JUDGE_KEY_REQUIRED", True),
        judge_key_secret=_env(
            "HARNESS_JUDGE_KEY_SECRET", "agent-evaluation-harness-judge-key"
        ),
        bifrost_admin_url=_env(
            "BIFROST_ADMIN_URL", "http://bifrost.bifrost.svc.cluster.local:8080"
        ).rstrip("/"),
        judge_budget_usd=budget,
        judge_budget_reset=_env("HARNESS_JUDGE_BUDGET_RESET", "1M"),
        judge_rate_limit_requests=_positive(
            "HARNESS_JUDGE_RATE_LIMIT_REQUESTS",
            _env_int("HARNESS_JUDGE_RATE_LIMIT_REQUESTS", 2000),
        ),
        judge_rate_limit_reset=_env("HARNESS_JUDGE_RATE_LIMIT_RESET", "1h"),
        judge_allowed_models=tuple(
            m.strip()
            for m in _env("HARNESS_JUDGE_ALLOWED_MODELS").split(",")
            if m.strip()
        ),
    )


@dataclass(frozen=True)
class RunnerSettings:
    """What one harness Job needs. Built by the reconciler as the Job's env."""

    target_url: str
    agent_namespace: str
    agent: str
    revision: str
    dataset: str
    checks: tuple[str, ...]
    threshold: float
    mode: str
    trigger: str
    result_configmap: str
    pod_namespace: str
    cluster: str
    bifrost_url: str
    judge_model: str
    case_concurrency: int
    case_timeout: int
    ready_timeout: int
    max_cases: int
    checks_path: str
    datasets_dir: str
    credentials_dir: str
    # Never printed: repr=False keeps it out of any accidental log of settings.
    judge_virtual_key: str = field(default="", repr=False)


def load_runner_settings() -> RunnerSettings:
    raw_checks = _env("HARNESS_CHECKS", '["expected-answer"]')
    try:
        checks = json.loads(raw_checks)
    except ValueError as exc:
        raise ConfigError(f"HARNESS_CHECKS must be a JSON list, got {raw_checks!r}") from exc
    if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
        raise ConfigError(f"HARNESS_CHECKS must be a JSON list of names, got {raw_checks!r}")

    target_url = _env("HARNESS_TARGET_URL", required=True)
    if not target_url.endswith("/"):
        target_url += "/"

    credentials_dir = _env("CREDENTIALS_DIR", JOB_CREDENTIALS_DIR)
    return RunnerSettings(
        target_url=target_url,
        agent_namespace=_env("HARNESS_AGENT_NAMESPACE", required=True),
        agent=_env("HARNESS_AGENT", required=True),
        revision=_env("HARNESS_REVISION", required=True),
        dataset=_env("HARNESS_DATASET", required=True),
        checks=tuple(checks),
        threshold=_env_float("HARNESS_THRESHOLD", 0.8),
        mode=_env("HARNESS_MODE", "report"),
        trigger=_env("HARNESS_TRIGGER", "new-revision"),
        result_configmap=_env("HARNESS_RESULT_CONFIGMAP", required=True),
        pod_namespace=_env("POD_NAMESPACE", required=True),
        cluster=_env("CLUSTER_NAME"),
        bifrost_url=_env(
            "BIFROST_URL", "http://bifrost.bifrost.svc.cluster.local:8080/v1"
        ).rstrip("/"),
        judge_model=_env("HARNESS_JUDGE_MODEL", required=True),
        case_concurrency=_positive(
            "HARNESS_CASE_CONCURRENCY", _env_int("HARNESS_CASE_CONCURRENCY", 2)
        ),
        case_timeout=_positive(
            "HARNESS_CASE_TIMEOUT_SECONDS", _env_int("HARNESS_CASE_TIMEOUT_SECONDS", 120)
        ),
        ready_timeout=_positive(
            "HARNESS_READY_TIMEOUT_SECONDS", _env_int("HARNESS_READY_TIMEOUT_SECONDS", 600)
        ),
        max_cases=_positive("HARNESS_MAX_CASES", _env_int("HARNESS_MAX_CASES", 50)),
        checks_path=_env("HARNESS_CHECKS_PATH", f"{JOB_CHECKS_DIR}/{JOB_CHECKS_FILE}"),
        datasets_dir=_env("HARNESS_DATASETS_DIR", JOB_DATASETS_DIR),
        credentials_dir=credentials_dir,
        judge_virtual_key=_read_secret_file(f"{credentials_dir}/{JUDGE_KEY_FILE}"),
    )


def load_checks(path: str) -> dict[str, Check]:
    """Parse `checks: {expected-answer: {casePassScore, prompt}}`."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read harness checks at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"harness checks at {path} are not valid YAML: {exc}") from exc

    entries = raw.get("checks") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ConfigError(f"harness checks at {path} define no `checks` mapping")

    out: dict[str, Check] = {}
    for name, spec in entries.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"harness check {name!r} must be a mapping")
        prompt = spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError(f"harness check {name!r} has no prompt")
        try:
            score = float(spec.get("casePassScore", 0.7))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"harness check {name!r} casePassScore is not a number"
            ) from exc
        if not 0 <= score <= 1:
            raise ConfigError(
                f"harness check {name!r} casePassScore must be in [0, 1], got {score}"
            )
        out[str(name)] = Check(name=str(name), prompt=prompt, case_pass_score=score)
    return out
