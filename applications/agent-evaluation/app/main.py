"""Entrypoint: serve /metrics and the probes, then run the loops forever.

LISTEN BEFORE RECONCILING. The HTTP server starts before the first Langfuse or
Kubernetes call, and `/healthz` answers as soon as the process is serving. A
first deploy can sit for minutes while an ExternalSecret syncs or a freshly
attached IAM policy propagates, and a liveness probe that depended on those would
restart the pod in a loop and never recover. `/readyz` is the one that reports
the dependency, so the split is what keeps a slow dependency from looking like a
crash.

The listener binds the pod network because the AMP scraper reaches it through a
ClusterIP Service; the chart publishes no host port and creates no Ingress, so it
is reachable only from inside the cluster.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from prometheus_client import generate_latest
from prometheus_client.core import CollectorRegistry

from . import events
from .cluster_loop import ClusterLoop
from .config import ConfigError, load_catalog, load_settings
from .declarations import DeclarationReader
from .langfuse_client import LangfuseClient
from .metrics import Metrics
from .metrics_loop import MetricsLoop
from .naming import owner_token
from .project_loop import ProjectLoop

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logger = logging.getLogger("agent-evaluation")


class _State:
    """What the probes report. Written by the loops, read by the HTTP handler.

    Readiness is per LOOP, not a single flag, because an instance is only doing
    its job when every loop it is responsible for is working. On a hub that
    includes the project loop: if the catalog evaluators and the judge connection
    cannot be published, this instance is not delivering the one thing only it
    can do, and reporting Ready would be fake-green — the addon would look
    healthy while no agent anywhere could be scored. A spoke has no project loop,
    so its readiness rests on the rules loop alone.
    """

    def __init__(self, required_loops: tuple[str, ...]):
        self.lock = threading.Lock()
        self._required = required_loops
        self._ok: dict[str, bool] = {name: False for name in required_loops}
        self._detail: dict[str, object] = {}
        self._reasons: dict[str, str] = {
            name: "no pass has completed yet" for name in required_loops
        }

    def loop_ready(self, loop: str, **detail) -> None:
        with self.lock:
            self._ok[loop] = True
            self._reasons.pop(loop, None)
            self._detail.update(detail)

    def loop_failing(self, loop: str, reason: str) -> None:
        with self.lock:
            self._ok[loop] = False
            self._reasons[loop] = reason

    def snapshot(self) -> tuple[bool, dict]:
        with self.lock:
            ready = all(self._ok.get(name) for name in self._required)
            detail: dict[str, object] = {
                "status": "ready" if ready else "initializing",
                **self._detail,
            }
            if not ready:
                detail["waitingOn"] = {
                    name: self._reasons.get(name, "not ready")
                    for name in self._required
                    if not self._ok.get(name)
                }
            return ready, detail


def _make_handler(registry: CollectorRegistry, state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/metrics"):
                body = generate_latest(registry)
                self._respond(200, body, "text/plain; version=0.0.4; charset=utf-8")
                return
            if self.path.startswith("/healthz"):
                # Deliberately unconditional: the process is serving HTTP.
                self._respond(200, b'{"status":"ok"}', "application/json")
                return
            if self.path.startswith("/readyz"):
                ready, detail = state.snapshot()
                body = json.dumps(detail).encode()
                self._respond(200 if ready else 503, body, "application/json")
                return
            self._respond(404, b'{"error":"not found"}', "application/json")

        def log_message(self, fmt, *args):
            # Probe and scrape traffic every few seconds would drown the
            # reconcile logs, which are the ones worth reading.
            logger.debug("http %s", fmt % args)

    return Handler


def _load_kube() -> bool:
    """Load Kubernetes credentials. False when none are available yet.

    Deliberately not fatal. The whole reason /healthz and /readyz are split is
    that a dependency which is not ready must not look like a crash: exiting here
    would put the pod in CrashLoopBackOff, where the probes can no longer report
    anything at all. Returning False instead leaves the process serving, lets
    /readyz say why, and lets the next reconcile pass retry — so the pod recovers
    on its own once the dependency appears.
    """
    try:
        k8s_config.load_incluster_config()
        return True
    except k8s_config.ConfigException:
        pass
    try:
        k8s_config.load_kube_config()
        return True
    except Exception as exc:  # noqa: BLE001 - any failure here is non-fatal
        logger.error(
            "no Kubernetes credentials available (%s). Still serving: /readyz "
            "reports not-ready and each reconcile pass retries. In-cluster this "
            "means the ServiceAccount token is not mounted yet.",
            exc,
        )
        return False


def _run_loop(name: str, interval: int, body, stop: threading.Event, metrics, cluster):
    """Run one loop forever, surviving any exception it raises.

    A reconciler that exits on an unexpected error turns a transient Langfuse
    hiccup into a CrashLoopBackOff. Every pass is a full diff, so the next one
    recovers whatever this one missed.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            if body():
                metrics.last_success.labels(cluster, name).set(time.time())
        except Exception:  # noqa: BLE001
            metrics.errors.labels(cluster, name).inc()
            logger.exception("%s loop pass failed", name)
        elapsed = time.monotonic() - started
        stop.wait(max(1.0, interval - elapsed))


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        stream=sys.stdout,
    )

    try:
        settings = load_settings()
        catalog = load_catalog(settings.catalog_path)
    except ConfigError as exc:
        # Configuration that cannot be defaulted is a deploy error, not a
        # transient one: exit and let the Deployment's restart surface it.
        logger.error("configuration error: %s", exc)
        return 2

    metrics = Metrics()
    # A hub is only ready when it can also publish the project-wide objects.
    state = _State(("rules", "project") if settings.role == "hub" else ("rules",))

    token = owner_token(settings.cluster)
    metrics.cluster_info.labels(settings.cluster, token, settings.role).set(1)
    metrics.judge_key_present.labels(settings.cluster).set(
        1 if settings.judge_virtual_key else 0
    )
    logger.info(
        "agent-evaluation reconciler starting: role=%s cluster=%s owner_token=%s "
        "evaluators=%d judge_default=%s aliases=%s langfuse=%s",
        settings.role,
        settings.cluster,
        token,
        len(catalog.evaluators),
        catalog.judge_default,
        ",".join(catalog.judge_aliases),
        settings.langfuse_url,
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", settings.metrics_port),  # noqa: S104 - see module docstring
        _make_handler(metrics.registry, state),
    )
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("serving /metrics, /healthz and /readyz on :%d", settings.metrics_port)

    kube_ready = _load_kube()
    if not kube_ready:
        state.loop_failing("rules", "waiting for Kubernetes credentials")
    core = k8s_client.CoreV1Api()
    reader = DeclarationReader(core)
    recorder = events.EventRecorder(settings.cluster, core)

    langfuse = LangfuseClient(
        settings.langfuse_url,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
        allow_project_writes=settings.is_hub,
        timeout=settings.langfuse_timeout,
    )

    cluster_loop = ClusterLoop(
        settings, catalog, langfuse, metrics, reader, recorder
    )
    metrics_loop = MetricsLoop(
        settings, catalog, langfuse, metrics, lambda: cluster_loop.last_plan
    )

    stop = threading.Event()

    def _shutdown(signum, _frame):
        logger.info("signal %s received; finishing the current pass and exiting", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threads: list[threading.Thread] = []

    if settings.is_hub:
        project_loop = ProjectLoop(settings, catalog, langfuse, metrics)

        def project_pass() -> bool:
            ok = project_loop.run_once()
            if ok:
                state.loop_ready(
                    "project",
                    role=settings.role,
                    cluster=settings.cluster,
                    evaluators=len(catalog.evaluators),
                )
            else:
                state.loop_failing(
                    "project",
                    "cannot publish the catalog evaluators or the judge LLM "
                    "connection; see the reconciler logs",
                )
            return ok

        threads.append(
            threading.Thread(
                target=_run_loop,
                args=(
                    "project",
                    settings.reconcile_interval,
                    project_pass,
                    stop,
                    metrics,
                    settings.cluster,
                ),
                daemon=True,
            )
        )

    kube = {"ready": kube_ready}

    def cluster_pass() -> bool:
        # Retry the credential load until it works, so a token that is mounted
        # late recovers without a restart.
        if not kube["ready"]:
            kube["ready"] = _load_kube()
            if not kube["ready"]:
                state.loop_failing("rules", "waiting for Kubernetes credentials")
                return False
        ok = cluster_loop.run_once()
        plan = cluster_loop.last_plan
        if ok and plan is not None:
            state.loop_ready(
                "rules",
                role=settings.role,
                cluster=settings.cluster,
                agents=plan.declared,
                rules=len(plan.groups),
                skipped=plan.skipped,
            )
        elif not ok:
            state.loop_failing(
                "rules", "the last reconcile pass did not complete"
            )
        return ok

    threads.append(
        threading.Thread(
            target=_run_loop,
            args=(
                "rules",
                settings.reconcile_interval,
                cluster_pass,
                stop,
                metrics,
                settings.cluster,
            ),
            daemon=True,
        )
    )
    threads.append(
        threading.Thread(
            target=_run_loop,
            args=(
                "metrics",
                settings.metrics_interval,
                metrics_loop.run_once,
                stop,
                metrics,
                settings.cluster,
            ),
            daemon=True,
        )
    )

    for thread in threads:
        thread.start()

    stop.wait()
    server.shutdown()
    langfuse.close()
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
