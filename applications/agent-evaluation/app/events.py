"""Report a declaration this reconciler cannot act on as a Kubernetes Event.

This is the whole user-facing error channel for the per-cluster loop. An unknown
evaluator, an unknown judge alias or an unknown level is skipped rather than
guessed at, and the Event on the agent's own ConfigMap is how the owning team
finds out — `kubectl describe configmap <agent>-evaluation` in their own
namespace, with no access to the platform's logs.

Events are deduplicated by (object, reason, message) with a count and a last-seen
timestamp, which matters because these loops run every 60 seconds: a spoke
waiting for the hub to publish an evaluator would otherwise write one Event per
pass until it arrives.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

COMPONENT = "agent-evaluation-reconciler"

# Reasons, kept short and stable: teams and runbooks match on these.
REASON_UNKNOWN_EVALUATOR = "UnknownEvaluator"
REASON_EVALUATOR_NOT_PUBLISHED = "EvaluatorNotPublished"
REASON_UNKNOWN_JUDGE = "UnknownJudge"
REASON_INVALID_DECLARATION = "InvalidEvaluationDeclaration"
REASON_RULE_LIMIT = "EvaluationRuleLimitReached"
REASON_RECONCILED = "EvaluationReconciled"


class EventRecorder:
    """Emits Events against the agents' ConfigMaps, deduplicating repeats."""

    def __init__(self, cluster: str, core_v1: client.CoreV1Api | None = None):
        self._core = core_v1 or client.CoreV1Api()
        self._cluster = cluster
        # (namespace, name, reason, message) -> event name, so a repeat patches
        # the existing Event's count instead of creating another.
        self._seen: dict[tuple[str, str, str, str], str] = {}

    def warn(self, namespace: str, configmap: str, reason: str, message: str) -> None:
        self._emit(namespace, configmap, reason, message, "Warning")

    def normal(self, namespace: str, configmap: str, reason: str, message: str) -> None:
        self._emit(namespace, configmap, reason, message, "Normal")

    def _emit(
        self,
        namespace: str,
        configmap: str,
        reason: str,
        message: str,
        event_type: str,
    ) -> None:
        # The cluster is in the message because one Langfuse project serves the
        # whole fleet: "evaluator not published" needs to say which cluster is
        # waiting for it.
        message = f"[{self._cluster}] {message}"
        key = (namespace, configmap, reason, message)
        now = datetime.now(timezone.utc)
        existing = self._seen.get(key)

        if existing and self._patch_count(namespace, existing, now):
            return

        name = f"{configmap}.{reason.lower()}.{abs(hash(message)) % 10**8:08d}"
        event = client.CoreV1Event(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            involved_object=client.V1ObjectReference(
                api_version="v1",
                kind="ConfigMap",
                name=configmap,
                namespace=namespace,
            ),
            reason=reason,
            message=message,
            type=event_type,
            source=client.V1EventSource(component=COMPONENT),
            first_timestamp=now,
            last_timestamp=now,
            count=1,
            reporting_component=COMPONENT,
            reporting_instance=self._cluster,
        )
        try:
            self._core.create_namespaced_event(namespace=namespace, body=event)
            self._seen[key] = name
        except ApiException as exc:
            if exc.status == 409:
                # Another replica, or this one after a restart, already wrote it.
                self._seen[key] = name
                self._patch_count(namespace, name, now)
                return
            # An Event that cannot be written must not fail the pass: the rules
            # still need reconciling. Log it and move on.
            logger.warning(
                "could not record Event %s on %s/%s: %s %s",
                reason,
                namespace,
                configmap,
                exc.status,
                exc.reason,
            )

    def _patch_count(self, namespace: str, name: str, now: datetime) -> bool:
        """Bump an existing Event's count. False when it is gone (expired)."""
        try:
            current = self._core.read_namespaced_event(name=name, namespace=namespace)
            self._core.patch_namespaced_event(
                name=name,
                namespace=namespace,
                body={
                    "count": (current.count or 1) + 1,
                    "lastTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            return True
        except ApiException:
            # Events are garbage-collected after an hour by default; fall back to
            # creating a fresh one.
            return False
