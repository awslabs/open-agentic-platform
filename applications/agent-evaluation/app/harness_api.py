"""The harness verdict endpoint: the web provider of the harness AnalysisTemplate.

Argo Rollouts creates one AnalysisRun per revision and polls this endpoint
(POST /v1/verdict) every 15 seconds. The first poll for a revision creates the
runner Job in the evaluation namespace; later polls return its state. The
answer is one of pending, pass, fail, insufficient, error. The AnalysisTemplate
maps them to success, failure or inconclusive, and Rollouts promotes or aborts.

Why an endpoint and not the `job` provider: that provider runs its Job in the
agent's namespace, which would put the judge key and the datasets into team
namespaces. Design: docs/architecture/agent-harness.md, decisions D2, D3, D6.

Rules this module keeps:
- The caller is not trusted beyond its arguments. The Rollout is read first and
  any mismatch answers "not the current revision" with no side effect: no
  declaration read, no Job created or deleted, no Event written.
- The dataset, checks, threshold, mode and target come from the agent's
  declaration, never from the request.
- Every step is idempotent, so a repeated poll is safe. One process-wide lock
  covers the Job lookup, the creation and the slot count.
- The endpoint owns the clock: past run timeout plus slot wait it answers
  `error`, so a run of `pending` answers can never end as a pass.
- Nothing returned or logged carries the judge key.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from kubernetes.client.rest import ApiException

from . import events
from .datasets import available_datasets
from .harness_config import HarnessSettings
from .harness_declarations import (
    DNS1035_LABEL_RE,
    DNS1123_LABEL_RE,
    MAX_LABEL_LEN,
    PREVIEW_SUFFIX,
)
from .harness_job import (
    LABEL_AGENT,
    LABEL_AGENT_NS,
    LABEL_REVISION_NUMBER,
    LABEL_RUN,
    POD_TEMPLATE_HASH_LABEL,
    JobFactory,
    _finished_condition,
    _labels,
    _owned_by,
    _ts,
)
from .harness_results import ResultSchemaError, parse_result
from .naming import harness_job_name, result_configmap_name

logger = logging.getLogger(__name__)

PATH = "/v1/verdict"
MAX_QUERY_BYTES = 512
EXPECTED_PARAMS = ("namespace", "agent", "revision", "hash")
HASH_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
REVISION_RE = re.compile(r"^[0-9]{1,9}$")
ROLLOUT_REVISION_ANNOTATION = "rollout.argoproj.io/revision"
# One fixed message for every step 2 failure, so a caller learns nothing about
# another team's agent.
NOT_CURRENT = "not the current revision"
MAX_MESSAGE_CHARS = 300


class InvalidRequest(Exception):
    """The request does not meet the input rules: HTTP 400."""


def _answer(state: str, message: str, job: str | None = None) -> dict:
    body = {"state": state, "message": message[:MAX_MESSAGE_CHARS]}
    if job:
        body["job"] = job
    return body


def validate(query: str) -> dict[str, str]:
    """The four arguments, or InvalidRequest. Unknown parameters are rejected."""
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise InvalidRequest("query string is too long")
    try:
        params = parse_qs(query, keep_blank_values=True, strict_parsing=bool(query))
    except ValueError as exc:
        raise InvalidRequest("malformed query string") from exc
    unknown = sorted(set(params) - set(EXPECTED_PARAMS))
    if unknown:
        raise InvalidRequest(f"unknown parameter {unknown[0]!r}")
    values: dict[str, str] = {}
    for key in EXPECTED_PARAMS:
        got = params.get(key)
        if not got or len(got) != 1 or not got[0]:
            raise InvalidRequest(f"{key} is required exactly once")
        values[key] = got[0]
    ns, agent = values["namespace"], values["agent"]
    if len(ns) > MAX_LABEL_LEN or not DNS1123_LABEL_RE.match(ns):
        raise InvalidRequest("namespace is not a DNS-1123 label")
    preview = agent + PREVIEW_SUFFIX
    if (
        len(agent) > MAX_LABEL_LEN
        or not DNS1123_LABEL_RE.match(agent)
        or len(preview) > MAX_LABEL_LEN
        or not DNS1035_LABEL_RE.match(preview)
    ):
        raise InvalidRequest("agent is not a valid agent name")
    if not REVISION_RE.match(values["revision"]):
        raise InvalidRequest("revision is not a decimal integer")
    if len(values["hash"]) > MAX_LABEL_LEN or not HASH_RE.match(values["hash"]):
        raise InvalidRequest("hash is not a valid pod-template hash")
    return values


class VerdictService:
    """The handler logic, apart from HTTP so tests drive it with the fakes."""

    def __init__(
        self,
        settings,
        harness_settings: HarnessSettings,
        jobs: JobFactory,
        reader,
        recorder,
        batch_api,
        custom_api,
        core_api,
        clock=time.time,
    ):
        self._s = settings
        self._h = harness_settings
        self._jobs = jobs
        self._reader = reader
        self._recorder = recorder
        self._batch = batch_api
        self._custom = custom_api
        self._core = core_api
        self._clock = clock
        self._lock = threading.Lock()

    # ── steps ────────────────────────────────────────────────────────────

    def verdict(self, namespace: str, agent: str, revision: str, pod_hash: str) -> dict:
        # Step 2: the Rollout, before anything else.
        try:
            rollout = self._custom.get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", namespace, "rollouts", agent
            )
        except ApiException as exc:
            if exc.status == 404:
                return _answer("error", NOT_CURRENT)
            raise
        current = _revision_number(rollout)
        if current is None or int(revision) > current:
            return _answer("error", NOT_CURRENT)
        if int(revision) < current:
            # Rollouts ends this run itself after a second push. An error here
            # could fail the old run before it is terminated.
            return _answer("pending", "a newer revision exists; this run is being replaced")
        if ((rollout.get("status") or {}).get("currentPodHash")) != pod_hash:
            return _answer("error", NOT_CURRENT)

        # Step 3: the declaration, the dataset, the preview Service.
        decl, cm, error = self._reader.read_one(namespace, agent)
        if cm is None and decl is None:
            return _answer("error", "no harness declaration for this agent")
        if decl is None:
            self._recorder.warn(
                namespace, cm.metadata.name, events.REASON_INVALID_HARNESS_DECLARATION, error
            )
            return _answer("error", "the harness declaration is invalid")
        available = available_datasets(self._h.datasets_dir)
        if decl.dataset not in available:
            self._recorder.warn(
                namespace,
                decl.configmap,
                events.REASON_UNKNOWN_DATASET,
                f"dataset {decl.dataset!r} is not in the platform catalog {sorted(available)}",
            )
            return _answer("error", f"dataset {decl.dataset} is not in the platform catalog")
        refusal = self._preview_refusal(decl, pod_hash)
        if refusal:
            return _answer("error", refusal)

        # Step 4: the Job, by its name.
        name = harness_job_name(namespace, agent, revision, pod_hash, decl.dataset)
        with self._lock:
            return self._advance(decl, rollout, revision, pod_hash, name)

    def _preview_refusal(self, decl, pod_hash: str) -> str | None:
        try:
            service = self._core.read_namespaced_service(
                name=decl.preview_service, namespace=decl.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                self._recorder.normal(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_HARNESS_WAITING_FOR_PREVIEW,
                    f"no Service named {decl.preview_service} in {decl.namespace}",
                )
                return f"no Service named {decl.preview_service}"
            raise
        spec = getattr(service, "spec", None)
        if getattr(spec, "type", None) == "ExternalName":
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_PREVIEW_UNSUPPORTED,
                f"Service {decl.preview_service} is of type ExternalName: the harness "
                "only targets the blue-green preview Service of the Rollout and starts no run",
            )
            return "the preview Service is of type ExternalName"
        if (getattr(spec, "selector", None) or {}).get(POD_TEMPLATE_HASH_LABEL) != pod_hash:
            return "the preview Service does not select this revision"
        return None

    def _advance(self, decl, rollout, revision: str, pod_hash: str, name: str) -> dict:
        ns = self._s.namespace
        jobs = list(
            self._batch.list_namespaced_job(namespace=ns, label_selector=f"{LABEL_RUN}=true").items
        )
        job = next((j for j in jobs if j.metadata.name == name), None)
        if job is None:
            job = self._create(decl, rollout, revision, pod_hash, name, jobs)
            if isinstance(job, dict):
                return job  # an answer, not a Job
        state, cond = _finished_condition(job)
        age = self._clock() - (_ts(getattr(job.metadata, "creation_timestamp", None)) or self._clock())
        cap = self._h.run_timeout + self._h.slot_wait_seconds
        if state == "Failed":
            reason = getattr(cond, "reason", None) or "Failed"
            return _answer("error", f"the runner Job failed: {reason}", name)
        if state == "Complete":
            return self._complete(decl, job, pod_hash, name)
        suspended = bool(getattr(getattr(job, "spec", None), "suspend", False))
        if age > cap:
            if suspended:
                self._delete(name)
                return _answer("error", "no slot in time", name)
            return _answer("error", "the run exceeded its total time limit", name)
        if suspended:
            active = sum(
                1
                for j in jobs
                if _finished_condition(j)[0] is None
                and not bool(getattr(getattr(j, "spec", None), "suspend", False))
            )
            if active < self._h.max_concurrent_jobs:
                self._batch.patch_namespaced_job(
                    name=name, namespace=ns, body={"spec": {"suspend": False}}
                )
                return _answer("pending", "the run started", name)
            return _answer("pending", "waiting for a free slot", name)
        return _answer("pending", "the run is in progress", name)

    def _create(self, decl, rollout, revision, pod_hash, name, jobs):
        """Step 5. Returns the new Job, or an answer dict when none may start."""
        ns = self._s.namespace
        agent_jobs = [
            j
            for j in jobs
            if _labels(j).get(LABEL_AGENT_NS) == decl.namespace
            and _labels(j).get(LABEL_AGENT) == decl.agent
        ]
        if any(_number(j) > int(revision) for j in agent_jobs):
            return _answer("error", "superseded by a newer revision")
        if decl.mode == "gate" and not self._h.judge_key_required:
            return _answer("error", "gate mode needs a required judge key")
        if not self._judge_key_present() and self._h.judge_key_required:
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_JUDGE_KEY_UNAVAILABLE,
                "the platform judge-key Secret is missing, so no harness run starts "
                f"({decl.mode} mode)",
            )
            return _answer("error", "the platform judge-key Secret is missing")
        owner = self._owner_ref()
        if owner is None:
            return _answer("error", "the harness is not set up on this cluster")
        # Only now, after the checks passed: replace this agent's older runs.
        for old in agent_jobs:
            if _number(old) < int(revision) and _finished_condition(old)[0] is None:
                self._delete(old.metadata.name)
                jobs.remove(old)
        body = self._jobs.job_body(decl, pod_hash, revision, name, owner=owner, suspend=True)
        try:
            created = self._batch.create_namespaced_job(namespace=ns, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            # Another poll created it between the list and the create.
            return _answer("pending", "the run is starting")
        jobs.append(created)
        self._recorder.normal(
            decl.namespace,
            decl.configmap,
            events.REASON_HARNESS_RUN_STARTED,
            f"harness run {name} started for revision {pod_hash} (number {revision}) on "
            f"dataset {decl.dataset} ({decl.mode} mode)",
        )
        if decl.mcp_servers or decl.sub_agents:
            declared = ", ".join(
                part
                for part in (
                    f"MCP servers {list(decl.mcp_servers)}" if decl.mcp_servers else "",
                    f"sub-agents {list(decl.sub_agents)}" if decl.sub_agents else "",
                )
                if part
            )
            self._recorder.warn(
                decl.namespace,
                decl.configmap,
                events.REASON_HARNESS_AGENT_HAS_TOOLS,
                f"the agent declares {declared}. The platform datasets are written "
                "for an agent without tools or sub-agents: a case that asks for an "
                "action may be carried out for real, and a live-data case may fail "
                "because the agent answers correctly from a tool",
            )
        return created

    def _complete(self, decl, job, pod_hash: str, name: str) -> dict:
        try:
            cm = self._core.read_namespaced_config_map(
                name=result_configmap_name(name), namespace=self._s.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return _answer("error", "the Job finished without writing a result", name)
            raise
        if not _owned_by(cm, job):
            return _answer("error", "the result does not belong to this Job", name)
        try:
            run = parse_result(cm.data)
        except ResultSchemaError:
            return _answer("error", "the result could not be read", name)
        # The end check (D6): the preview and the Rollout still show this revision.
        refusal = self._preview_refusal(decl, pod_hash)
        if refusal:
            return _answer("error", refusal, name)
        try:
            rollout = self._custom.get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", decl.namespace, "rollouts", decl.agent
            )
        except ApiException as exc:
            if exc.status == 404:
                return _answer("error", NOT_CURRENT, name)
            raise
        if (rollout.get("status") or {}).get("currentPodHash") != pod_hash:
            return _answer("error", NOT_CURRENT, name)
        if run.status == "error":
            return _answer("error", run.error or "the run errored", name)
        if run.status == "insufficient":
            return _answer(
                "insufficient",
                f"{run.passed + run.failed} of {run.cases} cases were judged, below the minimum",
                name,
            )
        ratio = run.pass_ratio if run.pass_ratio is not None else 0.0
        state = "pass" if ratio >= decl.threshold else "fail"
        return _answer(
            state,
            f"{run.passed}/{run.cases} cases passed; threshold {decl.threshold}",
            name,
        )

    # ── small helpers ────────────────────────────────────────────────────

    def _judge_key_present(self) -> bool:
        try:
            secret = self._core.read_namespaced_secret(
                name=self._h.judge_key_secret, namespace=self._s.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return bool(getattr(secret, "data", None))

    def _owner_ref(self) -> dict | None:
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
            return None
        return {"apiVersion": "v1", "kind": "ConfigMap", "name": self._h.catalog_configmap, "uid": uid}

    def _delete(self, name: str) -> None:
        try:
            self._batch.delete_namespaced_job(
                name=name, namespace=self._s.namespace, propagation_policy="Background"
            )
        except ApiException as exc:
            if exc.status != 404:
                raise


def _revision_number(rollout) -> int | None:
    ann = ((rollout or {}).get("metadata") or {}).get("annotations") or {}
    try:
        return int(ann.get(ROLLOUT_REVISION_ANNOTATION, ""))
    except ValueError:
        return None


def _number(job) -> int:
    try:
        return int(_labels(job).get(LABEL_REVISION_NUMBER, "0"))
    except ValueError:
        return 0


def make_handler(service: VerdictService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802 - http.server API
            parts = urlsplit(self.path)
            if parts.path != PATH:
                self._send(404, _answer("error", "not found"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 1024))
            try:
                args = validate(parts.query)
            except (InvalidRequest, ValueError) as exc:
                logger.warning("harness verdict: invalid request: %s", exc)
                self._send(400, _answer("error", f"invalid request: {exc}"))
                return
            try:
                body = service.verdict(
                    args["namespace"], args["agent"], args["revision"], args["hash"]
                )
            except ApiException as exc:
                logger.warning(
                    "harness verdict: Kubernetes API %s %s for %s/%s",
                    exc.status, exc.reason, args["namespace"], args["agent"],
                )
                self._send(503, _answer("error", "the Kubernetes API failed; retry"))
                return
            except Exception:  # noqa: BLE001 - never leave a poll without an answer
                logger.exception("harness verdict failed for %s/%s", args["namespace"], args["agent"])
                self._send(503, _answer("error", "internal error; retry"))
                return
            if body["state"] != "pending":
                logger.info(
                    "harness verdict for %s/%s revision %s: %s",
                    args["namespace"], args["agent"], args["revision"], body["state"],
                )
            self._send(200, body)

        def _not_allowed(self):
            self._send(405, _answer("error", "method not allowed"))

        do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_allowed  # noqa: N815

        def log_message(self, fmt, *args):  # request lines carry query strings
            return

    return Handler


def serve(service: VerdictService, port: int) -> ThreadingHTTPServer:
    """Start the endpoint on its own listener, in a daemon thread."""
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
    threading.Thread(target=server.serve_forever, name="harness-api", daemon=True).start()
    logger.info("harness verdict endpoint listening on port %d", port)
    return server
