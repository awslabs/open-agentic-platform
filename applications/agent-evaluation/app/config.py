"""Reconciler configuration: environment for the deployment, a file for the catalog.

Split on purpose. Everything that differs per cluster (role, cluster name,
Langfuse URL, credentials) arrives as environment from the chart, the same way
the rest of the platform passes per-cluster values. The catalog, the coverage
levels and the judge aliases are platform policy shared by every cluster, so
they arrive as one mounted YAML file rendered from the chart's values — which
also means a prompt change is a chart change, reviewed in one place (D2).

No credential is ever read from the catalog file or from a chart value. The
Langfuse key pair and the judge virtual key are mounted from Secrets, which the
chart creates with ExternalSecret from Secrets Manager.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

STRICTNESS_LEVELS = ("low", "medium", "high")

# The observation fields a prompt variable may be populated from. An
# observation-target evaluation rule accepts only these, so a catalog entry
# naming anything else is rejected before any Langfuse call.
VALID_MAPPING_SOURCES = ("input", "output", "metadata", "tool_calls")

# Variables every catalog prompt uses, when an entry declares no mapping.
DEFAULT_MAPPING = {"input": "input", "output": "output"}


class ConfigError(Exception):
    """Configuration that cannot be defaulted. The reconciler exits on these."""


@dataclass(frozen=True)
class Evaluator:
    """One catalog entry: a judge prompt plus the scale it is scored on."""

    name: str
    prompt: str
    target: str
    score_type: str
    pass_threshold: dict[str, float]
    reasoning_prompt: str
    output_prompt: str
    # Langfuse variable -> observation field. Defaults cover input/output; an
    # entry that scores tool calls adds tool_calls.
    mapping: dict[str, str] = field(default_factory=dict)

    def threshold(self, strictness: str) -> float:
        try:
            return self.pass_threshold[strictness]
        except KeyError as exc:
            raise ConfigError(
                f"evaluator {self.name!r} has no passThreshold for strictness "
                f"{strictness!r}; catalog defines {sorted(self.pass_threshold)}"
            ) from exc


@dataclass(frozen=True)
class Catalog:
    evaluators: dict[str, Evaluator]
    coverage: dict[str, float]
    judge_default: str
    # Judge label -> the model id sent to the gateway. Two separate things on
    # purpose: the LABEL is what a developer writes in `evaluation.judge` and
    # what appears in evaluator object names, while the MODEL ID is what Bifrost
    # needs. They cannot be the same string, because Bifrost does not
    # auto-resolve a bare platform alias ("no providers found for model
    # 'judge-default' in model catalog to auto-resolve") and Langfuse requires
    # the model to be listed on the LLM connection. Using the real model id also
    # lets Langfuse attribute judge cost from its own model catalog.
    judge_models: dict[str, str]

    @property
    def judge_aliases(self) -> tuple[str, ...]:
        return tuple(self.judge_models)

    def judge_model(self, alias: str) -> str:
        try:
            return self.judge_models[alias]
        except KeyError as exc:
            raise ConfigError(
                f"judge {alias!r} is not in the platform's judge list "
                f"{sorted(self.judge_models)}"
            ) from exc

    def sampling_for(self, coverage: str) -> float:
        try:
            return self.coverage[coverage]
        except KeyError as exc:
            raise ConfigError(
                f"coverage level {coverage!r} is not defined; "
                f"chart defines {sorted(self.coverage)}"
            ) from exc


@dataclass(frozen=True)
class Settings:
    role: str
    cluster: str
    namespace: str
    langfuse_url: str
    langfuse_public_key: str
    langfuse_secret_key: str
    judge_virtual_key: str
    bifrost_url: str
    reconcile_interval: int
    metrics_interval: int
    metrics_windows: tuple[str, ...]
    metrics_port: int
    catalog_path: str
    langfuse_timeout: float
    # Langfuse's documented cap on active evaluation rules per project. Checked
    # before a create so the reconciler reports the limit rather than letting
    # Langfuse 409 mid-pass (D4).
    max_active_rules: int

    @property
    def is_hub(self) -> bool:
        return self.role == "hub"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, "" if default is None else default).strip()
    if required and not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _read_secret_file(path: str) -> str:
    """Read a credential from a mounted file, preferring it over an env var.

    A file mount is the better shape for a secret than an env var: it is not
    visible in `kubectl describe pod` output and it updates in place when the
    ExternalSecret refreshes. The env var stays supported because the chart's
    Secret is also loaded with envFrom for backward compatibility.
    """
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_settings() -> Settings:
    role = _env("ROLE", "spoke").lower()
    if role not in ("hub", "spoke"):
        raise ConfigError(f"ROLE must be 'hub' or 'spoke', got {role!r}")

    langfuse_url = _env("LANGFUSE_URL", required=True).rstrip("/")
    if not langfuse_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"LANGFUSE_URL must be an absolute http(s) URL, got {langfuse_url!r}"
        )

    secret_dir = _env("CREDENTIALS_DIR", "/etc/agent-evaluation/credentials")
    public_key = _read_secret_file(f"{secret_dir}/LANGFUSE_PUBLIC_KEY") or _env(
        "LANGFUSE_PUBLIC_KEY"
    )
    secret_key = _read_secret_file(f"{secret_dir}/LANGFUSE_SECRET_KEY") or _env(
        "LANGFUSE_SECRET_KEY"
    )
    if not public_key or not secret_key:
        raise ConfigError(
            "Langfuse public/secret key pair not found. The chart mounts it from "
            f"{secret_dir} via ExternalSecret; check that the Secrets Manager entry "
            "<resourcePrefix>-hub/langfuse-otel exists and the ExternalSecret synced."
        )

    # Budget/attribution tag for judge calls, not an access credential: Bifrost
    # authenticates to Bedrock with its own IAM identity. Absent is tolerated
    # (Bifrost runs is_vk_mandatory: false), and the judge then runs un-budgeted
    # — reported as a warning and on oap_agent_eval_judge_key_present.
    judge_vk = _read_secret_file(f"{secret_dir}/BIFROST_VIRTUAL_KEY") or _env(
        "BIFROST_VIRTUAL_KEY"
    )

    return Settings(
        role=role,
        cluster=_env("CLUSTER_NAME", required=True),
        namespace=_env("POD_NAMESPACE", "agent-evaluation"),
        langfuse_url=langfuse_url,
        langfuse_public_key=public_key,
        langfuse_secret_key=secret_key,
        judge_virtual_key=judge_vk,
        bifrost_url=_env(
            "BIFROST_URL", "http://bifrost.bifrost.svc.cluster.local:8080/v1"
        ).rstrip("/"),
        reconcile_interval=_env_int("RECONCILE_INTERVAL_SECONDS", 60),
        metrics_interval=_env_int("METRICS_INTERVAL_SECONDS", 300),
        metrics_windows=tuple(
            w.strip() for w in _env("METRICS_WINDOWS", "24h,7d").split(",") if w.strip()
        ),
        # Bound to the pod network, never a host port: the chart publishes no
        # hostPort and the Service is ClusterIP, so this listener is reachable
        # only from inside the cluster (the AMP scraper and the kubelet probes).
        metrics_port=_env_int("METRICS_PORT", 9090),
        catalog_path=_env("CATALOG_PATH", "/etc/agent-evaluation/catalog.yaml"),
        langfuse_timeout=float(_env("LANGFUSE_TIMEOUT_SECONDS", "30")),
        max_active_rules=_env_int("MAX_ACTIVE_RULES", 50),
    )


def _require(obj: dict, key: str, where: str):
    if key not in obj:
        raise ConfigError(f"{where} is missing required key {key!r}")
    return obj[key]


def load_catalog(path: str) -> Catalog:
    """Parse the mounted catalog file into evaluators, coverage and judge aliases."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read catalog at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"catalog at {path} is not valid YAML: {exc}") from exc

    coverage_raw = _require(raw, "coverage", "catalog")
    coverage: dict[str, float] = {}
    for level, rate in coverage_raw.items():
        rate = float(rate)
        if not 0 < rate <= 1:
            raise ConfigError(
                f"coverage.{level} must be in (0, 1], got {rate}"
            )
        coverage[level] = rate

    judge = raw.get("judge") or {}
    judge_default = str(_require(judge, "defaultAlias", "catalog.judge"))
    models_raw = _require(judge, "models", "catalog.judge")
    if not isinstance(models_raw, dict) or not models_raw:
        raise ConfigError(
            "catalog.judge.models must be a non-empty mapping of judge alias to "
            "gateway model id, for example {judge-default: us.amazon.nova-pro-v1:0}"
        )
    judge_models = {str(alias): str(model) for alias, model in models_raw.items()}
    if judge_default not in judge_models:
        raise ConfigError(
            f"catalog.judge.defaultAlias is {judge_default!r} but that alias is "
            f"not in catalog.judge.models {sorted(judge_models)}; an agent that "
            "does not name a judge would have no model to score with"
        )
    for alias, model in judge_models.items():
        if "--" in alias:
            raise ConfigError(
                f"judge alias {alias!r} must not contain '--': it is the separator "
                "between an evaluator and its judge in Langfuse object names"
            )
        if not model:
            raise ConfigError(f"judge alias {alias!r} has an empty model id")

    evaluators: dict[str, Evaluator] = {}
    for name, spec in (_require(raw, "catalog", "catalog")).items():
        if "--" in name:
            raise ConfigError(
                f"catalog evaluator {name!r} must not contain '--': it is the "
                "separator between an evaluator and its judge in Langfuse names"
            )
        thresholds = {
            level: float(value)
            for level, value in (_require(spec, "passThreshold", f"catalog.{name}")).items()
        }
        missing = [lvl for lvl in STRICTNESS_LEVELS if lvl not in thresholds]
        if missing:
            raise ConfigError(
                f"catalog.{name}.passThreshold is missing level(s) {missing}; "
                "every strictness level must resolve to a threshold"
            )
        for level, value in thresholds.items():
            if not 0 <= value <= 1:
                raise ConfigError(
                    f"catalog.{name}.passThreshold.{level} must be in [0, 1], got {value}"
                )
        evaluators[name] = Evaluator(
            name=name,
            prompt=str(_require(spec, "prompt", f"catalog.{name}")),
            target=str(spec.get("target", "root-agent-observation")),
            score_type=str(spec.get("scoreType", "numeric")),
            pass_threshold=thresholds,
            reasoning_prompt=str(spec.get("reasoningPrompt", "")),
            output_prompt=str(spec.get("outputPrompt", "")),
            mapping=dict(spec.get("mapping") or {}),
        )

    if not evaluators:
        raise ConfigError("catalog defines no evaluators")

    return Catalog(
        evaluators=evaluators,
        coverage=coverage,
        judge_default=judge_default,
        judge_models=judge_models,
    )
