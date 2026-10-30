"""Fakes for the two dependencies the loops talk to: Langfuse and Kubernetes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.config import Catalog, Evaluator, Settings
from app.metrics import Metrics


def _read_shape(payload: dict) -> dict:
    """Echo an evaluator the way Langfuse v4 actually reads it back.

    Two fields do NOT round-trip, and both used to make the diff report a
    change on every pass: the prompt is returned as chat messages rather than
    the string that was sent, and outputDefinition comes back carrying only
    dataType, with the reasoning/score descriptions dropped.
    """
    out: dict = {}
    if isinstance(payload.get("prompt"), str):
        out["prompt"] = [{"role": "user", "content": payload["prompt"]}]
    if isinstance(payload.get("outputDefinition"), dict):
        out["outputDefinition"] = {
            "dataType": payload["outputDefinition"].get("dataType")
        }
    return out


@dataclass
class FakeLangfuse:
    """In-memory stand-in with the same role separation as the real client."""

    allow_project_writes: bool = True
    evaluators: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    metric_responses: dict[str, list[dict]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    next_id: int = 1

    # ── evaluators ────────────────────────────────────────────────────────
    def list_evaluators(self):
        self.calls.append("list_evaluators")
        return list(self.evaluators)

    def create_evaluator(self, payload):
        """v4: always creates a NEW evaluator at version 1, never an upsert."""
        if not self.allow_project_writes:
            from app.langfuse_client import ProjectWriteForbidden

            raise ProjectWriteForbidden("spoke may not write evaluators")
        self.calls.append(f"create_evaluator:{payload['name']}")
        record = {
            **payload,
            **_read_shape(payload),
            "id": f"ev-{self.next_id}",
            "version": 1,
            "status": "active",
            "createdAt": f"2026-01-01T00:00:{self.next_id:02d}Z",
        }
        self.next_id += 1
        self.evaluators.append(record)
        return record

    def update_evaluator(self, evaluator_id, payload):
        """v4: PATCH replaces the definition, which publishes a new version."""
        if not self.allow_project_writes:
            from app.langfuse_client import ProjectWriteForbidden

            raise ProjectWriteForbidden("spoke may not write evaluators")
        self.calls.append(f"update_evaluator:{payload['name']}")
        out = None
        for i, e in enumerate(self.evaluators):
            if e.get("id") == evaluator_id:
                out = {
                    **payload,
                    **_read_shape(payload),
                    "id": evaluator_id,
                    "version": (e.get("version") or 1) + 1,
                    "status": "active",
                    "createdAt": e.get("createdAt"),
                }
                self.evaluators[i] = out
        if out is None:
            raise AssertionError(f"no evaluator with id {evaluator_id}")
        return out

    def delete_evaluator(self, evaluator_id):
        if not self.allow_project_writes:
            from app.langfuse_client import ProjectWriteForbidden

            raise ProjectWriteForbidden("spoke may not delete evaluators")
        self.calls.append(f"delete_evaluator:{evaluator_id}")
        self.evaluators = [e for e in self.evaluators if e.get("id") != evaluator_id]

    # ── llm connections ───────────────────────────────────────────────────
    def list_llm_connections(self):
        return list(self.connections)

    def upsert_llm_connection(self, payload):
        if not self.allow_project_writes:
            from app.langfuse_client import ProjectWriteForbidden

            raise ProjectWriteForbidden("spoke may not write the LLM connection")
        self.calls.append(f"upsert_connection:{payload['provider']}")
        self.connections = [
            c for c in self.connections if c["provider"] != payload["provider"]
        ] + [dict(payload)]
        return payload

    # ── rules ─────────────────────────────────────────────────────────────
    def list_evaluation_rules(self):
        self.calls.append("list_rules")
        return list(self.rules)

    def create_evaluation_rule(self, payload):
        self.calls.append(f"create_rule:{payload['name']}")
        record = {**payload, "id": f"rule-{self.next_id}"}
        self.next_id += 1
        self.rules.append(record)
        return record

    def update_evaluation_rule(self, rule_id, payload):
        self.calls.append(f"update_rule:{payload['name']}")
        self.rules = [
            {**payload, "id": rule_id} if r.get("id") == rule_id else r
            for r in self.rules
        ]
        return payload

    def delete_evaluation_rule(self, rule_id):
        name = next(
            (r["name"] for r in self.rules if r.get("id") == rule_id), rule_id
        )
        self.calls.append(f"delete_rule:{name}")
        self.rules = [r for r in self.rules if r.get("id") != rule_id]

    # ── metrics ───────────────────────────────────────────────────────────
    def query_metrics(self, query):
        import json

        key = json.dumps(query, sort_keys=True)
        self.calls.append("query_metrics")
        return self.metric_responses.get(key, self.metric_responses.get("*", []))


class FakeEvents:
    """Records Events instead of writing them, so tests can assert on them."""

    def __init__(self):
        self.warnings: list[tuple[str, str, str, str]] = []
        self.normals: list[tuple[str, str, str, str]] = []

    def warn(self, namespace, configmap, reason, message):
        self.warnings.append((namespace, configmap, reason, message))

    def normal(self, namespace, configmap, reason, message):
        self.normals.append((namespace, configmap, reason, message))

    def reasons(self) -> list[str]:
        return [w[2] for w in self.warnings]


class FakeReader:
    def __init__(self, declarations=None, malformed=None):
        self._declarations = declarations or []
        self._malformed = malformed or []

    def list(self):
        return list(self._declarations), list(self._malformed)


def make_configmap(namespace: str, name: str, data: dict, labels: dict | None = None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            namespace=namespace,
            name=name,
            labels=labels if labels is not None else {"evaluation.oap.io/agent": "a"},
        ),
        data=data,
    )


@pytest.fixture
def catalog() -> Catalog:
    def evaluator(name, low, medium, high):
        return Evaluator(
            name=name,
            prompt=f"Score {name}: {{{{input}}}} {{{{output}}}}",
            target="root-agent-observation",
            score_type="numeric",
            pass_threshold={"low": low, "medium": medium, "high": high},
            reasoning_prompt=f"Explain {name}.",
            output_prompt="Return a numeric value between 0 and 1.",
        )

    return Catalog(
        evaluators={
            "goal-completion": evaluator("goal-completion", 0.5, 0.7, 0.85),
            "helpfulness": evaluator("helpfulness", 0.5, 0.7, 0.85),
            "safety": evaluator("safety", 0.8, 0.9, 0.97),
        },
        coverage={"low": 0.01, "medium": 0.1, "high": 0.5},
        judge_default="judge-default",
        # Label -> gateway model id. Deliberately different strings: Bifrost
        # does not auto-resolve a bare platform alias, so the model sent to
        # Langfuse is the concrete model id.
        judge_models={
            "judge-default": "us.amazon.nova-pro-v1:0",
            "judge-strong": "us.amazon.nova-premier-v1:0",
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        role="spoke",
        cluster="dev",
        namespace="agent-evaluation",
        langfuse_url="http://langfuse.langfuse.svc.cluster.local:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        judge_virtual_key="vk-judge",
        bifrost_url="http://bifrost.bifrost.svc.cluster.local:8080/v1",
        reconcile_interval=60,
        metrics_interval=300,
        metrics_windows=("24h",),
        metrics_port=9090,
        catalog_path="/etc/agent-evaluation/catalog.yaml",
        langfuse_timeout=30.0,
        max_active_rules=50,
    )


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()
