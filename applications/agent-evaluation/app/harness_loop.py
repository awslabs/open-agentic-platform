"""The harness loop: one platform-namespace Job per agent revision (report mode).

Per pass:

1. Read the chart's harness catalog ConfigMap: it owns every Job and the
   judge-key Secret, so turning the harness off (Argo prunes it) lets the
   garbage collector remove them. No owner, no new Job.
   Make sure the harness judge virtual key exists (minted in this cluster's
   Bifrost) and is in the Secret the Jobs mount. Fail closed: while a required
   key is unavailable, no new Job is created.
2. Read the `<agent>-harness` declarations.
3. For each, read the agent's Rollout. A run starts when the preview Service
   already selects the newest ReplicaSet (`blueGreen.previewSelector ==
   currentPodHash`). The very first revision of an agent runs too, as the
   baseline (`trigger=first-revision`).
4. Discard runs this process saw active that finished after the preview
   moved to another revision (superseded). Collect the other finished Jobs'
   result ConfigMaps and publish oap_agent_harness_* for
   each agent's latest finished run on its declared dataset, against its
   declared threshold.
5. Prune history.

The Job objects ARE the state. Job names are deterministic per (namespace,
agent, revision, dataset, run number), so a second pass or a restarted
reconciler that finds the Job creates nothing: a revision runs once each time
it becomes current. A revision that ran before runs again (`trigger=rollback`,
run number n+1) when a retained Job of another revision of the agent was
created after its newest run; with no such Job (none started, or it was
stopped or pruned) there is no re-run. Jobs always live in the
reconciler's own namespace, never in the agent's, so a team namespace never
holds platform credentials or platform Jobs.

Nothing here can block a promotion: there is no AnalysisTemplate and no gate.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass

from kubernetes.client.rest import ApiException

from . import events
from .bifrost_admin import BifrostAdminError, vk_name
from .config import Catalog, Settings
from .datasets import available_datasets
from .harness_config import (
    JOB_CHECKS_DIR,
    JOB_CHECKS_FILE,
    JOB_CREDENTIALS_DIR,
    JOB_DATASETS_DIR,
    JUDGE_KEY_FILE,
    HarnessSettings,
)
from .harness_declarations import HarnessDeclaration, HarnessDeclarationError
from .harness_results import ResultSchemaError, RunResult, parse_result, summarize
from .metrics import HarnessRow, Metrics
from .naming import harness_job_name, result_configmap_name

logger = logging.getLogger(__name__)

MANAGED_BY = "agent-evaluation-reconciler"
LABEL_RUN = "harness.oap.io/run"
LABEL_RESULT = "harness.oap.io/result"
LABEL_JOB = "harness.oap.io/job"
LABEL_AGENT_NS = "harness.oap.io/agent-namespace"
LABEL_AGENT = "harness.oap.io/agent"
LABEL_REVISION = "harness.oap.io/revision"
LABEL_DATASET = "harness.oap.io/dataset"
# Ordinal of the run of one (agent, revision, dataset). Absent means 1, so
# Jobs created before re-runs existed count as first runs.
LABEL_RUN_NUMBER = "harness.oap.io/run-number"
ANN_TRIGGER = "harness.oap.io/trigger"
ANN_THRESHOLD = "harness.oap.io/threshold"
ANN_CHECKS = "harness.oap.io/checks"
ANN_MODE = "harness.oap.io/mode"
ANN_JUDGE_MODEL = "harness.oap.io/judge-model"
# On the judge-key Secret: the Bifrost key name its value belongs to.
ANN_JUDGE_KEY_NAME = "harness.oap.io/judge-key-name"
# Set by the Job controller on every pod of a Job, value = the Job's uid.
JOB_CONTROLLER_UID_LABEL = "batch.kubernetes.io/controller-uid"

TRIGGER_FIRST = "first-revision"
TRIGGER_NEW = "new-revision"
# A revision that ran before became current again after another revision was.
TRIGGER_ROLLBACK = "rollback"


@dataclass
class _Finished:
    job: object
    run: RunResult
    finished_at: float


def _labels(job) -> dict:
    return getattr(job.metadata, "labels", None) or {}


def _annotations(job) -> dict:
    return getattr(job.metadata, "annotations", None) or {}


def _agent_key(job) -> str:
    labels = _labels(job)
    return f"{labels.get(LABEL_AGENT_NS, '')}/{labels.get(LABEL_AGENT, '')}"


def _run_number(job) -> int:
    try:
        return max(1, int(_labels(job).get(LABEL_RUN_NUMBER, "1")))
    except ValueError:
        return 1


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


class HarnessLoop:
    def __init__(
        self,
        settings: Settings,
        harness_settings: HarnessSettings,
        catalog: Catalog,
        metrics: Metrics,
        reader,
        recorder,
        batch_api,
        custom_api,
        core_api,
        bifrost_admin,
        clock=time.time,
    ):
        self._s = settings
        self._h = harness_settings
        self._catalog = catalog
        self._metrics = metrics
        self._reader = reader
        self._recorder = recorder
        self._batch = batch_api
        self._custom = custom_api
        self._core = core_api
        self._bifrost = bifrost_admin
        self._clock = clock
        # Jobs whose completion Event and counter were already recorded by this
        # process. In memory on purpose: a restart re-announces at most the
        # retained history, which is bounded by historyLimit.
        self._reported: set[str] = set()
        # Jobs this process saw active at the end of its last pass. A run of
        # this set that has finished since, while the agent's preview moved to
        # another revision, may have sent its cases to the newer pods: it is
        # superseded, not a result. In memory on purpose: after a restart the
        # set is empty and finished runs are taken as they are.
        self._seen_active: set[str] = set()

    # ── judge model and key ──────────────────────────────────────────────

    @property
    def judge_model(self) -> str:
        return self._catalog.judge_model(self._h.judge_alias or self._catalog.judge_default)

    def _owner_ref(self) -> dict | None:
        """ownerReference to the chart's harness catalog ConfigMap, or None.

        Every Job and the judge-key Secret are owned by this chart object,
        which renders only with harness.enabled: when Argo prunes it (harness
        turned off), the garbage collector deletes the Jobs, their results
        (owned by the Jobs) and the Secret. None when it cannot be read: then
        nothing that would outlive the chart is created.
        """
        try:
            cm = self._core.read_namespaced_config_map(
                name=self._h.catalog_configmap, namespace=self._s.namespace
            )
        except ApiException as exc:
            logger.error(
                "harness: cannot read the owner ConfigMap %s: %s %s",
                self._h.catalog_configmap,
                exc.status,
                exc.reason,
            )
            return None
        uid = getattr(cm.metadata, "uid", None)
        if not uid:
            logger.error("harness: owner ConfigMap %s has no uid", self._h.catalog_configmap)
            return None
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "name": self._h.catalog_configmap,
            "uid": uid,
        }

    def _secret_state(self) -> tuple[str | None, str, set]:
        """(value, key name, owner uids) in the Secret; value is None when it does not exist."""
        try:
            secret = self._core.read_namespaced_secret(
                name=self._h.judge_key_secret, namespace=self._s.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return None, "", set()
            raise
        meta = getattr(secret, "metadata", None)
        key_name = str((getattr(meta, "annotations", None) or {}).get(ANN_JUDGE_KEY_NAME, ""))
        owners = {
            getattr(ref, "uid", None) for ref in getattr(meta, "owner_references", None) or []
        }
        raw = (getattr(secret, "data", None) or {}).get(JUDGE_KEY_FILE, "")
        try:
            value = base64.b64decode(raw).decode("utf-8") if raw else ""
        except (ValueError, UnicodeDecodeError):
            value = ""
        return value, key_name, owners

    def _secret_value(self) -> str | None:
        """Current key value in the Secret; None when the Secret does not exist."""
        return self._secret_state()[0]

    def _write_secret(self, value: str, key_name: str, owner: dict) -> None:
        current, current_name, owners = self._secret_state()
        # Also rewritten when the owner is missing, so a Secret written before
        # it had one is cleaned up with the chart too.
        if current == value and current_name == key_name and owner["uid"] in owners:
            return
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": self._h.judge_key_secret,
                "namespace": self._s.namespace,
                "labels": {"app.kubernetes.io/managed-by": MANAGED_BY},
                "annotations": {ANN_JUDGE_KEY_NAME: key_name},
                "ownerReferences": [dict(owner)],
            },
            "data": {JUDGE_KEY_FILE: base64.b64encode(value.encode()).decode()},
        }
        if current is None:
            self._core.create_namespaced_secret(namespace=self._s.namespace, body=body)
        else:
            self._core.replace_namespaced_secret(
                name=self._h.judge_key_secret, namespace=self._s.namespace, body=body
            )
        logger.info("harness judge key Secret %s updated", self._h.judge_key_secret)

    def _ensure_judge_key(self, owner: dict | None, delete_stale: bool = True) -> bool:
        if not self._h.judge_key_mint:
            try:
                return bool(self._secret_value())
            except ApiException as exc:
                logger.warning("cannot read harness judge key Secret: %s", exc.status)
                return False
        if self._bifrost is None or owner is None:
            # Without the owner a written Secret would outlive the chart.
            return False
        models = self._h.judge_allowed_models or (self.judge_model,)
        config = (
            models,
            self._h.judge_budget_usd,
            self._h.judge_budget_reset,
            self._h.judge_rate_limit_requests,
            self._h.judge_rate_limit_reset,
        )
        try:
            current, current_name, _ = self._secret_state()
            known = (current_name, current) if current and current_name else None
            value = self._bifrost.ensure_judge_key(
                *config, known=known, delete_stale=delete_stale
            )
            self._write_secret(value, vk_name(*config), owner)
            return True
        except (BifrostAdminError, ApiException) as exc:
            # Never log the key; the exception messages carry none.
            logger.warning("harness judge key unavailable: %s", exc)
            return False

    # ── the pass ─────────────────────────────────────────────────────────

    def run_once(self) -> bool:
        ok = True
        cluster = self._s.cluster
        ns = self._s.namespace

        # Listed before the key step: a stale judge key is deleted only while
        # no harness Job is active (see BifrostAdmin.ensure_judge_key).
        jobs = list(
            self._batch.list_namespaced_job(
                namespace=ns, label_selector=f"{LABEL_RUN}=true"
            ).items
        )
        any_active = any(_finished_condition(j)[0] is None for j in jobs)

        owner = self._owner_ref()
        if owner is None:
            ok = False  # no Job without an owner (logged by _owner_ref)
        key_ok = self._ensure_judge_key(owner, delete_stale=not any_active)
        self._metrics.harness_judge_key_present.labels(cluster).set(1 if key_ok else 0)
        can_create = key_ok or not self._h.judge_key_required
        if not can_create:
            ok = False

        try:
            declarations, malformed = self._reader.list()
        except HarnessDeclarationError as exc:
            logger.error("%s", exc)
            return False
        for cm, error in malformed:
            self._recorder.warn(
                cm.metadata.namespace,
                cm.metadata.name,
                events.REASON_INVALID_HARNESS_DECLARATION,
                error,
            )
        by_key = {d.key: d for d in declarations}
        # Agents whose declaration ConfigMap still exists but is invalid: their
        # history and series are kept (the declaration is skipped, not removed),
        # and nothing new runs for them. Keyed like a declaration, by the
        # ConfigMap's namespace and its harness.oap.io/agent label.
        held = {}
        for cm, _ in malformed:
            agent = (getattr(cm.metadata, "labels", None) or {}).get(LABEL_AGENT, "")
            key = f"{cm.metadata.namespace}/{agent}"
            if agent and key not in by_key:
                held[key] = (cm.metadata.namespace, agent)

        results = {
            cm.metadata.name: cm
            for cm in self._core.list_namespaced_config_map(
                namespace=ns, label_selector=f"{LABEL_RESULT}=true"
            ).items
        }

        available = available_datasets(self._h.datasets_dir)
        for decl in declarations:
            if owner is None:
                break
            if not can_create:
                self._recorder.warn(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_HARNESS_JUDGE_KEY_UNAVAILABLE,
                    "the harness judge virtual key is not available, so no harness "
                    "run starts (report mode: promotion is not affected)",
                )
                continue
            try:
                self._maybe_start(decl, jobs, results, available, owner)
            except ApiException as exc:
                ok = False
                logger.error(
                    "harness: %s failed: %s %s", decl.key, exc.status, exc.reason
                )

        undecided: set[str] = set()
        try:
            if not self._supersede_finished(jobs, results, by_key, undecided):
                ok = False
        except ApiException as exc:  # a failed delete: decided again next pass
            ok = False
            undecided |= {j.metadata.name for j in jobs if j.metadata.name in self._seen_active}
            logger.error("harness supersede failed: %s %s", exc.status, exc.reason)
        finished = self._collect(
            [j for j in jobs if j.metadata.name not in undecided], results, by_key
        )
        self._publish(declarations, finished, held)
        try:
            self._prune(jobs, results, finished, set(by_key) | set(held))
        except ApiException as exc:
            ok = False
            logger.error("harness prune failed: %s %s", exc.status, exc.reason)
        self._seen_active = undecided | {
            j.metadata.name for j in jobs if _finished_condition(j)[0] is None
        }
        return ok

    def _maybe_start(
        self, decl: HarnessDeclaration, jobs: list, results: dict, available: set, owner: dict
    ) -> None:
        if decl.dataset not in available:
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_UNKNOWN_DATASET,
                f"dataset {decl.dataset!r} is not in the platform catalog "
                f"{sorted(available)}",
            )
            return
        try:
            rollout = self._custom.get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", decl.namespace, "rollouts", decl.agent
            )
        except ApiException as exc:
            if exc.status == 404:
                return  # not deployed yet
            raise
        status = (rollout or {}).get("status") or {}
        revision = status.get("currentPodHash")
        preview = (status.get("blueGreen") or {}).get("previewSelector")
        if not revision or preview != revision:
            return  # the preview Service does not select the newest ReplicaSet yet

        agent_jobs = [j for j in jobs if _agent_key(j) == decl.key]
        same = [
            j
            for j in agent_jobs
            if _labels(j).get(LABEL_REVISION) == revision
            and _labels(j).get(LABEL_DATASET) == decl.dataset
        ]
        if not same:
            run_number = 1
            trigger = TRIGGER_FIRST if not agent_jobs else TRIGGER_NEW
        else:
            # The revision ran before. It runs again only if it came back: a
            # Job of another revision of this agent was created after its
            # newest run (the "another revision was current" record). Once the
            # re-run exists it is the newest, so later passes and a restarted
            # reconciler start nothing. Decided before other revisions' active
            # runs are stopped below, which deletes their Jobs.
            newest = max(_created(j) for j in same)
            if not any(
                _labels(j).get(LABEL_REVISION) != revision and _created(j) > newest
                for j in agent_jobs
            ):
                return  # this revision already ran or is running
            run_number = max(_run_number(j) for j in same) + 1
            trigger = TRIGGER_ROLLBACK
        name = harness_job_name(
            decl.namespace, decl.agent, revision, decl.dataset, run=run_number
        )

        # This agent's active runs of other revisions: replaced by this run.
        to_stop = [
            j
            for j in agent_jobs
            if _finished_condition(j)[0] is None and _labels(j).get(LABEL_REVISION) != revision
        ]
        # They free their slots, so they do not count against this run.
        active = sum(1 for j in jobs if _finished_condition(j)[0] is None) - len(to_stop)
        if active >= self._h.max_concurrent_jobs:
            # No slot even so: stop them now (their cases would reach the new
            # pods) and wait for a slot.
            self._stop(decl, to_stop, jobs, revision)
            logger.info(
                "harness: %s revision %s waits, %d runs active", decl.key, revision, active
            )
            return

        # A result left under this name by an earlier Job of the same name (the
        # Job was deleted by hand and is now recreated): delete it first, so the
        # new run's runner can create its own (the runner Role is create-only).
        stale = result_configmap_name(name)
        if stale in results:
            self._delete_result(stale)
            results.pop(stale, None)
        body = self.job_body(decl, revision, name, trigger, run_number, owner=owner)
        created = self._batch.create_namespaced_job(namespace=self._s.namespace, body=body)
        # A new Job object under a name an earlier Job may have had: its
        # completion has not been announced yet.
        self._reported.discard(name)
        jobs.append(created)
        # Stopped only after the create succeeded: the in-between run's Job is
        # the record a rollback re-run is decided from, so a failed create
        # must leave it in place for the next pass.
        self._stop(decl, to_stop, jobs, revision)
        run_text = f" run {run_number}" if run_number != 1 else ""
        self._recorder.normal(
            decl.namespace,
            decl.configmap,
            events.REASON_HARNESS_RUN_STARTED,
            f"harness run {name} started for revision {revision}{run_text} on dataset "
            f"{decl.dataset} ({trigger}, report mode: promotion not affected)",
        )

    def _stop(self, decl: HarnessDeclaration, to_stop: list, jobs: list, revision: str) -> None:
        """Delete active runs that revision `revision` replaces; each is superseded."""
        for job in to_stop:
            self._batch.delete_namespaced_job(
                name=job.metadata.name,
                namespace=self._s.namespace,
                propagation_policy="Background",
            )
            jobs.remove(job)
            self._record_superseded(
                decl,
                f"harness run for revision {_labels(job).get(LABEL_REVISION)} "
                f"was stopped: revision {revision} replaced it",
            )

    def job_body(
        self,
        decl: HarnessDeclaration,
        revision: str,
        name: str,
        trigger: str,
        run_number: int = 1,
        owner: dict | None = None,
    ) -> dict:
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
            "HARNESS_RUN_NUMBER": str(run_number),
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
            LABEL_RUN_NUMBER: str(run_number),
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
            # Owned by the chart's catalog ConfigMap: see _owner_ref.
            metadata["ownerReferences"] = [dict(owner)]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata,
            "spec": {
                # A failed run is reported, never retried: a retry would drive
                # the agent again with the same cases.
                "backoffLimit": 0,
                "activeDeadlineSeconds": h.run_timeout,
                "template": {
                    "metadata": {"labels": {**labels, LABEL_JOB: name}},
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

    # ── results ──────────────────────────────────────────────────────────

    def _error_run(self, job, finished_at: float, error: str) -> RunResult:
        labels, ann = _labels(job), _annotations(job)
        try:
            checks = tuple(json.loads(ann.get(ANN_CHECKS, "[]")))
        except ValueError:
            checks = ()
        try:
            threshold = float(ann.get(ANN_THRESHOLD, "0"))
        except ValueError:
            threshold = 0.0
        return summarize(
            namespace=labels.get(LABEL_AGENT_NS, ""),
            agent=labels.get(LABEL_AGENT, ""),
            revision=labels.get(LABEL_REVISION, ""),
            dataset=labels.get(LABEL_DATASET, ""),
            checks=checks,
            threshold=threshold,
            mode=ann.get(ANN_MODE, "report"),
            trigger=ann.get(ANN_TRIGGER, ""),
            judge_model=ann.get(ANN_JUDGE_MODEL, ""),
            judge_key_present=False,
            started_at=_ts(job.metadata.creation_timestamp) or finished_at,
            finished_at=finished_at,
            status="error",
            error=error,
        )

    def _record_superseded(self, decl: HarnessDeclaration | None, message: str) -> None:
        self._metrics.harness_runs.labels(self._s.cluster, "superseded").inc()
        if decl is not None:
            self._recorder.normal(
                decl.namespace, decl.configmap, events.REASON_HARNESS_RUN_SUPERSEDED, message
            )

    def _supersede_finished(self, jobs: list, results: dict, by_key: dict, undecided: set) -> bool:
        """Drop runs that finished after their revision moved, unseen by this process.

        `_maybe_start` stops a run that is still active when a newer revision
        appears. A run can also finish between two passes after the preview
        Service already moved on (live campaign finding S2): its cases may
        have reached the newer pods, so its result is not this revision's.
        A Job this process last saw active, now finished, whose agent's
        preview no longer selects its revision is superseded: deleted with its
        result, counted, an Event, nothing published. If the Rollout cannot be
        read, the decision waits for the next pass (`undecided`).
        """
        ok = True
        for job in list(jobs):
            name = job.metadata.name
            if name not in self._seen_active or _finished_condition(job)[0] is None:
                continue
            labels = _labels(job)
            try:
                rollout = self._custom.get_namespaced_custom_object(
                    "argoproj.io",
                    "v1alpha1",
                    labels.get(LABEL_AGENT_NS, ""),
                    "rollouts",
                    labels.get(LABEL_AGENT, ""),
                )
            except ApiException as exc:
                if exc.status == 404:
                    continue  # the agent is gone; pruning follows its declaration
                logger.error(
                    "harness supersede check for %s failed: %s %s", name, exc.status, exc.reason
                )
                undecided.add(name)
                ok = False
                continue
            status = (rollout or {}).get("status") or {}
            preview = (status.get("blueGreen") or {}).get("previewSelector")
            revision = labels.get(LABEL_REVISION)
            if not preview or preview == revision:
                continue
            self._delete_job(name, results)
            jobs.remove(job)
            self._record_superseded(
                by_key.get(_agent_key(job)),
                f"harness run for revision {revision} was discarded: it finished after "
                f"revision {preview} replaced it in the preview",
            )
        return ok

    def _collect(self, jobs: list, results: dict, by_key: dict) -> list[_Finished]:
        finished: list[_Finished] = []
        for job in jobs:
            state, cond = _finished_condition(job)
            if state is None:
                continue
            finished_at = (
                _ts(getattr(job.status, "completion_time", None))
                or _ts(getattr(cond, "last_transition_time", None))
                or self._clock()
            )
            cm = results.get(result_configmap_name(job.metadata.name))
            foreign = cm is not None and not _owned_by(cm, job)
            if cm is None or foreign:
                # Job names repeat when a Job is deleted and recreated; only a
                # result owned by THIS Job object (same uid) is its result.
                run = self._error_run(
                    job,
                    finished_at,
                    f"the Job finished ({state}) without writing a result"
                    + (
                        "; a result ConfigMap from another Job with this name was ignored"
                        if foreign
                        else ""
                    ),
                )
            else:
                try:
                    run = parse_result(cm.data)
                except ResultSchemaError as exc:
                    run = self._error_run(job, finished_at, f"result not understood: {exc}")
            finished.append(_Finished(job=job, run=run, finished_at=run.finished_at))
            self._report(job, run, by_key.get(_agent_key(job)))
        return finished

    def _report(self, job, run: RunResult, decl: HarnessDeclaration | None) -> None:
        name = job.metadata.name
        if name in self._reported:
            return
        self._reported.add(name)
        result = "completed" if run.status == "completed" else "error"
        self._metrics.harness_runs.labels(self._s.cluster, result).inc()
        if decl is None:
            return
        if run.status == "completed":
            self._recorder.normal(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_COMPLETED,
                _with_failed_ids(
                    f"revision {run.revision}: {run.passed}/{run.cases} passed on "
                    f"{run.dataset} ({run.errors} errors)"
                # A completed run carries an error only when the run deadline
                # cut it short: say so, the ratio covers the judged cases only.
                + (f"; {run.error}" if run.error else "")
                + (
                    "; expectTools in this dataset are not checked yet"
                    if run.expect_tools_ignored
                    else ""
                )
                    + ", report mode: promotion not affected",
                    run,
                ),
            )
        else:
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_FAILED,
                f"revision {run.revision}: harness run errored: {run.error} "
                "(report mode: promotion not affected)",
            )

    def _publish(self, declarations: list, finished: list[_Finished], held: dict) -> None:
        # Series follow the CURRENT declaration: the newest finished run for the
        # declared dataset, shown against the declared threshold. A dataset or
        # threshold change on the same revision starts no run (the Job name
        # ignores the threshold), so threshold_met is recomputed here from the
        # run's pass ratio. An agent whose declaration is invalid (held) has no
        # current declaration and keeps its newest run as it was.
        latest: dict[tuple[str, str | None], _Finished] = {}
        for item in finished:
            agent_key = _agent_key(item.job)
            for key in ((agent_key, item.run.dataset), (agent_key, None)):
                if key not in latest or item.finished_at > latest[key].finished_at:
                    latest[key] = item
        rows: list[HarnessRow] = []
        agents = [(d.key, d.namespace, d.agent, d) for d in declarations]
        agents += [(key, ns, agent, None) for key, (ns, agent) in sorted(held.items())]
        for key, namespace, agent, current in agents:
            item = latest.get((key, current.dataset if current else None))
            if item is None:
                continue  # never run on this dataset: absent, not 0
            run = item.run
            completed = run.status == "completed"
            threshold = current.threshold if current else run.threshold
            threshold_met = None
            if completed and run.pass_ratio is not None:
                threshold_met = run.pass_ratio >= threshold
            rows.append(
                HarnessRow(
                    cluster=self._s.cluster,
                    namespace=namespace,
                    agent=agent,
                    dataset=run.dataset,
                    revision=run.revision,
                    mode=run.mode,
                    judge_model=run.judge_model,
                    trigger=run.trigger,
                    status=run.status,
                    threshold=threshold,
                    last_run_timestamp=item.finished_at,
                    cases=run.cases if completed else None,
                    passed=run.passed if completed else None,
                    failed=run.failed if completed else None,
                    errors=run.errors if completed else None,
                    pass_ratio=run.pass_ratio if completed else None,
                    threshold_met=threshold_met,
                    dataset_digest=run.dataset_digest,
                    checks_digest=run.checks_digest,
                )
            )
        self._metrics.publish_harness(rows)

    # ── history ──────────────────────────────────────────────────────────

    def _delete_job(self, name: str, results: dict) -> None:
        try:
            self._batch.delete_namespaced_job(
                name=name, namespace=self._s.namespace, propagation_policy="Background"
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
        cm_name = result_configmap_name(name)
        if cm_name in results:
            self._delete_result(cm_name)
            results.pop(cm_name, None)
        self._reported.discard(name)

    def _delete_result(self, cm_name: str) -> None:
        try:
            self._core.delete_namespaced_config_map(name=cm_name, namespace=self._s.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _prune(self, jobs: list, results: dict, finished: list[_Finished], present: set) -> None:
        """Remove history of agents whose declaration ConfigMap is gone.

        `present` holds every agent with a declaration ConfigMap, valid or not.
        """
        deleted: set[str] = set()
        for job in list(jobs):
            if _agent_key(job) not in present:
                self._delete_job(job.metadata.name, results)
                deleted.add(job.metadata.name)

        per_agent: dict[str, list[_Finished]] = {}
        for item in finished:
            key = _agent_key(item.job)
            if key in present:
                per_agent.setdefault(key, []).append(item)
        for items in per_agent.values():
            items.sort(key=lambda i: i.finished_at, reverse=True)
            for item in items[self._h.history_limit :]:
                self._delete_job(item.job.metadata.name, results)
                deleted.add(item.job.metadata.name)

        # Result ConfigMaps whose Job is gone (a delete that half-failed).
        live = {j.metadata.name for j in jobs} - deleted
        for cm_name, cm in list(results.items()):
            job_name = (getattr(cm.metadata, "labels", None) or {}).get(LABEL_JOB)
            if job_name and job_name not in live:
                self._delete_result(cm_name)
                results.pop(cm_name, None)
        for name in deleted:
            jobs[:] = [j for j in jobs if j.metadata.name != name]
