"""The harness loop: one platform-namespace Job per agent revision (report mode).

Per pass:

1. Make sure the harness judge virtual key exists (minted in this cluster's
   Bifrost) and is in the Secret the Jobs mount. Fail closed: while a required
   key is unavailable, no new Job is created.
2. Read the `<agent>-harness` declarations.
3. For each, read the agent's Rollout. A run starts when the preview Service
   already selects the newest ReplicaSet (`blueGreen.previewSelector ==
   currentPodHash`). The very first revision of an agent runs too, as the
   baseline (`trigger=first-revision`).
4. Collect finished Jobs' result ConfigMaps and publish oap_agent_harness_* for
   each agent's latest finished run.
5. Prune history.

The Job objects ARE the state. Job names are deterministic per (namespace,
agent, revision, dataset), so a second pass or a restarted reconciler that
finds the Job creates nothing: a revision runs once. Jobs always live in the
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
ANN_TRIGGER = "harness.oap.io/trigger"
ANN_THRESHOLD = "harness.oap.io/threshold"
ANN_CHECKS = "harness.oap.io/checks"
ANN_MODE = "harness.oap.io/mode"
ANN_JUDGE_MODEL = "harness.oap.io/judge-model"
# On the judge-key Secret: the Bifrost key name its value belongs to.
ANN_JUDGE_KEY_NAME = "harness.oap.io/judge-key-name"

TRIGGER_FIRST = "first-revision"
TRIGGER_NEW = "new-revision"


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


def _finished_condition(job):
    """'Complete' or 'Failed' with its condition, or (None, None) while active."""
    status = getattr(job, "status", None)
    for cond in getattr(status, "conditions", None) or []:
        if cond.type in ("Complete", "Failed") and str(cond.status) == "True":
            return cond.type, cond
    return None, None


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

    # ── judge model and key ──────────────────────────────────────────────

    @property
    def judge_model(self) -> str:
        return self._catalog.judge_model(self._h.judge_alias or self._catalog.judge_default)

    def _secret_state(self) -> tuple[str | None, str]:
        """(value, key name) in the Secret; value is None when the Secret does not exist."""
        try:
            secret = self._core.read_namespaced_secret(
                name=self._h.judge_key_secret, namespace=self._s.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return None, ""
            raise
        meta = getattr(secret, "metadata", None)
        key_name = str((getattr(meta, "annotations", None) or {}).get(ANN_JUDGE_KEY_NAME, ""))
        raw = (getattr(secret, "data", None) or {}).get(JUDGE_KEY_FILE, "")
        try:
            value = base64.b64decode(raw).decode("utf-8") if raw else ""
        except (ValueError, UnicodeDecodeError):
            value = ""
        return value, key_name

    def _secret_value(self) -> str | None:
        """Current key value in the Secret; None when the Secret does not exist."""
        return self._secret_state()[0]

    def _write_secret(self, value: str, key_name: str) -> None:
        current, current_name = self._secret_state()
        if current == value and current_name == key_name:
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

    def _ensure_judge_key(self) -> bool:
        if not self._h.judge_key_mint:
            try:
                return bool(self._secret_value())
            except ApiException as exc:
                logger.warning("cannot read harness judge key Secret: %s", exc.status)
                return False
        if self._bifrost is None:
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
            current, current_name = self._secret_state()
            known = (current_name, current) if current and current_name else None
            value = self._bifrost.ensure_judge_key(*config, known=known)
            self._write_secret(value, vk_name(*config))
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

        key_ok = self._ensure_judge_key()
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

        jobs = list(
            self._batch.list_namespaced_job(
                namespace=ns, label_selector=f"{LABEL_RUN}=true"
            ).items
        )
        results = {
            cm.metadata.name: cm
            for cm in self._core.list_namespaced_config_map(
                namespace=ns, label_selector=f"{LABEL_RESULT}=true"
            ).items
        }

        available = available_datasets(self._h.datasets_dir)
        for decl in declarations:
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
                self._maybe_start(decl, jobs, available)
            except ApiException as exc:
                ok = False
                logger.error(
                    "harness: %s failed: %s %s", decl.key, exc.status, exc.reason
                )

        finished = self._collect(jobs, results, by_key)
        self._publish(declarations, finished)
        try:
            self._prune(jobs, results, finished, by_key)
        except ApiException as exc:
            ok = False
            logger.error("harness prune failed: %s %s", exc.status, exc.reason)
        return ok

    def _maybe_start(self, decl: HarnessDeclaration, jobs: list, available: set) -> None:
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

        name = harness_job_name(decl.namespace, decl.agent, revision, decl.dataset)
        if any(j.metadata.name == name for j in jobs):
            return  # this revision already ran or is running

        agent_jobs = [j for j in jobs if _agent_key(j) == decl.key]
        for job in agent_jobs:
            state, _ = _finished_condition(job)
            if state is None and _labels(job).get(LABEL_REVISION) != revision:
                self._batch.delete_namespaced_job(
                    name=job.metadata.name,
                    namespace=self._s.namespace,
                    propagation_policy="Background",
                )
                jobs.remove(job)
                self._metrics.harness_runs.labels(self._s.cluster, "superseded").inc()
                self._recorder.normal(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_HARNESS_RUN_SUPERSEDED,
                    f"harness run for revision {_labels(job).get(LABEL_REVISION)} "
                    f"was stopped: revision {revision} replaced it",
                )

        active = sum(1 for j in jobs if _finished_condition(j)[0] is None)
        if active >= self._h.max_concurrent_jobs:
            logger.info(
                "harness: %s revision %s waits, %d runs active", decl.key, revision, active
            )
            return

        trigger = TRIGGER_FIRST if not agent_jobs else TRIGGER_NEW
        body = self.job_body(decl, revision, name, trigger)
        created = self._batch.create_namespaced_job(namespace=self._s.namespace, body=body)
        jobs.append(created)
        self._recorder.normal(
            decl.namespace,
            decl.configmap,
            events.REASON_HARNESS_RUN_STARTED,
            f"harness run {name} started for revision {revision} on dataset "
            f"{decl.dataset} ({trigger}, report mode: promotion not affected)",
        )

    def job_body(self, decl: HarnessDeclaration, revision: str, name: str, trigger: str) -> dict:
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
        labels = {
            LABEL_RUN: "true",
            LABEL_AGENT_NS: decl.namespace,
            LABEL_AGENT: decl.agent,
            LABEL_REVISION: revision,
            LABEL_DATASET: decl.dataset,
            "app.kubernetes.io/managed-by": MANAGED_BY,
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
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
            },
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
                                    "optional": True,
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
            if cm is None:
                run = self._error_run(
                    job, finished_at, f"the Job finished ({state}) without writing a result"
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
                f"revision {run.revision}: {run.passed}/{run.cases} passed on "
                f"{run.dataset} ({run.errors} errors), report mode: promotion not affected",
            )
        else:
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_FAILED,
                f"revision {run.revision}: harness run errored: {run.error} "
                "(report mode: promotion not affected)",
            )

    def _publish(self, declarations: list, finished: list[_Finished]) -> None:
        latest: dict[str, _Finished] = {}
        for item in finished:
            key = _agent_key(item.job)
            if key not in latest or item.finished_at > latest[key].finished_at:
                latest[key] = item
        rows: list[HarnessRow] = []
        for decl in declarations:
            item = latest.get(decl.key)
            if item is None:
                continue  # never run: absent, not 0
            run = item.run
            completed = run.status == "completed"
            rows.append(
                HarnessRow(
                    cluster=self._s.cluster,
                    namespace=decl.namespace,
                    agent=decl.agent,
                    dataset=run.dataset,
                    revision=run.revision,
                    mode=run.mode,
                    judge_model=run.judge_model,
                    trigger=run.trigger,
                    status=run.status,
                    threshold=run.threshold,
                    last_run_timestamp=item.finished_at,
                    cases=run.cases if completed else None,
                    passed=run.passed if completed else None,
                    failed=run.failed if completed else None,
                    errors=run.errors if completed else None,
                    pass_ratio=run.pass_ratio if completed else None,
                    threshold_met=run.threshold_met if completed else None,
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

    def _prune(self, jobs: list, results: dict, finished: list[_Finished], by_key: dict) -> None:
        deleted: set[str] = set()
        for job in list(jobs):
            if _agent_key(job) not in by_key:
                self._delete_job(job.metadata.name, results)
                deleted.add(job.metadata.name)

        per_agent: dict[str, list[_Finished]] = {}
        for item in finished:
            key = _agent_key(item.job)
            if key in by_key:
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
