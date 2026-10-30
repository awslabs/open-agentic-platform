"""Read the agents' evaluation declarations off this cluster.

The `agent` component renders one ConfigMap per agent with evaluation turned on,
labelled `evaluation.oap.io/agent` (D2). Because that ConfigMap is a KubeVela
output, deleting the Application or setting `enabled: false` deletes it, and this
reconciler then sees the agent disappear from the list — which is why deletion
needs no finalizer.

Reading is always done with the label selector, even though the ClusterRole has
to grant cluster-wide ConfigMap read (Kubernetes RBAC cannot scope a list by
label). That keeps the reconciler from pulling every ConfigMap in the cluster
into memory on each pass.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

LABEL_SELECTOR = "evaluation.oap.io/agent"

VALID_STRICTNESS = ("low", "medium", "high")


class DeclarationError(Exception):
    """A declaration this reconciler cannot act on. Reported as an Event."""


@dataclass(frozen=True)
class RequestedEvaluator:
    name: str
    # None means "use the agent's strictness"; resolved later against the catalog.
    strictness: str | None = None


@dataclass(frozen=True)
class AgentDeclaration:
    """One agent's evaluation declaration, as rendered by the component."""

    agent: str
    namespace: str
    configmap: str
    backend: str
    model: str
    strictness: str
    coverage: str
    evaluators: tuple[RequestedEvaluator, ...]
    sampling: float | None
    judge: str | None

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.agent}"

    def trace_tag(self, cluster: str) -> str:
        """The scoped tag the agent puts on its traces (D4)."""
        return f"agent:{cluster}/{self.namespace}/{self.agent}"


def _parse_evaluators(raw: str, where: str) -> tuple[RequestedEvaluator, ...]:
    """Parse the component's JSON-marshalled evaluators list.

    The component emits `json.Marshal(parameter.evaluation.evaluators)`, so an
    entry is either a bare name or {name, strictness}.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise DeclarationError(f"{where}: evaluators is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise DeclarationError(f"{where}: evaluators must be a non-empty list")

    out: list[RequestedEvaluator] = []
    seen: set[str] = set()
    for entry in parsed:
        if isinstance(entry, str):
            name, strictness = entry, None
        elif isinstance(entry, dict):
            name = entry.get("name")
            strictness = entry.get("strictness")
            if not isinstance(name, str) or not name:
                raise DeclarationError(f"{where}: an evaluator entry has no name")
            if strictness is not None and strictness not in VALID_STRICTNESS:
                raise DeclarationError(
                    f"{where}: evaluator {name!r} has strictness {strictness!r}; "
                    f"expected one of {list(VALID_STRICTNESS)}"
                )
        else:
            raise DeclarationError(
                f"{where}: an evaluator entry is neither a name nor an object"
            )
        if name in seen:
            # Two entries for one evaluator would produce two thresholds for one
            # series. Take the first and say so, rather than picking at random.
            raise DeclarationError(
                f"{where}: evaluator {name!r} is listed more than once"
            )
        seen.add(name)
        out.append(RequestedEvaluator(name=name, strictness=strictness))
    return tuple(out)


def parse_declaration(cm) -> AgentDeclaration:
    """Turn one ConfigMap into a declaration, or raise DeclarationError."""
    meta = cm.metadata
    where = f"{meta.namespace}/{meta.name}"
    data = cm.data or {}

    agent = data.get("agent") or (meta.labels or {}).get(LABEL_SELECTOR)
    if not agent:
        raise DeclarationError(f"{where}: no agent name in data or label")

    strictness = data.get("strictness", "medium")
    if strictness not in VALID_STRICTNESS:
        raise DeclarationError(
            f"{where}: strictness {strictness!r} is not one of {list(VALID_STRICTNESS)}"
        )
    coverage = data.get("coverage", "medium")

    sampling: float | None = None
    if "sampling" in data:
        try:
            sampling = float(data["sampling"])
        except ValueError as exc:
            raise DeclarationError(
                f"{where}: sampling {data['sampling']!r} is not a number"
            ) from exc
        if not 0 < sampling <= 1:
            raise DeclarationError(
                f"{where}: sampling must be in (0, 1], got {sampling}"
            )

    return AgentDeclaration(
        agent=agent,
        namespace=meta.namespace,
        configmap=meta.name,
        backend=data.get("backend", "centralized"),
        model=data.get("model", ""),
        strictness=strictness,
        coverage=coverage,
        evaluators=_parse_evaluators(data.get("evaluators", "[]"), where),
        sampling=sampling,
        judge=data.get("judge") or None,
    )


class DeclarationReader:
    def __init__(self, core_v1: client.CoreV1Api | None = None):
        self._core = core_v1 or client.CoreV1Api()

    def list(self) -> tuple[list[AgentDeclaration], list[tuple[object, str]]]:
        """Return (declarations, [(configmap, error)]) for this cluster.

        A single malformed ConfigMap must not stop the pass: the other agents'
        rules still need reconciling, so bad ones are returned separately for the
        caller to report as Events.
        """
        try:
            listing = self._core.list_config_map_for_all_namespaces(
                label_selector=LABEL_SELECTOR
            )
        except ApiException as exc:
            raise DeclarationError(
                f"listing evaluation ConfigMaps failed: {exc.status} {exc.reason}"
            ) from exc

        good: list[AgentDeclaration] = []
        bad: list[tuple[object, str]] = []
        for cm in listing.items:
            try:
                good.append(parse_declaration(cm))
            except DeclarationError as exc:
                bad.append((cm, str(exc)))
        good.sort(key=lambda d: d.key)
        return good, bad
