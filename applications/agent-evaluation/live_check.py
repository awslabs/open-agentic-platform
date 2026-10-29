"""Validate the reconciler's real payloads against a live Langfuse.

Not a unit test: it talks to a real project and creates real objects. Run it
against a hub with a port-forward to Langfuse. It reports what each step did and
cleans up the throwaway rule it creates.

    python live_check.py --catalog /path/to/catalog.yaml \
        --url http://127.0.0.1:3999 --keys /path/to/lfkeys.json
"""

from __future__ import annotations

import argparse
import json
import sys

from app.cluster_loop import ClusterLoop
from app.config import load_catalog
from app.declarations import AgentDeclaration, RequestedEvaluator
from app.langfuse_client import LangfuseClient, LangfuseError
from app.metrics import Metrics
from app.project_loop import JUDGE_PROVIDER, ProjectLoop
from app.config import Settings


class Recorder:
    def __init__(self):
        self.warnings = []

    def warn(self, ns, cm, reason, message):
        self.warnings.append((reason, message))

    def normal(self, *a):
        pass


class Reader:
    def __init__(self, decls):
        self._decls = decls

    def list(self):
        return list(self._decls), []


def ok(msg):
    print(f"  PASS  {msg}")


def fail(msg):
    print(f"  FAIL  {msg}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--cluster", default="hub")
    p.add_argument("--keep", action="store_true", help="do not delete the test rule")
    args = p.parse_args()

    keys = json.load(open(args.keys))
    catalog = load_catalog(args.catalog)
    settings = Settings(
        role="hub",
        cluster=args.cluster,
        namespace="agent-evaluation",
        langfuse_url=args.url,
        langfuse_public_key=keys["LANGFUSE_PUBLIC_KEY"],
        langfuse_secret_key=keys["LANGFUSE_SECRET_KEY"],
        judge_virtual_key="",
        bifrost_url="http://bifrost.bifrost.svc.cluster.local:8080/v1",
        reconcile_interval=60,
        metrics_interval=300,
        metrics_windows=("24h",),
        metrics_port=9090,
        catalog_path=args.catalog,
        langfuse_timeout=30.0,
        max_active_rules=50,
    )
    client = LangfuseClient(
        settings.langfuse_url,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
        allow_project_writes=True,
        timeout=30.0,
    )
    metrics = Metrics()
    failures = 0

    print("\n== 1. judge LLM connection (hub-owned) ==")
    loop = ProjectLoop(settings, catalog, client, metrics)
    try:
        loop._reconcile_connection()
        ok(f"upserted connection provider={JUDGE_PROVIDER}")
    except LangfuseError as exc:
        fail(f"connection refused: {exc}")
        failures += 1

    print("\n== 2. catalog evaluators (hub-owned) ==")
    desired = loop.desired_evaluators()
    print(f"  catalog implies {len(desired)} evaluator object(s)")
    for name, payload in sorted(desired.items()):
        try:
            created = client.create_evaluator_version(payload)
            ok(
                f"{name}: version={created.get('version')} "
                f"scope={created.get('scope')} vars={created.get('variables')}"
            )
        except LangfuseError as exc:
            fail(f"{name}: {exc}")
            failures += 1

    print("\n== 3. write-on-change (a second pass must write nothing) ==")
    before = [c for c in [] ]  # placeholder for readability
    published_before = len(client.list_evaluators())
    if loop.run_once():
        after = len(client.list_evaluators())
        if after == published_before:
            ok(f"no new versions minted ({after} total evaluator versions)")
        else:
            fail(f"minted {after - published_before} extra version(s)")
            failures += 1
    else:
        fail("project loop reported failure")
        failures += 1

    print("\n== 4. evaluation rule (per-cluster) ==")
    published = {e["name"] for e in client.list_evaluators() if e.get("name")}
    decl = AgentDeclaration(
        agent="livecheck-agent",
        namespace="agents",
        configmap="livecheck-agent-evaluation",
        backend="centralized",
        model="claude-sonnet",
        strictness="medium",
        coverage="medium",
        evaluators=(RequestedEvaluator("goal-completion"), RequestedEvaluator("tool-use")),
        sampling=None,
        judge=None,
    )
    recorder = Recorder()
    cluster = ClusterLoop(
        settings, catalog, client, metrics, Reader([decl]), recorder
    )
    created_rules = []
    plan = cluster.plan([decl], published)
    for group in plan.groups.values():
        payload = group.payload(args.cluster)
        try:
            rule = client.create_evaluation_rule(payload)
            created_rules.append(rule.get("id"))
            ok(
                f"{payload['name']}: evaluator={payload['evaluator']['name']} "
                f"sampling={payload['sampling']} "
                f"mapping={[m['variable'] for m in payload['mapping']]}"
            )
        except LangfuseError as exc:
            fail(f"{payload['name']}: {exc}")
            failures += 1
    for reason, message in recorder.warnings:
        print(f"  EVENT {reason}: {message[:150]}")

    print("\n== 5. metrics query (scores view) ==")
    metrics_loop_ok = True
    from app.metrics_loop import MetricsLoop

    cluster.last_plan = plan
    mloop = MetricsLoop(settings, catalog, client, metrics, lambda: plan)
    if mloop.run_once():
        ok("metrics pass completed (no scores yet is expected on a fresh project)")
    else:
        fail("metrics pass failed")
        failures += 1

    if not args.keep:
        print("\n== 6. cleanup ==")
        for rule_id in created_rules:
            try:
                client.delete_evaluation_rule(rule_id)
                ok(f"deleted test rule {rule_id}")
            except LangfuseError as exc:
                fail(f"could not delete rule {rule_id}: {exc}")
                failures += 1

    print(f"\n{'FAILURES: %d' % failures if failures else 'ALL CHECKS PASSED'}")
    client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
