"""Harness Job construction and the small helpers both harness halves share.

The verdict endpoint (harness_api) creates the Job; the reader (harness_loop)
reads it back and publishes. Both need the label names, the Job body and the
way a finished Job is recognised, so they live here once.
"""

from __future__ import annotations

import json
import logging

from .config import Catalog, Settings
from .harness_config import (
    JOB_CHECKS_DIR,
    JOB_CHECKS_FILE,
    JOB_CREDENTIALS_DIR,
    JOB_DATASETS_DIR,
    JUDGE_KEY_FILE,
    HarnessSettings,
)
from .harness_declarations import HarnessDeclaration
from .harness_results import RunResult
from .naming import result_configmap_name

logger = logging.getLogger(__name__)

MANAGED_BY = "agent-evaluation-reconciler"
LABEL_RUN = "harness.oap.io/run"
LABEL_RESULT = "harness.oap.io/result"
LABEL_JOB = "harness.oap.io/job"
LABEL_AGENT_NS = "harness.oap.io/agent-namespace"
LABEL_AGENT = "harness.oap.io/agent"
# The pod-template hash the run scored. It is the `revision` label of the
# oap_agent_harness_* series, as it always was.
LABEL_REVISION = "harness.oap.io/revision"
LABEL_DATASET = "harness.oap.io/dataset"
# The Rollout revision number (rollout.argoproj.io/revision) of the run. A
# newer number replaces an older one; the pod hash cannot be ordered.
LABEL_REVISION_NUMBER = "harness.oap.io/revision-number"
ANN_TRIGGER = "harness.oap.io/trigger"
ANN_THRESHOLD = "harness.oap.io/threshold"
ANN_CHECKS = "harness.oap.io/checks"
ANN_MODE = "harness.oap.io/mode"
ANN_JUDGE_MODEL = "harness.oap.io/judge-model"
# Pod-template annotation the endpoint sets when it unsuspends the Job: the
# absolute time (epoch seconds) the run must end. The runner reads it through
# the downward API, so the clock starts at the unsuspend, not at the create.
# A suspended Job that never ran accepts a template annotation change; env
# cannot change.
ANN_RUN_DEADLINE = "harness.oap.io/run-deadline"
# Argo Rollouts pins the preview Service's selector to the previewed revision.
POD_TEMPLATE_HASH_LABEL = "rollouts-pod-template-hash"
# Set by the Job controller on every pod of a Job, value = the Job's uid.
JOB_CONTROLLER_UID_LABEL = "batch.kubernetes.io/controller-uid"
# Analysis analysis cannot tell a first revision from a rollback (design D7).
TRIGGER_NEW = "new-revision"


def _labels(job) -> dict:
    return getattr(job.metadata, "labels", None) or {}


def _annotations(job) -> dict:
    return getattr(job.metadata, "annotations", None) or {}


def _agent_key(job) -> str:
    labels = _labels(job)
    return f"{labels.get(LABEL_AGENT_NS, '')}/{labels.get(LABEL_AGENT, '')}"



def _created(job) -> float:
    # Set by the API server, so it orders Jobs across reconciler restarts.
    return _ts(getattr(job.metadata, "creation_timestamp", None)) or 0.0


def _finished_condition(job):
    """'Complete' or 'Failed' with its condition, or (None, None) while active."""
    status = getattr(job, "status", None)
    for cond in getattr(status, "conditions", None) or []:
        if cond.type in ("Complete", "Failed") and str(cond.status) == "True":
            return cond.type, cond
    return None, None


def _owned_by(obj, job) -> bool:
    """True when one of obj's ownerReferences carries the Job's uid.

    Fails closed: a Job or an object without a uid never matches.
    """
    uid = getattr(job.metadata, "uid", None)
    if not uid:
        return False
    refs = getattr(obj.metadata, "owner_references", None) or []
    return any(getattr(ref, "uid", None) == uid for ref in refs)


MAX_EVENT_MESSAGE_CHARS = 1000


def _with_failed_ids(message: str, run: RunResult) -> str:
    """Append "; failed: id1, id2" so a team sees which cases to look at.

    Capped at MAX_EVENT_MESSAGE_CHARS with "+N more". Errored cases keep the
    count only: they say nothing about the revision.
    """
    failed = [c.id for c in run.case_results if c.status == "failed"]
    if not failed:
        return message
    shown: list[str] = []
    for index, case_id in enumerate(failed):
        rest = len(failed) - index - 1
        candidate = f"{message}; failed: {', '.join(shown + [case_id])}"
        more = f" +{rest} more" if rest else ""
        if len(candidate + more) > MAX_EVENT_MESSAGE_CHARS:
            break
        shown.append(case_id)
    hidden = len(failed) - len(shown)
    if not shown:
        return f"{message}; failed: +{hidden} more"[:MAX_EVENT_MESSAGE_CHARS]
    return f"{message}; failed: {', '.join(shown)}" + (f" +{hidden} more" if hidden else "")




def _ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return value.timestamp()
    except AttributeError:
        return None


class JobFactory:
    """Builds harness Job bodies from the platform settings."""

    def __init__(self, settings: Settings, harness_settings: HarnessSettings, catalog: Catalog):
        self._s = settings
        self._h = harness_settings
        self._catalog = catalog

    @property
    def judge_model(self) -> str:
        return self._catalog.judge_model(self._h.judge_alias or self._catalog.judge_default)

    def job_body(
        self,
        decl: HarnessDeclaration,
        revision: str,
        revision_number: str,
        name: str,
        owner: dict | None = None,
        suspend: bool = False,
    ) -> dict:
        """The runner Job for one revision.

        `revision` is the pod-template hash (the series label), `revision_number`
        the Rollout's revision number. The Job is created suspended: the
        suspended Job is the wait for a slot (its age, from creation, is the
        wait). The run's clock starts when the endpoint unsuspends it.
        """
        trigger = TRIGGER_NEW
        h = self._h
        checks = json.dumps(list(decl.checks))
        target = (
            f"http://{decl.preview_service}.{decl.namespace}.svc.cluster.local:"
            f"{decl.preview_port}/"
        )
        env = {
            "HARNESS_TARGET_URL": target,
            "HARNESS_AGENT_NAMESPACE": decl.namespace,
            "HARNESS_AGENT": decl.agent,
            "HARNESS_REVISION": revision,
            "HARNESS_DATASET": decl.dataset,
            "HARNESS_CHECKS": checks,
            "HARNESS_THRESHOLD": str(decl.threshold),
            "HARNESS_MODE": decl.mode,
            "HARNESS_TRIGGER": trigger,
            "HARNESS_RESULT_CONFIGMAP": result_configmap_name(name),
            "CLUSTER_NAME": self._s.cluster,
            "BIFROST_URL": self._s.bifrost_url,
            "HARNESS_JUDGE_MODEL": self.judge_model,
            "HARNESS_CASE_CONCURRENCY": str(h.case_concurrency),
            "HARNESS_CASE_TIMEOUT_SECONDS": str(h.case_timeout),
            "HARNESS_READY_TIMEOUT_SECONDS": str(h.ready_timeout),
            # Same value as activeDeadlineSeconds, so the runner can stop first.
            "HARNESS_RUN_TIMEOUT_SECONDS": str(h.run_timeout),
            "HARNESS_MAX_CASES": str(h.max_cases),
            "HARNESS_MIN_JUDGED_RATIO": str(h.min_judged_ratio),
            "HARNESS_CHECKS_PATH": f"{JOB_CHECKS_DIR}/{JOB_CHECKS_FILE}",
            "HARNESS_DATASETS_DIR": JOB_DATASETS_DIR,
            "CREDENTIALS_DIR": JOB_CREDENTIALS_DIR,
        }
        env_list = [{"name": k, "value": v} for k, v in env.items()]
        env_list.append(
            {
                "name": "POD_NAMESPACE",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
            }
        )
        # activeDeadlineSeconds counts from the Job's start (the unsuspend), not
        # the runner's: the runner stops at this absolute time (minus its
        # margin). The endpoint stamps it on the pod template in the same patch
        # that unsuspends the Job. Empty when absent: the runner then counts
        # from its own start.
        env_list.append(
            {
                "name": "HARNESS_RUN_DEADLINE_EPOCH",
                "valueFrom": {
                    "fieldRef": {"fieldPath": f"metadata.annotations['{ANN_RUN_DEADLINE}']"}
                },
            }
        )
        # The Job's uid, from the label the Job controller puts on its pods:
        # the runner makes its result owned by this Job object (see _collect).
        env_list.append(
            {
                "name": "HARNESS_JOB_UID",
                "valueFrom": {
                    "fieldRef": {
                        "fieldPath": f"metadata.labels['{JOB_CONTROLLER_UID_LABEL}']"
                    }
                },
            }
        )
        labels = {
            LABEL_RUN: "true",
            LABEL_AGENT_NS: decl.namespace,
            LABEL_AGENT: decl.agent,
            LABEL_REVISION: revision,
            LABEL_DATASET: decl.dataset,
            LABEL_REVISION_NUMBER: str(revision_number),
            "app.kubernetes.io/managed-by": MANAGED_BY,
        }
        metadata = {
            "name": name,
            "namespace": self._s.namespace,
            "labels": labels,
            "annotations": {
                ANN_TRIGGER: trigger,
                ANN_THRESHOLD: str(decl.threshold),
                ANN_CHECKS: checks,
                ANN_MODE: decl.mode,
                ANN_JUDGE_MODEL: self.judge_model,
            },
        }
        if owner is not None:
            # Owned by the chart's catalog ConfigMap: turning the harness off
            # (Argo prunes it) lets the garbage collector delete the Jobs.
            metadata["ownerReferences"] = [dict(owner)]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata,
            "spec": {
                # A failed run is reported, never retried: a retry would drive
                # the agent again with the same cases, and the result ConfigMap
                # is create-only. The one exception is a pod the cluster took
                # away (drain, consolidation, preemption): podFailurePolicy
                # ignores it, so the Job starts a new pod without counting a
                # failure, within the same absolute run deadline. Without it a
                # node consolidation would fail the Job and abort a gate.
                "backoffLimit": 0,
                "podFailurePolicy": {
                    "rules": [
                        {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]}
                    ]
                },
                # Created suspended; the deadline counts from the unsuspend.
                "suspend": suspend,
                "activeDeadlineSeconds": h.run_timeout,
                "template": {
                    "metadata": {
                        "labels": {**labels, LABEL_JOB: name},
                        # Karpenter (and EKS Auto Mode) does not consolidate or
                        # drift a node while this pod runs.
                        "annotations": {"karpenter.sh/do-not-disrupt": "true"},
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": h.runner_service_account,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "runner",
                                "image": h.job_image,
                                "imagePullPolicy": h.job_image_pull_policy,
                                "command": ["python", "-m", "app.harness_runner"],
                                "env": env_list,
                                "resources": dict(h.runner_resources),
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "harness-checks",
                                        "mountPath": JOB_CHECKS_DIR,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "harness-datasets",
                                        "mountPath": JOB_DATASETS_DIR,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "judge-key",
                                        "mountPath": f"{JOB_CREDENTIALS_DIR}/{JUDGE_KEY_FILE}",
                                        "subPath": JUDGE_KEY_FILE,
                                        "readOnly": True,
                                    },
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "harness-checks",
                                "configMap": {
                                    "name": h.catalog_configmap,
                                    "items": [
                                        {"key": JOB_CHECKS_FILE, "path": JOB_CHECKS_FILE}
                                    ],
                                },
                            },
                            {
                                "name": "harness-datasets",
                                "configMap": {"name": h.datasets_configmap},
                            },
                            {
                                "name": "judge-key",
                                "secret": {
                                    "secretName": h.judge_key_secret,
                                    # Required when the key is required: a
                                    # pod never starts without it and so
                                    # never runs the judge un-budgeted.
                                    "optional": not h.judge_key_required,
                                    "items": [
                                        {"key": JUDGE_KEY_FILE, "path": JUDGE_KEY_FILE}
                                    ],
                                },
                            },
                            {"name": "tmp", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

