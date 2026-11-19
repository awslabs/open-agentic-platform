"""The harness reader: publishes metrics and Events from finished AnalysisRuns.

Argo Rollouts decides when a revision is scored: it creates one AnalysisRun per
revision, ends it when a newer revision arrives, and keeps a history. The
verdict endpoint (harness_api) starts the runner Job when the run polls it. What
only this process can do is turn the results into oap_agent_harness_* series and
Events on the agent's `<agent>-harness` ConfigMap, and clean up the Jobs.

Per pass:

1. Read the judge-key Secret and publish whether it is there. A missing required
   key fails the pass: the endpoint answers `error` and starts no run.
2. Read the `<agent>-harness` declarations and the AnalysisRuns labelled
   `harness.oap.io/agent` (all namespaces), the platform's Jobs and results.
3. A run with `spec.terminate: true` was ended by Rollouts (a newer revision):
   it is superseded, recorded once, nothing published. For every other finished
   run, the verdict is read from the metric measurement, never from the run
   phase: a failed `dryRun` metric leaves the run `Successful`.
4. Per agent, the finished run with the highest revision number is published,
   whatever the Rollout is doing now, so the series of revision N-1 stay while
   the run for N is going.
5. For each gate agent, read the Rollout: when its stable revision has harness
   runs and none passed, publish oap_agent_harness_unverified_stable and write
   one HarnessStableRevisionUnverified Warning per (Rollout uid, hash).
6. Delete Jobs and results of agents that no longer declare a harness, of runs
   that are older than the published one, and of finished runs whose AnalysisRun
   Rollouts has pruned.

State is the cluster's: the only memory kept here is the set of AnalysisRun
uids (and unverified stable hashes) already recorded, so a finished run (listed until Rollouts prunes it) is
announced once per process. A restart repeats the Events of the runs still
listed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from kubernetes.client.rest import ApiException

from . import events
from .harness_declarations import HarnessDeclaration, HarnessDeclarationError
from .harness_job import (
    ANN_CHECKS,
    ANN_JUDGE_MODEL,
    ANN_MODE,
    ANN_THRESHOLD,
    ANN_TRIGGER,
    LABEL_AGENT,
    LABEL_AGENT_NS,
    LABEL_DATASET,
    LABEL_JOB,
    LABEL_RESULT,
    LABEL_REVISION,
    LABEL_REVISION_NUMBER,
    LABEL_RUN,
    _agent_key,
    _annotations,
    _finished_condition,
    _labels,
    _owned_by,
    _ts,
    _with_failed_ids,
)
from .harness_results import ResultSchemaError, RunResult, parse_result, summarize
from .metrics import HarnessRow, Metrics
from .naming import result_configmap_name

logger = logging.getLogger(__name__)

METRIC_NAME = "oap-harness"
ROLLOUT_REVISION_ANNOTATION = "rollout.argoproj.io/revision"
FINISHED_PHASES = ("Successful", "Failed", "Error", "Inconclusive")
# Argo Rollouts labels each AnalysisRun with the pod-template hash it scores.
POD_HASH_LABEL = "rollouts-pod-template-hash"
_RUNNING = "running"


@dataclass
class _Run:
    """One finished AnalysisRun, with the verdict read from its metric."""

    uid: str
    namespace: str
    agent: str
    revision_number: int
    state: str | None  # pass, fail, insufficient, error; None when no verdict arrived
    message: str
    job: str | None
    finished_at: float

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.agent}"


@dataclass
class _Finished:
    key: str
    revision_number: int
    run: RunResult
    finished_at: float
    job: str | None


def _phase_of(obj) -> str:
    return str(((obj or {}).get("status") or {}).get("phase") or "")


def _verdict_of(analysis_run: dict) -> tuple[str | None, str, str | None]:
    """(state, message, job) from the harness metric's last answer.

    The web provider stores the endpoint's JSON body as the measurement value.
    The last measurement that is not `pending` is the run's answer. No such
    measurement (the endpoint was unreachable, or the run ran out of polls)
    means no verdict.
    """
    for result in ((analysis_run.get("status") or {}).get("metricResults")) or []:
        if result.get("name") != METRIC_NAME:
            continue
        for measurement in reversed(result.get("measurements") or []):
            try:
                body = json.loads(measurement.get("value") or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(body, dict):
                continue
            state = body.get("state")
            if state and state != "pending":
                return str(state), str(body.get("message", "")), body.get("job")
        # Pending to the end of the run, or errors only.
        for measurement in reversed(result.get("measurements") or []):
            try:
                body = json.loads(measurement.get("value") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(body, dict) and body.get("job"):
                return None, "the run ended with no verdict", body.get("job")
    return None, "the run ended with no verdict", None


class HarnessLoop:
    def __init__(
        self,
        settings,
        harness_settings,
        metrics: Metrics,
        reader,
        recorder,
        batch_api,
        custom_api,
        core_api,
    ):
        self._s = settings
        self._h = harness_settings
        self._metrics = metrics
        self._reader = reader
        self._recorder = recorder
        self._batch = batch_api
        self._custom = custom_api
        self._core = core_api
        # AnalysisRun uids already recorded (Event and counter), per process.
        self._reported: set[str] = set()
        # (Rollout uid, stable pod hash) already reported as unverified, per
        # process: the same once-only rule as _reported.
        self._unverified_reported: set[tuple[str, str]] = set()

    # ── the pass ─────────────────────────────────────────────────────────

    def run_once(self) -> bool:
        """One pass. A pass that caught an error counts toward errors_total."""
        ok = self._pass()
        if not ok:
            self._metrics.errors.labels(self._s.cluster, "harness").inc()
        return ok

    def _judge_key_present(self) -> bool | None:
        """True or False; None when the Secret cannot be read."""
        try:
            secret = self._core.read_namespaced_secret(
                name=self._h.judge_key_secret, namespace=self._s.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return False
            logger.warning("cannot read the harness judge key Secret: %s", exc.status)
            return None
        return bool(getattr(secret, "data", None))

    def _pass(self) -> bool:
        ok = True
        cluster = self._s.cluster
        ns = self._s.namespace

        present = self._judge_key_present()
        self._metrics.harness_judge_key_present.labels(cluster).set(1 if present else 0)
        if present is None:
            ok = False
        elif not present and self._h.judge_key_required:
            # The endpoint answers `error` and writes the Event on the agent;
            # here the pass fails so AgentHarnessReconcileErrors can fire.
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
        # An agent whose declaration is invalid keeps its history and series.
        held = {}
        for cm, _ in malformed:
            agent = (getattr(cm.metadata, "labels", None) or {}).get(LABEL_AGENT, "")
            key = f"{cm.metadata.namespace}/{agent}"
            if agent and key not in by_key:
                held[key] = (cm.metadata.namespace, agent)

        try:
            analysis_runs = self._list_analysis_runs()
            jobs = {
                j.metadata.name: j
                for j in self._batch.list_namespaced_job(
                    namespace=ns, label_selector=f"{LABEL_RUN}=true"
                ).items
            }
            results = {
                cm.metadata.name: cm
                for cm in self._core.list_namespaced_config_map(
                    namespace=ns, label_selector=f"{LABEL_RESULT}=true"
                ).items
            }
        except ApiException as exc:
            logger.error("harness: listing failed: %s %s", exc.status, exc.reason)
            return False

        finished: list[_Finished] = []
        known_revisions: set[tuple[str, int]] = set()
        # (agent key, pod hash) -> what each of its runs ended with: a verdict
        # state, None (no verdict, including runs Rollouts terminated), or
        # _RUNNING.
        by_hash: dict[tuple[str, str], list[str | None]] = {}
        for item in analysis_runs:
            meta = item.get("metadata") or {}
            labels = meta.get("labels") or {}
            agent = labels.get(LABEL_AGENT, "")
            namespace = meta.get("namespace", "")
            number = _number(meta.get("annotations"))
            uid = str(meta.get("uid") or meta.get("name") or "")
            if not agent or not namespace or not uid:
                continue
            known_revisions.add((f"{namespace}/{agent}", number))
            decl = by_key.get(f"{namespace}/{agent}")
            states = by_hash.setdefault((f"{namespace}/{agent}", _hash_of(item)), [])
            if (item.get("spec") or {}).get("terminate"):
                states.append(_verdict_of(item)[0])
                self._superseded(uid, decl, number)
                self._delete_run_jobs(f"{namespace}/{agent}", number, jobs, results)
                continue
            if _phase_of(item) not in FINISHED_PHASES:
                states.append(_RUNNING)
                continue
            state, message, job = _verdict_of(item)
            states.append(state)
            run = _Run(
                uid=uid,
                namespace=namespace,
                agent=agent,
                revision_number=number,
                state=state,
                message=message,
                job=job,
                finished_at=_ts(((item.get("status") or {}).get("startedAt"))) or 0.0,
            )
            parsed = self._collect(run, jobs, results)
            self._report(run, parsed, decl)
            if parsed is not None:
                finished.append(parsed)

        self._publish(declarations, finished, held)
        if not self._check_stable(declarations, by_hash):
            ok = False
        try:
            self._prune(jobs, results, finished, set(by_key) | set(held), known_revisions)
        except ApiException as exc:
            ok = False
            logger.error("harness prune failed: %s %s", exc.status, exc.reason)
        return ok

    def _list_analysis_runs(self) -> list[dict]:
        listing = self._custom.list_cluster_custom_object(
            "argoproj.io", "v1alpha1", "analysisruns", label_selector=LABEL_AGENT
        )
        return list((listing or {}).get("items") or [])

    # ── reading a run ────────────────────────────────────────────────────

    def _collect(self, run: _Run, jobs: dict, results: dict) -> _Finished | None:
        job = jobs.get(run.job) if run.job else None
        if job is None:
            return None  # pruned, or no Job ever started: nothing to publish
        state, cond = _finished_condition(job)
        if state is None:
            return None  # the run ended while its Job still runs: no result yet
        finished_at = (
            _ts(getattr(job.status, "completion_time", None))
            or _ts(getattr(cond, "last_transition_time", None))
            or run.finished_at
        )
        cm = results.get(result_configmap_name(job.metadata.name))
        foreign = cm is not None and not _owned_by(cm, job)
        if cm is None or foreign:
            parsed = self._error_run(
                job,
                finished_at,
                f"the Job finished ({state}) without writing a result"
                + ("; a result ConfigMap from another Job with this name was ignored" if foreign else ""),
            )
        else:
            try:
                parsed = parse_result(cm.data)
            except ResultSchemaError as exc:
                parsed = self._error_run(job, finished_at, f"result not understood: {exc}")
        return _Finished(
            key=run.key,
            revision_number=run.revision_number,
            run=parsed,
            finished_at=parsed.finished_at,
            job=run.job,
        )

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

    # ── Events and counters, once per AnalysisRun ────────────────────────

    def _superseded(self, uid: str, decl: HarnessDeclaration | None, number: int) -> None:
        if uid in self._reported:
            return
        self._reported.add(uid)
        self._metrics.harness_runs.labels(self._s.cluster, "superseded").inc()
        if decl is not None:
            self._recorder.normal(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_SUPERSEDED,
                f"harness run for revision number {number} was ended by Argo Rollouts: "
                "a newer revision replaced it",
            )

    def _report(self, run: _Run, parsed: _Finished | None, decl: HarnessDeclaration | None) -> None:
        if run.uid in self._reported:
            return
        self._reported.add(run.uid)
        mode = parsed.run.mode if parsed else (decl.mode if decl else "report")
        note = "gate mode" if mode == "gate" else "report mode: promotion not affected"
        if parsed is None and run.state in (None, "error"):
            # No Job result to show: a run that ended with no verdict, or the
            # endpoint refused (declaration, key, preview, not current).
            self._metrics.harness_runs.labels(self._s.cluster, "error").inc()
            if decl is not None:
                self._recorder.warn(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_HARNESS_RUN_FAILED,
                    f"revision number {run.revision_number}: harness run errored: "
                    f"{run.message} ({note})",
                )
                self._gate_event(decl, mode, run)
            return
        if parsed is None:
            return  # a finished run whose Job is already pruned: history, not news
        result = parsed.run
        counted = result.status if result.status in ("completed", "insufficient") else "error"
        self._metrics.harness_runs.labels(self._s.cluster, counted).inc()
        if decl is None:
            return
        if result.status in ("completed", "insufficient"):
            text = (
                f"revision {result.revision}: {result.passed}/{result.cases} passed on "
                f"{result.dataset} ({result.errors} errors)"
                # A completed run carries an error only when the run deadline cut
                # it short: the ratio covers the judged cases only.
                + (f"; {result.error}" if result.error else "")
                + (
                    "; expectTools in this dataset are not checked yet"
                    if result.expect_tools_ignored
                    else ""
                )
                + f", {note}"
            )
            self._recorder.normal(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_COMPLETED,
                _with_failed_ids(text, result),
            )
        else:
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_RUN_FAILED,
                f"revision {result.revision}: harness run errored: {result.error} ({note})",
            )
        self._gate_event(decl, mode, run)

    def _gate_event(self, decl: HarnessDeclaration, mode: str, run: _Run) -> None:
        if mode == "gate" and run.state != "pass":
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_GATE_ABORTED,
                f"revision number {run.revision_number}: the gate did not pass "
                f"({run.state or 'no verdict'}: {run.message}); Argo Rollouts aborted the revision",
            )

    # ── gate: a stable revision with no pass ─────────────────────────────

    def _check_stable(self, declarations: list, by_hash: dict) -> bool:
        """Flag gate agents whose stable revision has runs and none passed.

        Argo Rollouts v1.9 can promote a revision without a passing
        pre-promotion run: it fast-tracks any ReplicaSet that still carries a
        scale-down deadline, which a superseded preview does for
        scaleDownDelaySeconds. A stable hash with no run at all is not flagged:
        a revision promoted before the harness was enabled looks the same.
        """
        ok = True
        rows: list[tuple[str, str, str, str]] = []
        for d in declarations:
            if d.mode != "gate":
                continue
            try:
                rollout = self._custom.get_namespaced_custom_object(
                    "argoproj.io", "v1alpha1", d.namespace, "rollouts", d.agent
                )
            except ApiException as exc:
                if exc.status != 404:
                    ok = False
                    logger.warning(
                        "harness: cannot read Rollout %s/%s: %s", d.namespace, d.agent, exc.status
                    )
                continue
            stable = str(((rollout or {}).get("status") or {}).get("stableRS") or "")
            states = by_hash.get((d.key, stable)) if stable else None
            if not states or "pass" in states or _RUNNING in states:
                continue
            rows.append((self._s.cluster, d.namespace, d.agent, stable))
            uid = str(((rollout or {}).get("metadata") or {}).get("uid") or d.key)
            if (uid, stable) in self._unverified_reported:
                continue
            self._unverified_reported.add((uid, stable))
            seen = ", ".join(sorted({s or "no verdict" for s in states}))
            self._recorder.warn(
                d.namespace,
                d.configmap,
                events.REASON_HARNESS_STABLE_UNVERIFIED,
                f"stable revision {stable} of Rollout {d.agent} has {len(states)} harness "
                f"run(s) and none passed ({seen}): it became stable without a pass. "
                "Check how it was promoted, and roll back if it should not serve.",
            )
        self._metrics.publish_unverified_stable(rows)
        return ok

    # ── metrics ──────────────────────────────────────────────────────────

    def _publish(self, declarations: list, finished: list[_Finished], held: dict) -> None:
        # The highest revision number wins; series follow the CURRENT declaration
        # (dataset and threshold). An invalid declaration (held) keeps the newest
        # run as it was.
        latest: dict[tuple[str, str | None], _Finished] = {}
        for item in finished:
            for key in ((item.key, item.run.dataset), (item.key, None)):
                best = latest.get(key)
                if best is None or (item.revision_number, item.finished_at) > (
                    best.revision_number,
                    best.finished_at,
                ):
                    latest[key] = item
        rows: list[HarnessRow] = []
        agents = [(d.key, d.namespace, d.agent, d) for d in declarations]
        agents += [(key, ns, agent, None) for key, (ns, agent) in sorted(held.items())]
        for key, namespace, agent, current in agents:
            item = latest.get((key, current.dataset if current else None))
            if item is None:
                continue  # never run on this dataset: absent, not 0
            run = item.run
            completed = run.status in ("completed", "insufficient")
            threshold = current.threshold if current else run.threshold
            threshold_met = None
            if run.status == "completed" and run.pass_ratio is not None:
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
                    pass_ratio=run.pass_ratio if run.status == "completed" else None,
                    threshold_met=threshold_met,
                    dataset_digest=run.dataset_digest,
                    checks_digest=run.checks_digest,
                )
            )
        self._metrics.publish_harness(rows)

    # ── cleanup ──────────────────────────────────────────────────────────

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

    def _delete_result(self, cm_name: str) -> None:
        try:
            self._core.delete_namespaced_config_map(name=cm_name, namespace=self._s.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _delete_run_jobs(self, key: str, number: int, jobs: dict, results: dict) -> None:
        """A terminated run's Job: its cases may have reached the newer pods."""
        for name, job in list(jobs.items()):
            if _agent_key(job) == key and _labels(job).get(LABEL_REVISION_NUMBER) == str(number):
                try:
                    self._delete_job(name, results)
                except ApiException as exc:
                    logger.error("harness delete failed: %s %s", exc.status, exc.reason)
                    continue
                jobs.pop(name, None)

    def _prune(
        self,
        jobs: dict,
        results: dict,
        finished: list[_Finished],
        present: set,
        known: set,
    ) -> None:
        """Delete Jobs (and results) nothing publishes or waits for any more.

        Kept: the Job of the published (highest finished) run of each agent, and
        every Job of a higher revision. Deleted: Jobs of agents with no
        declaration, Jobs of lower revisions, active Jobs below the newest
        revision of the agent, and finished Jobs whose AnalysisRun Rollouts has
        pruned.
        """
        published: dict[str, _Finished] = {}
        for item in finished:
            best = published.get(item.key)
            if best is None or (item.revision_number, item.finished_at) > (
                best.revision_number,
                best.finished_at,
            ):
                published[item.key] = item
        newest: dict[str, int] = {}
        for job in jobs.values():
            key = _agent_key(job)
            newest[key] = max(newest.get(key, 0), _job_number(job))
        for name, job in list(jobs.items()):
            key = _agent_key(job)
            number = _job_number(job)
            keep_item = published.get(key)
            remove = False
            if key not in present:
                remove = True
            elif keep_item is not None and keep_item.job == name:
                remove = False
            elif keep_item is not None and number < keep_item.revision_number:
                remove = True
            elif number < newest.get(key, 0):
                remove = True
            elif _finished_condition(job)[0] is not None and (key, number) not in known:
                remove = True
            if remove:
                self._delete_job(name, results)
                jobs.pop(name, None)
        # Result ConfigMaps whose Job is gone (a delete that half-failed).
        for cm_name, cm in list(results.items()):
            job_name = (getattr(cm.metadata, "labels", None) or {}).get(LABEL_JOB)
            if job_name and job_name not in jobs:
                self._delete_result(cm_name)
                results.pop(cm_name, None)


def _hash_of(analysis_run: dict) -> str:
    """The pod-template hash a run scores: Rollouts' label, else the hash arg."""
    meta = analysis_run.get("metadata") or {}
    label = (meta.get("labels") or {}).get(POD_HASH_LABEL)
    if label:
        return str(label)
    for arg in (analysis_run.get("spec") or {}).get("args") or []:
        if arg.get("name") == "hash" and arg.get("value"):
            return str(arg["value"])
    return ""


def _number(annotations) -> int:
    try:
        return int((annotations or {}).get(ROLLOUT_REVISION_ANNOTATION, ""))
    except ValueError:
        return 0


def _job_number(job) -> int:
    try:
        return int(_labels(job).get(LABEL_REVISION_NUMBER, "0"))
    except ValueError:
        return 0
