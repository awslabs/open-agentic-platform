"""Read the agents' harness declarations off this cluster.

The `agent` component renders `<agent>-harness`, labelled `harness.oap.io/agent`,
when the agent declares a `harness` block. Like the evaluation declaration it is
a KubeVela output, so removing the block or the Application deletes it and the
reconciler then prunes that agent's runs. Reading is by label selector only
(same reasoning as declarations.py).

The harness runner's `oap-harness-*-result` ConfigMaps carry
`harness.oap.io/agent` as well, so the selector excludes anything labelled
`harness.oap.io/result`. Results are never parsed as declarations and never get
an InvalidHarnessDeclaration Event.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.rest import ApiException

from .datasets import MAX_NAME_LEN, NAME_RE
from .harness_config import KNOWN_CHECKS

logger = logging.getLogger(__name__)

LABEL_SELECTOR = "harness.oap.io/agent"
# Set by the harness runner on its result ConfigMaps (harness_runner._write_result).
LABEL_RESULT = "harness.oap.io/result"
# "key" means the label exists, "!key" that it does not (Kubernetes set-based selector).
DECLARATION_SELECTOR = f"{LABEL_SELECTOR},!{LABEL_RESULT}"

# Report mode only. `gate` is rejected by the CUE already; checking again here
# means a hand-written ConfigMap cannot turn a report into a gate.
VALID_MODES = ("report",)

DEFAULT_PREVIEW_PORT = 8083

# Both values come from a team-writable ConfigMap and end up in a platform Job:
# `agent` in its labels and env, `previewService` in its target URL. Only plain
# in-namespace names are accepted, so a declaration cannot point the Job at
# another host or make Job creation fail for everyone.
MAX_LABEL_LEN = 63
DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS1035_LABEL_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


class HarnessDeclarationError(Exception):
    """A harness declaration this reconciler cannot act on. Reported as an Event."""


@dataclass(frozen=True)
class HarnessDeclaration:
    agent: str
    namespace: str
    configmap: str
    dataset: str
    checks: tuple[str, ...]
    threshold: float
    mode: str
    preview_service: str
    preview_port: int

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.agent}"


def parse_harness_declaration(cm) -> HarnessDeclaration:
    meta = cm.metadata
    where = f"{meta.namespace}/{meta.name}"
    data = cm.data or {}

    agent = data.get("agent") or (meta.labels or {}).get(LABEL_SELECTOR)
    if not agent:
        raise HarnessDeclarationError(f"{where}: no agent name in data or label")
    if (
        not isinstance(agent, str)
        or len(agent) > MAX_LABEL_LEN
        or not DNS1123_LABEL_RE.match(agent)
    ):
        raise HarnessDeclarationError(
            f"{where}: agent {agent!r} is not a DNS-1123 label of at most "
            f"{MAX_LABEL_LEN} characters"
        )

    dataset = data.get("dataset")
    if (
        not isinstance(dataset, str)
        or len(dataset) > MAX_NAME_LEN
        or not NAME_RE.match(dataset)
    ):
        raise HarnessDeclarationError(
            f"{where}: dataset {dataset!r} is missing or not a valid dataset name"
        )

    try:
        checks = json.loads(data.get("checks", ""))
    except ValueError as exc:
        raise HarnessDeclarationError(f"{where}: checks is not valid JSON: {exc}") from exc
    if not isinstance(checks, list) or not checks:
        raise HarnessDeclarationError(f"{where}: checks must be a non-empty list")
    unknown = [c for c in checks if c not in KNOWN_CHECKS]
    if unknown:
        raise HarnessDeclarationError(
            f"{where}: unknown check(s) {unknown}; known checks are {list(KNOWN_CHECKS)}"
        )

    raw_threshold = data.get("threshold", "0.8")
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise HarnessDeclarationError(
            f"{where}: threshold {raw_threshold!r} is not a number"
        ) from exc
    if not 0 < threshold <= 1:
        raise HarnessDeclarationError(
            f"{where}: threshold must be in (0, 1], got {threshold}"
        )

    mode = data.get("mode", "report")
    if mode not in VALID_MODES:
        raise HarnessDeclarationError(
            f"{where}: mode {mode!r} is not supported; only {list(VALID_MODES)} "
            "is implemented, so the harness never blocks a promotion"
        )

    raw_port = data.get("previewPort", str(DEFAULT_PREVIEW_PORT))
    try:
        preview_port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise HarnessDeclarationError(
            f"{where}: previewPort {raw_port!r} is not an integer"
        ) from exc
    if not 0 < preview_port < 65536:
        raise HarnessDeclarationError(f"{where}: previewPort {preview_port} is out of range")

    preview_service = data.get("previewService") or f"{agent}-preview"
    if len(preview_service) > MAX_LABEL_LEN or not DNS1035_LABEL_RE.match(preview_service):
        raise HarnessDeclarationError(
            f"{where}: previewService {preview_service!r} is not a Service name "
            f"(a DNS-1035 label of at most {MAX_LABEL_LEN} characters) in this namespace"
        )

    return HarnessDeclaration(
        agent=agent,
        namespace=meta.namespace,
        configmap=meta.name,
        dataset=dataset,
        checks=tuple(dict.fromkeys(checks)),
        threshold=threshold,
        mode=mode,
        preview_service=preview_service,
        preview_port=preview_port,
    )


class HarnessDeclarationReader:
    def __init__(self, core_v1: client.CoreV1Api | None = None):
        self._core = core_v1 or client.CoreV1Api()

    def list(self) -> tuple[list[HarnessDeclaration], list[tuple[object, str]]]:
        """Return (declarations, [(configmap, error)]); a bad one never stops the pass."""
        try:
            listing = self._core.list_config_map_for_all_namespaces(
                label_selector=DECLARATION_SELECTOR
            )
        except ApiException as exc:
            raise HarnessDeclarationError(
                f"listing harness ConfigMaps failed: {exc.status} {exc.reason}"
            ) from exc

        good: list[HarnessDeclaration] = []
        bad: list[tuple[object, str]] = []
        for cm in listing.items:
            try:
                good.append(parse_harness_declaration(cm))
            except HarnessDeclarationError as exc:
                bad.append((cm, str(exc)))
        good.sort(key=lambda d: d.key)
        return good, bad
