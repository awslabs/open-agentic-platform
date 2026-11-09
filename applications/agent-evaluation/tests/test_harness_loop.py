"""The harness loop: one platform-namespace Job per agent revision, report mode."""

from __future__ import annotations

import base64
import dataclasses
from types import SimpleNamespace

from app.bifrost_admin import BifrostAdminError
from app.events import (
    REASON_HARNESS_JUDGE_KEY_UNAVAILABLE,
    REASON_HARNESS_RUN_COMPLETED,
    REASON_HARNESS_RUN_FAILED,
    REASON_HARNESS_RUN_STARTED,
    REASON_HARNESS_RUN_SUPERSEDED,
    REASON_INVALID_HARNESS_DECLARATION,
    REASON_UNKNOWN_DATASET,
)
from app.harness_declarations import HarnessDeclaration
from app.harness_loop import HarnessLoop
from app.harness_results import CaseResult, summarize, to_configmap_data
from app.naming import harness_job_name, result_configmap_name
from tests.conftest import (
    FakeBatch,
    FakeCore,
    FakeCustomObjects,
    FakeEvents,
    FakeReader,
    make_configmap,
)

PLATFORM_NS = "agent-evaluation"


def decl(agent="assistant", namespace="team-a", dataset="example-smoke-v1", threshold=0.5):
    return HarnessDeclaration(
        agent=agent,
        namespace=namespace,
        configmap=f"{agent}-harness",
        dataset=dataset,
        checks=("expected-answer",),
        threshold=threshold,
        mode="report",
        preview_service=f"{agent}-preview",
        preview_port=8083,
    )


class FakeBifrost:
    def __init__(self, value="sk-bf-judge", fail=False):
        self.value = value
        self.fail = fail
        self.calls = []
        self.known = []

    def ensure_judge_key(self, models, *args, known=None):
        self.calls.append(tuple(models))
        self.known.append(known)
        if self.fail:
            raise BifrostAdminError("bifrost down")
        return self.value


def build(settings, harness_settings, catalog, metrics, declarations, *, fakes=None, bifrost=None, malformed=None):
    fakes = fakes or SimpleNamespace(
        batch=FakeBatch(), custom=FakeCustomObjects(), core=FakeCore(), events=FakeEvents()
    )
    fakes.reader = FakeReader(declarations, malformed)
    loop = HarnessLoop(
        settings,
        harness_settings,
        catalog,
        metrics,
        fakes.reader,
        fakes.events,
        fakes.batch,
        fakes.custom,
        fakes.core,
        bifrost if bifrost is not None else FakeBifrost(),
        clock=lambda: 5_000.0,
    )
    return loop, fakes


def write_result(core, job_name, d, revision, outcomes, finished_at=3_000.0, trigger="first-revision"):
    cases = [
        CaseResult(id=f"c{i}", status=s, score=None, reason="", latency_ms=1, tags=())
        for i, s in enumerate(outcomes)
    ]
    run = summarize(
        namespace=d.namespace,
        agent=d.agent,
        revision=revision,
        dataset=d.dataset,
        checks=d.checks,
        threshold=d.threshold,
        mode="report",
        trigger=trigger,
        judge_model="us.amazon.nova-pro-v1:0",
        judge_key_present=True,
        started_at=finished_at - 10,
        finished_at=finished_at,
        case_results=cases,
    )
    core.create_namespaced_config_map(
        PLATFORM_NS,
        {
            "metadata": {
                "name": result_configmap_name(job_name),
                "namespace": PLATFORM_NS,
                "labels": {"harness.oap.io/result": "true", "harness.oap.io/job": job_name},
            },
            "data": to_configmap_data(run),
        },
    )


def sample(metrics, name, agent="assistant", namespace="team-a", dataset="example-smoke-v1"):
    return metrics.registry.get_sample_value(
        name, {"cluster": "dev", "namespace": namespace, "agent": agent, "dataset": dataset}
    )


def env_of(body):
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in body["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_first_revision_creates_a_job_in_the_platform_namespace(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")

    assert loop.run_once()

    assert len(f.batch.created) == 1
    body = f.batch.created[0]
    assert body["metadata"]["namespace"] == PLATFORM_NS == settings.namespace
    assert body["metadata"]["name"] == harness_job_name("team-a", "assistant", "rev1", "example-smoke-v1")
    assert body["metadata"]["annotations"]["harness.oap.io/trigger"] == "first-revision"
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["activeDeadlineSeconds"] == harness_settings.run_timeout
    pod = body["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "agent-evaluation-harness-runner"
    container = pod["containers"][0]
    assert container["command"] == ["python", "-m", "app.harness_runner"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    env = env_of(body)
    assert env["HARNESS_TARGET_URL"] == "http://assistant-preview.team-a.svc.cluster.local:8083/"
    assert env["HARNESS_JUDGE_MODEL"] == "us.amazon.nova-pro-v1:0"
    assert env["HARNESS_TRIGGER"] == "first-revision"
    assert env["POD_NAMESPACE"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
    assert not any(k.startswith("LANGFUSE") for k in env)
    secret_vol = next(v for v in pod["volumes"] if v["name"] == "judge-key")
    assert secret_vol["secret"]["secretName"] == "agent-evaluation-harness-judge-key"
    assert secret_vol["secret"]["optional"] is True
    mount = next(m for m in container["volumeMounts"] if m["name"] == "judge-key")
    assert mount["mountPath"] == "/etc/agent-evaluation/credentials/BIFROST_VIRTUAL_KEY"
    assert REASON_HARNESS_RUN_STARTED in [n[2] for n in f.events.normals]
    # The minted key landed in the Secret the Job mounts.
    secret = f.core.secrets[(PLATFORM_NS, "agent-evaluation-harness-judge-key")]
    assert base64.b64decode(secret.data["BIFROST_VIRTUAL_KEY"]).decode() == "sk-bf-judge"
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 1


def test_preview_selector_not_switched_yet_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev2", preview="rev1")
    loop.run_once()
    assert f.batch.created == []


def test_agent_not_deployed_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    assert loop.run_once()
    assert f.batch.created == []


def test_same_revision_and_restart_create_no_new_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    loop.run_once()
    assert len(f.batch.created) == 1

    restarted, _ = build(settings, harness_settings, catalog, metrics, [decl()], fakes=f)
    restarted.run_once()
    assert len(f.batch.created) == 1

    # Also once the Job has finished.
    f.batch.finish(f.batch.created[0]["metadata"]["name"])
    restarted.run_once()
    assert len(f.batch.created) == 1


def test_new_revision_supersedes_the_active_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    old = f.batch.created[0]["metadata"]["name"]

    f.custom.set_rollout("team-a", "assistant", "rev2")
    loop.run_once()

    assert len(f.batch.created) == 2
    new = f.batch.created[1]
    assert new["metadata"]["annotations"]["harness.oap.io/trigger"] == "new-revision"
    assert env_of(new)["HARNESS_REVISION"] == "rev2"
    assert old in f.batch.deleted
    assert REASON_HARNESS_RUN_SUPERSEDED in [n[2] for n in f.events.normals]
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "superseded"}
    ) == 1


def test_new_revision_after_a_finished_run_keeps_history(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    old = f.batch.created[0]["metadata"]["name"]
    f.batch.finish(old)
    f.custom.set_rollout("team-a", "assistant", "rev2")
    loop.run_once()
    assert old not in f.batch.deleted
    assert f.batch.created[1]["metadata"]["annotations"]["harness.oap.io/trigger"] == "new-revision"


def test_nothing_finished_publishes_no_pass_ratio(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert sample(metrics, "oap_agent_harness_cases") is None


def test_finished_job_with_result_publishes_metrics(settings, harness_settings, catalog, metrics):
    d = decl(threshold=0.5)
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed", "passed", "failed", "error"])
    f.batch.finish(name)

    loop.run_once()
    loop.run_once()  # the completion is announced once

    assert sample(metrics, "oap_agent_harness_cases") == 4
    assert sample(metrics, "oap_agent_harness_passed") == 2
    assert sample(metrics, "oap_agent_harness_failed") == 1
    assert sample(metrics, "oap_agent_harness_case_errors") == 1
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 0.5
    assert sample(metrics, "oap_agent_harness_threshold_met") == 1
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    assert sample(metrics, "oap_agent_harness_last_run_timestamp_seconds") == 3_000.0
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_info",
        {
            "cluster": "dev",
            "namespace": "team-a",
            "agent": "assistant",
            "dataset": "example-smoke-v1",
            "revision": "rev1",
            "mode": "report",
            "judge_model": "us.amazon.nova-pro-v1:0",
            "trigger": "first-revision",
            "status": "completed",
        },
    ) == 1
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) == 1
    completed = [n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert len(completed) == 1
    assert "2/4 passed" in completed[0][3]
    assert "promotion not affected" in completed[0][3]


def test_failed_job_without_result_is_a_run_error(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    f.batch.finish(f.batch.created[0]["metadata"]["name"], state="Failed")

    loop.run_once()

    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert sample(metrics, "oap_agent_harness_threshold_met") is None
    assert sample(metrics, "oap_agent_harness_last_run_timestamp_seconds") == 2_000.0
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "error"}
    ) == 1
    assert REASON_HARNESS_RUN_FAILED in f.events.reasons()
    info = [
        s for s in metrics.registry.collect() if s.name == "oap_agent_harness_info"
    ][0].samples
    assert [s.labels["status"] for s in info] == ["error"]


def test_result_with_unknown_schema_is_a_run_error(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    f.core.create_namespaced_config_map(
        PLATFORM_NS,
        {
            "metadata": {
                "name": result_configmap_name(name),
                "namespace": PLATFORM_NS,
                "labels": {"harness.oap.io/result": "true", "harness.oap.io/job": name},
            },
            "data": {"result.json": '{"schema": "something/v9"}'},
        },
    )
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert REASON_HARNESS_RUN_FAILED in f.events.reasons()


def test_unknown_dataset_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl(dataset="not-shipped")])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    assert f.batch.created == []
    assert REASON_UNKNOWN_DATASET in f.events.reasons()


def test_judge_key_unavailable_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()], bifrost=FakeBifrost(fail=True))
    f.custom.set_rollout("team-a", "assistant", "rev1")
    assert loop.run_once() is False
    assert f.batch.created == []
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 0
    assert REASON_HARNESS_JUDGE_KEY_UNAVAILABLE in f.events.reasons()


def test_judge_key_not_required_still_runs(settings, harness_settings, catalog, metrics):
    hs = dataclasses.replace(harness_settings, judge_key_required=False)
    loop, f = build(settings, hs, catalog, metrics, [decl()], bifrost=FakeBifrost(fail=True))
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    assert len(f.batch.created) == 1


def test_secret_written_only_on_change(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [])
    loop.run_once()
    loop.run_once()
    assert f.core.secret_writes == ["create"]


def test_secret_value_is_offered_back_to_bifrost_after_restart(
    settings, harness_settings, catalog, metrics
):
    loop, f = build(settings, harness_settings, catalog, metrics, [])
    loop.run_once()
    secret = f.core.secrets[(PLATFORM_NS, "agent-evaluation-harness-judge-key")]
    key_name = secret.metadata.annotations["harness.oap.io/judge-key-name"]
    assert key_name.startswith("oap-harness-judge-")
    # A new process (fresh BifrostAdmin, same Secret) passes the stored pair in.
    bifrost = FakeBifrost()
    restarted, _ = build(settings, harness_settings, catalog, metrics, [], fakes=f, bifrost=bifrost)
    restarted.run_once()
    assert bifrost.known == [(key_name, "sk-bf-judge")]
    assert f.core.secret_writes == ["create"]


def test_malformed_declaration_is_reported(settings, harness_settings, catalog, metrics):
    bad = make_configmap("team-b", "x-harness", {}, labels={"harness.oap.io/agent": "x"})
    loop, f = build(settings, harness_settings, catalog, metrics, [], malformed=[(bad, "no dataset")])
    loop.run_once()
    assert REASON_INVALID_HARNESS_DECLARATION in f.events.reasons()


def test_declaration_removed_deletes_jobs_results_and_series(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed"])
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0

    gone, _ = build(settings, harness_settings, catalog, metrics, [], fakes=f)
    gone.run_once()

    assert f.batch.jobs == {}
    assert f.core.configmaps == {}
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert sample(metrics, "oap_agent_harness_cases") is None


def test_history_limit_prunes_the_oldest(settings, harness_settings, catalog, metrics):
    hs = dataclasses.replace(harness_settings, history_limit=2)
    d = decl()
    loop, f = build(settings, hs, catalog, metrics, [d])
    names = []
    for i, rev in enumerate(("rev1", "rev2", "rev3")):
        f.custom.set_rollout("team-a", "assistant", rev)
        loop.run_once()
        name = f.batch.created[-1]["metadata"]["name"]
        names.append(name)
        trigger = f.batch.created[-1]["metadata"]["annotations"]["harness.oap.io/trigger"]
        write_result(f.core, name, d, rev, ["passed"], finished_at=3_000.0 + i, trigger=trigger)
        f.batch.finish(name, at=2_000.0 + i)
    loop.run_once()

    assert set(f.batch.jobs) == set(names[1:])
    assert (PLATFORM_NS, result_configmap_name(names[0])) not in f.core.configmaps
    # The latest run is the one published.
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_info",
        {
            "cluster": "dev",
            "namespace": "team-a",
            "agent": "assistant",
            "dataset": "example-smoke-v1",
            "revision": "rev3",
            "mode": "report",
            "judge_model": "us.amazon.nova-pro-v1:0",
            "trigger": "new-revision",
            "status": "completed",
        },
    ) == 1


def test_concurrency_limit_defers_new_jobs(settings, harness_settings, catalog, metrics):
    hs = dataclasses.replace(harness_settings, max_concurrent_jobs=1)
    loop, f = build(settings, hs, catalog, metrics, [decl("a"), decl("b")])
    f.custom.set_rollout("team-a", "a", "r1")
    f.custom.set_rollout("team-a", "b", "r1")
    loop.run_once()
    assert len(f.batch.created) == 1
    f.batch.finish(f.batch.created[0]["metadata"]["name"])
    loop.run_once()
    assert len(f.batch.created) == 2


def test_mint_disabled_uses_an_existing_secret(settings, harness_settings, catalog, metrics):
    hs = dataclasses.replace(harness_settings, judge_key_mint=False)
    bifrost = FakeBifrost()
    loop, f = build(settings, hs, catalog, metrics, [decl()], bifrost=bifrost)
    f.custom.set_rollout("team-a", "assistant", "rev1")
    assert loop.run_once() is False  # no Secret yet
    f.core.create_namespaced_secret(
        PLATFORM_NS,
        {
            "metadata": {"name": "agent-evaluation-harness-judge-key", "namespace": PLATFORM_NS},
            "data": {"BIFROST_VIRTUAL_KEY": base64.b64encode(b"sk-bf-platform").decode()},
        },
    )
    assert loop.run_once()
    assert len(f.batch.created) == 1
    assert bifrost.calls == []
