"""The harness loop: one platform-namespace Job per agent revision, report mode."""

from __future__ import annotations

import base64
import dataclasses
import json
from types import SimpleNamespace

from app.bifrost_admin import BifrostAdminError
from app.events import (
    REASON_HARNESS_JUDGE_KEY_UNAVAILABLE,
    REASON_HARNESS_PREVIEW_UNSUPPORTED,
    REASON_HARNESS_RUN_COMPLETED,
    REASON_HARNESS_RUN_FAILED,
    REASON_HARNESS_RUN_STARTED,
    REASON_HARNESS_RUN_SUPERSEDED,
    REASON_HARNESS_WAITING_FOR_PREVIEW,
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
CATALOG_UID = "uid-harness-catalog"
CATALOG_NAME = "agent-evaluation-harness-catalog"


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
        self.delete_stale = []

    def ensure_judge_key(self, models, *args, known=None, delete_stale=True):
        self.calls.append(tuple(models))
        self.known.append(known)
        self.delete_stale.append(delete_stale)
        if self.fail:
            raise BifrostAdminError("bifrost down")
        return self.value


def build(settings, harness_settings, catalog, metrics, declarations, *, fakes=None, bifrost=None, malformed=None):
    fakes = fakes or SimpleNamespace(
        batch=FakeBatch(), custom=FakeCustomObjects(), core=FakeCore(), events=FakeEvents()
    )
    fakes.reader = FakeReader(declarations, malformed)
    # The chart's catalog ConfigMap: owner of the Jobs and the judge Secret.
    if (PLATFORM_NS, harness_settings.catalog_configmap) not in fakes.core.configmaps:
        fakes.core.create_namespaced_config_map(
            PLATFORM_NS,
            {
                "metadata": {
                    "name": harness_settings.catalog_configmap,
                    "namespace": PLATFORM_NS,
                    "uid": CATALOG_UID,
                    "labels": {},
                },
                "data": {},
            },
        )
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


def write_result(
    core,
    job_name,
    d,
    revision,
    outcomes,
    finished_at=3_000.0,
    trigger="first-revision",
    owner_uid=None,
    **extra,
):
    """Write a result the way the runner does, owned by the Job's uid.

    owner_uid defaults to the uid of the first Job created under job_name.
    """
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
        **extra,
    )
    core.create_namespaced_config_map(
        PLATFORM_NS,
        {
            "metadata": {
                "name": result_configmap_name(job_name),
                "namespace": PLATFORM_NS,
                "labels": {"harness.oap.io/result": "true", "harness.oap.io/job": job_name},
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": job_name,
                        "uid": owner_uid or FakeBatch.uid_for(job_name),
                    }
                ],
            },
            "data": to_configmap_data(run),
        },
    )


def loop_key_name(loop, harness_settings) -> str:
    """The Bifrost key name the loop writes on the Secret for its config."""
    from app.bifrost_admin import vk_name

    h = harness_settings
    return vk_name(
        h.judge_allowed_models or (loop.judge_model,),
        h.judge_budget_usd,
        h.judge_budget_reset,
        h.judge_rate_limit_requests,
        h.judge_rate_limit_reset,
    )


def result_configmaps(f) -> dict:
    """The ConfigMaps in the fake, minus the chart's catalog ConfigMap."""
    return {k: v for k, v in f.core.configmaps.items() if k[1] != CATALOG_NAME}


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
    assert env["HARNESS_RUN_NUMBER"] == "1"
    assert env["HARNESS_RUN_TIMEOUT_SECONDS"] == str(harness_settings.run_timeout)
    assert body["metadata"]["labels"]["harness.oap.io/run-number"] == "1"
    assert body["spec"]["template"]["metadata"]["labels"]["harness.oap.io/run-number"] == "1"
    assert env["POD_NAMESPACE"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
    assert env["HARNESS_JOB_UID"] == {
        "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/controller-uid']"}
    }
    assert not any(k.startswith("LANGFUSE") for k in env)
    secret_vol = next(v for v in pod["volumes"] if v["name"] == "judge-key")
    assert secret_vol["secret"]["secretName"] == "agent-evaluation-harness-judge-key"
    # Required key: the pod never starts without it.
    assert secret_vol["secret"]["optional"] is False
    mount = next(m for m in container["volumeMounts"] if m["name"] == "judge-key")
    assert mount["mountPath"] == "/etc/agent-evaluation/credentials/BIFROST_VIRTUAL_KEY"
    assert REASON_HARNESS_RUN_STARTED in [n[2] for n in f.events.normals]
    # The minted key landed in the Secret the Job mounts.
    secret = f.core.secrets[(PLATFORM_NS, "agent-evaluation-harness-judge-key")]
    assert base64.b64decode(secret.data["BIFROST_VIRTUAL_KEY"]).decode() == "sk-bf-judge"
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 1


def test_job_and_judge_secret_are_owned_by_the_catalog_configmap(
    settings, harness_settings, catalog, metrics
):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    assert loop.run_once()
    owner = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "name": CATALOG_NAME,
        "uid": CATALOG_UID,
    }
    assert f.batch.created[0]["metadata"]["ownerReferences"] == [owner]
    secret = f.core.secrets[(PLATFORM_NS, "agent-evaluation-harness-judge-key")]
    assert [r.uid for r in secret.metadata.owner_references] == [CATALOG_UID]


def test_judge_secret_without_an_owner_is_rewritten_once(
    settings, harness_settings, catalog, metrics
):
    loop, f = build(settings, harness_settings, catalog, metrics, [])
    f.core.create_namespaced_secret(
        PLATFORM_NS,
        {
            "metadata": {
                "name": "agent-evaluation-harness-judge-key",
                "namespace": PLATFORM_NS,
                "annotations": {"harness.oap.io/judge-key-name": loop_key_name(loop, harness_settings)},
            },
            "data": {"BIFROST_VIRTUAL_KEY": base64.b64encode(b"sk-bf-judge").decode()},
        },
    )
    loop.run_once()
    loop.run_once()
    assert f.core.secret_writes == ["create", "replace"]


def test_missing_catalog_configmap_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    del f.core.configmaps[(PLATFORM_NS, CATALOG_NAME)]
    f.custom.set_rollout("team-a", "assistant", "rev1")
    assert loop.run_once() is False
    assert f.batch.created == []
    assert f.core.secrets == {}


def test_preview_selector_not_switched_yet_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev2", preview="rev1")
    loop.run_once()
    assert f.batch.created == []


def test_agent_not_deployed_creates_no_job(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    assert loop.run_once()
    assert f.batch.created == []
    waiting = [n for n in f.events.normals if n[2] == REASON_HARNESS_WAITING_FOR_PREVIEW]
    assert waiting and "no Rollout named assistant" in waiting[0][3]
    assert waiting[0][:2] == ("team-a", "assistant-harness")


def test_preview_selector_not_set_yet_says_so(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1", preview="")
    assert loop.run_once()
    assert f.batch.created == []
    waiting = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_WAITING_FOR_PREVIEW]
    assert waiting and "previewSelector" in waiting[0]


def test_canary_rollout_is_reported_as_unsupported(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1", strategy="canary")
    loop.run_once()
    assert f.batch.created == []
    unsupported = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_PREVIEW_UNSUPPORTED]
    assert unsupported and "previewService" in unsupported[0]


def test_preview_not_switched_yet_records_no_event(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev2", preview="rev1")
    loop.run_once()
    assert f.batch.created == []
    assert f.events.warnings == []
    assert REASON_HARNESS_WAITING_FOR_PREVIEW not in [n[2] for n in f.events.normals]


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
    loop.run_once()  # the reconciler sees the run finish on its revision
    f.custom.set_rollout("team-a", "assistant", "rev2")
    loop.run_once()
    assert old not in f.batch.deleted
    assert f.batch.created[1]["metadata"]["annotations"]["harness.oap.io/trigger"] == "new-revision"


def test_run_that_finished_after_the_revision_moved_is_superseded(
    settings, harness_settings, catalog, metrics
):
    # Live finding (campaign Pass 3 S2, D5): the preview switched to rev2 five
    # seconds after the rev1 Job started, the Job finished before the next
    # pass, and was then reported and published as a completed rev1 run
    # although its cases had reached the rev2 pods.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    old = f.batch.created[0]["metadata"]["name"]

    f.custom.set_rollout("team-a", "assistant", "rev2")
    write_result(f.core, old, d, "rev1", ["passed", "passed"])
    f.batch.finish(old)
    loop.run_once()

    assert old in f.batch.deleted
    assert (PLATFORM_NS, result_configmap_name(old)) not in f.core.configmaps
    superseded = [n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_SUPERSEDED]
    assert len(superseded) == 1 and "rev1" in superseded[0][3] and "rev2" in superseded[0][3]
    assert REASON_HARNESS_RUN_COMPLETED not in [n[2] for n in f.events.normals]
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "superseded"}
    ) == 1
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) is None
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert not [
        s for m in metrics.registry.collect() if m.name == "oap_agent_harness_info"
        for s in m.samples if s.labels.get("revision") == "rev1"
    ]
    # rev2 gets its own run, as a new revision of the agent.
    new = f.batch.created[1]
    assert env_of(new)["HARNESS_REVISION"] == "rev2"
    assert new["metadata"]["annotations"]["harness.oap.io/trigger"] == "new-revision"


def test_restart_takes_a_finished_run_as_it_is(settings, harness_settings, catalog, metrics):
    # The supersede check needs this process to have seen the run active; a
    # restarted reconciler keeps today's behaviour and publishes the result.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    old = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, old, d, "rev1", ["passed"])
    f.batch.finish(old)
    f.custom.set_rollout("team-a", "assistant", "rev2")

    restarted, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    restarted.run_once()

    assert old not in f.batch.deleted
    assert REASON_HARNESS_RUN_SUPERSEDED not in [n[2] for n in f.events.normals]
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) == 1


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
    write_result(
        f.core, name, d, "rev1", ["passed", "passed", "failed", "error"],
        dataset_digest="0123456789ab", checks_digest="ba9876543210",
    )
    f.batch.finish(name)

    loop.run_once()
    loop.run_once()  # the completion is announced once

    assert sample(metrics, "oap_agent_harness_cases") == 4
    assert sample(metrics, "oap_agent_harness_passed") == 2
    assert sample(metrics, "oap_agent_harness_failed") == 1
    assert sample(metrics, "oap_agent_harness_case_errors") == 1
    # The errored case is outside the denominator: 2 passed of 3 judged.
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 2 / 3
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
            "dataset_digest": "0123456789ab",
            "checks_digest": "ba9876543210",
        },
    ) == 1
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) == 1
    completed = [n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert len(completed) == 1
    assert "2/4 passed" in completed[0][3]
    assert "promotion not affected" in completed[0][3]


def test_completed_event_names_the_failed_case_ids(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed", "failed", "failed"])
    f.batch.finish(name)
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED][0]
    assert "failed: c1, c2" in completed


def test_completed_event_caps_the_failed_list_at_1000_chars(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["failed"] * 200)
    f.batch.finish(name)
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED][0]
    assert len(completed) <= 1000
    listed, more = completed.split("; failed: ", 1)[1].rsplit(" +", 1)
    assert more.endswith(" more")
    assert len(listed.split(", ")) + int(more.split()[0]) == 200


def test_run_cut_short_by_the_deadline_says_so_in_the_event(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(
        f.core, name, d, "rev1", ["passed", "error"],
        error="run deadline reached: 1 of 2 cases not run",
    )
    f.batch.finish(name)
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert len(completed) == 1
    assert "run deadline reached: 1 of 2 cases not run" in completed[0]


def test_expect_tools_ignored_is_named_in_the_event(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed"], expect_tools_ignored=True)
    f.batch.finish(name)
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert "expectTools in this dataset are not checked yet" in completed[0]


def test_run_where_every_case_errored_publishes_no_counts(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["error", "error", "error", "error"])
    f.batch.finish(name)
    loop.run_once()

    for metric in ("cases", "passed", "failed", "case_errors", "pass_ratio", "threshold_met"):
        assert sample(metrics, f"oap_agent_harness_{metric}") is None, metric
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    info = [s for s in metrics.registry.collect() if s.name == "oap_agent_harness_info"][0].samples
    assert [s.labels["status"] for s in info] == ["error"]
    assert REASON_HARNESS_RUN_FAILED in f.events.reasons()
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "error"}
    ) == 1


def test_threshold_change_publishes_the_declared_threshold(settings, harness_settings, catalog, metrics):
    # Live finding (campaign S5c, D3): lowering the declared threshold on the
    # same revision started no run (correct) but the series kept the run's old
    # threshold and threshold_met 0.
    strict = decl(threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [strict])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, strict, "rev1", ["passed", "passed", "failed"])
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_threshold") == 0.9
    assert sample(metrics, "oap_agent_harness_threshold_met") == 0

    relaxed, _ = build(settings, harness_settings, catalog, metrics, [decl(threshold=0.5)], fakes=f)
    relaxed.run_once()

    assert len(f.batch.created) == 1  # a threshold change is not a new run
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    assert sample(metrics, "oap_agent_harness_threshold_met") == 1
    assert abs(sample(metrics, "oap_agent_harness_pass_ratio") - 2 / 3) < 1e-9


def test_series_follow_the_declared_dataset(settings, harness_settings, catalog, metrics, harness_dirs):
    # Live finding (campaign S5c, D4): after the declaration went back to the
    # smoke dataset, the series still showed the newer run on another dataset.
    smoke_text = (harness_dirs.datasets / "example-smoke-v1.yaml").read_text()
    for other in ("campaign-fail-v1", "campaign-new-v1"):
        (harness_dirs.datasets / f"{other}.yaml").write_text(
            smoke_text.replace("example-smoke-v1", other)
        )
    smoke = decl(threshold=0.8)
    fail = decl(dataset="campaign-fail-v1", threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [smoke])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    smoke_job = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, smoke_job, smoke, "rev1", ["passed", "passed"], finished_at=3_000.0)
    f.batch.finish(smoke_job)
    loop.run_once()

    on_fail, _ = build(settings, harness_settings, catalog, metrics, [fail], fakes=f)
    on_fail.run_once()
    fail_job = f.batch.created[1]["metadata"]["name"]
    write_result(f.core, fail_job, fail, "rev1", ["passed", "failed"], finished_at=4_000.0)
    f.batch.finish(fail_job)
    on_fail.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio", dataset="campaign-fail-v1") == 0.5
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None

    back, _ = build(settings, harness_settings, catalog, metrics, [smoke], fakes=f)
    back.run_once()
    assert len(f.batch.created) == 2  # the smoke run of rev1 exists: no re-run
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert sample(metrics, "oap_agent_harness_threshold") == 0.8
    assert sample(metrics, "oap_agent_harness_last_run_timestamp_seconds") == 3_000.0
    for metric in ("pass_ratio", "threshold", "info"):
        assert not [
            s for s in metrics.registry.collect() if s.name == f"oap_agent_harness_{metric}"
            for s in s.samples if s.labels.get("dataset") == "campaign-fail-v1"
        ], metric

    # A dataset with no finished run yet: the agent's series are absent, not 0.
    new = decl(dataset="campaign-new-v1", threshold=0.8)
    pending, _ = build(settings, harness_settings, catalog, metrics, [new], fakes=f)
    pending.run_once()
    new_job = f.batch.created[2]["metadata"]["name"]
    for metric in ("cases", "pass_ratio", "threshold", "threshold_met", "info"):
        assert not [
            s for s in metrics.registry.collect() if s.name == f"oap_agent_harness_{metric}"
            for s in s.samples
        ], metric

    # Every case errored on the declared dataset: still a run error, no counts.
    write_result(f.core, new_job, new, "rev1", ["error", "error"], finished_at=5_000.0)
    f.batch.finish(new_job)
    pending.run_once()
    for metric in ("cases", "passed", "failed", "case_errors", "pass_ratio", "threshold_met"):
        assert sample(metrics, f"oap_agent_harness_{metric}", dataset="campaign-new-v1") is None, metric
    assert sample(metrics, "oap_agent_harness_threshold", dataset="campaign-new-v1") == 0.8
    info = [s for s in metrics.registry.collect() if s.name == "oap_agent_harness_info"][0].samples
    assert [(s.labels["dataset"], s.labels["status"]) for s in info] == [("campaign-new-v1", "error")]


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
                "ownerReferences": [{"uid": FakeBatch.uid_for(name)}],
            },
            "data": {"result.json": '{"schema": "something/v9"}'},
        },
    )
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "result not understood" in failed[0]


def test_recreated_job_never_publishes_the_old_jobs_result(
    settings, harness_settings, catalog, metrics
):
    # Reviewer repro (C1): a run finishes with a result; the Job is deleted by
    # hand and the next pass recreates it under the same name. The old result
    # must not be taken as the new run's.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed", "passed"])
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0

    del f.batch.jobs[name]  # kubectl delete job
    loop.run_once()

    assert [b["metadata"]["name"] for b in f.batch.created] == [name, name]
    # Deleted before the create, so the new runner can write its own result.
    assert (PLATFORM_NS, result_configmap_name(name)) not in f.core.configmaps
    f.batch.finish(name, at=4_000.0)
    loop.run_once()
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "without writing a result" in failed[-1]


def test_result_owned_by_another_job_is_an_errored_run(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed", "passed"], owner_uid="uid-of-an-older-job")
    f.batch.finish(name)
    loop.run_once()

    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert REASON_HARNESS_RUN_COMPLETED not in [n[2] for n in f.events.normals]
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "from another Job with this name was ignored" in failed[0]
    info = [s for s in metrics.registry.collect() if s.name == "oap_agent_harness_info"][0]
    assert [s.labels["status"] for s in info.samples] == ["error"]


def test_result_with_a_wrong_typed_field_is_an_errored_run_and_pruned(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed"])
    cm = f.core.configmaps[(PLATFORM_NS, result_configmap_name(name))]
    raw = json.loads(cm.data["result.json"])
    raw["caseResults"][0]["tags"] = 5
    cm.data["result.json"] = json.dumps(raw)
    f.batch.finish(name)
    assert loop.run_once() is True
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "result not understood" in failed[0]
    # Pruned like any other run once the declaration is gone.
    gone, _ = build(settings, harness_settings, catalog, metrics, [], fakes=f)
    gone.run_once()
    assert f.batch.jobs == {}
    assert result_configmaps(f) == {}


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


def test_judge_key_mount_is_optional_only_when_the_key_is_not_required(
    settings, harness_settings, catalog, metrics
):
    relaxed = dataclasses.replace(harness_settings, judge_key_required=False)
    loop, f = build(settings, relaxed, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    pod = f.batch.created[0]["spec"]["template"]["spec"]
    vol = next(v for v in pod["volumes"] if v["name"] == "judge-key")
    assert vol["secret"]["optional"] is True


def test_stale_judge_keys_are_kept_while_a_job_is_active(
    settings, harness_settings, catalog, metrics
):
    # A config change mints a new key while a Job runs with the old value
    # (its subPath mount never updates): the old key must survive the run.
    bifrost = FakeBifrost()
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()], bifrost=bifrost)
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()  # no Job yet when the key step runs
    loop.run_once()  # the Job is active
    name = f.batch.created[0]["metadata"]["name"]
    f.batch.finish(name)
    loop.run_once()  # none active
    assert bifrost.delete_stale == [True, False, True]


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


def test_invalid_declaration_is_skipped_and_keeps_history(settings, harness_settings, catalog, metrics):
    # Live finding (campaign S7c): hand-editing a rendered <agent>-harness
    # ConfigMap to a hostile previewService pruned the agent's Jobs, results
    # and series, and the re-rendered ConfigMap re-ran the same revision.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed"])
    f.batch.finish(name)
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0

    edited = make_configmap(
        "team-a", "assistant-harness", {"previewService": "evil.example.com"},
        labels={"harness.oap.io/agent": "assistant"},
    )
    invalid, _ = build(
        settings, harness_settings, catalog, metrics, [], fakes=f,
        malformed=[(edited, "previewService 'evil.example.com' is not a Service name")],
    )
    invalid.run_once()
    assert REASON_INVALID_HARNESS_DECLARATION in f.events.reasons()
    assert name in f.batch.jobs
    assert (PLATFORM_NS, result_configmap_name(name)) in f.core.configmaps
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert sample(metrics, "oap_agent_harness_cases") == 1

    # A new revision while the declaration is invalid starts nothing.
    f.custom.set_rollout("team-a", "assistant", "rev2")
    invalid.run_once()
    assert len(f.batch.created) == 1

    # Fixed again on the already-run revision: no re-run.
    f.custom.set_rollout("team-a", "assistant", "rev1")
    fixed, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    fixed.run_once()
    assert len(f.batch.created) == 1
    assert name in f.batch.jobs


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
    assert result_configmaps(f) == {}
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
        loop.run_once()  # seen finished before the next revision

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
            "dataset_digest": "",
            "checks_digest": "",
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


# ── rollback re-runs (DEF-2) ─────────────────────────────────────────────


def info_labels(metrics):
    return [
        s.labels
        for m in metrics.registry.collect()
        if m.name == "oap_agent_harness_info"
        for s in m.samples
    ]


def score_last(f, d, revision, outcomes, finished_at):
    """Write a result for the newest created Job and mark it finished."""
    body = f.batch.created[-1]
    name = body["metadata"]["name"]
    trigger = body["metadata"]["annotations"]["harness.oap.io/trigger"]
    write_result(f.core, name, d, revision, outcomes, finished_at=finished_at, trigger=trigger)
    f.batch.finish(name, at=finished_at)
    return name


def settle(loop, f, d, revision, outcomes, finished_at):
    """Finish the newest Job and let the reconciler see it finished."""
    name = score_last(f, d, revision, outcomes, finished_at)
    loop.run_once()
    return name


def go_to(loop, f, revision, agent="assistant"):
    f.custom.set_rollout("team-a", agent, revision)
    loop.run_once()


def test_revision_that_becomes_current_again_gets_a_fresh_run(
    settings, harness_settings, catalog, metrics
):
    # Live finding DEF-2: after A -> B -> A the dashboard kept showing B's run
    # as the current result because A's Job already existed.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    first = score_last(f, d, "rev1", ["passed", "failed"], 3_000.0)
    loop.run_once()
    go_to(loop, f, "rev2")
    score_last(f, d, "rev2", ["failed", "failed"], 4_000.0)
    loop.run_once()
    assert [l["revision"] for l in info_labels(metrics)] == ["rev2"]

    go_to(loop, f, "rev1")

    assert len(f.batch.created) == 3
    rerun = f.batch.created[-1]
    name = rerun["metadata"]["name"]
    assert name == harness_job_name("team-a", "assistant", "rev1", "example-smoke-v1", run=2)
    assert name != first
    assert rerun["metadata"]["annotations"]["harness.oap.io/trigger"] == "rollback"
    assert rerun["metadata"]["labels"]["harness.oap.io/run-number"] == "2"
    assert rerun["spec"]["template"]["metadata"]["labels"]["harness.oap.io/run-number"] == "2"
    env = env_of(rerun)
    assert env["HARNESS_RUN_NUMBER"] == "2"
    assert env["HARNESS_TRIGGER"] == "rollback"
    assert env["HARNESS_REVISION"] == "rev1"
    assert env["HARNESS_RESULT_CONFIGMAP"] == result_configmap_name(name)
    started = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_STARTED]
    assert "revision rev1 run 2" in started[-1] and "rollback" in started[-1]
    # Both earlier runs are kept as history.
    assert first in f.batch.jobs

    score_last(f, d, "rev1", ["passed", "passed"], 5_000.0)
    loop.run_once()

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
            "trigger": "rollback",
            "status": "completed",
            "dataset_digest": "",
            "checks_digest": "",
        },
    ) == 1
    assert len(info_labels(metrics)) == 1
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert sample(metrics, "oap_agent_harness_last_run_timestamp_seconds") == 5_000.0
    assert len(f.batch.created) == 3


def test_continuously_current_revision_never_reruns(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    settle(loop, f, d, "rev2", ["passed"], 4_000.0)
    go_to(loop, f, "rev1")
    assert len(f.batch.created) == 3
    for _ in range(5):  # the re-run is active
        loop.run_once()
    score_last(f, d, "rev1", ["passed"], 5_000.0)
    for _ in range(5):  # the re-run is finished
        loop.run_once()
    assert len(f.batch.created) == 3
    assert [n[2] for n in f.events.normals].count(REASON_HARNESS_RUN_STARTED) == 3


def test_restart_does_not_rerun_a_rolled_back_revision(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    settle(loop, f, d, "rev2", ["passed"], 4_000.0)
    go_to(loop, f, "rev1")
    assert len(f.batch.created) == 3

    restarted, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    restarted.run_once()  # the re-run is still active
    assert len(f.batch.created) == 3

    score_last(f, d, "rev1", ["passed", "failed"], 5_000.0)
    again, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    again.run_once()
    again.run_once()
    assert len(f.batch.created) == 3
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 0.5
    assert {l["trigger"] for l in info_labels(metrics)} == {"rollback"}


def test_rollback_history_limit_prunes_per_agent(settings, harness_settings, catalog, metrics):
    hs = dataclasses.replace(harness_settings, history_limit=2)
    d, other = decl(), decl(agent="other")
    loop, f = build(settings, hs, catalog, metrics, [d, other])
    go_to(loop, f, "o1", agent="other")
    other_job = settle(loop, f, other, "o1", ["passed"], 2_500.0)
    go_to(loop, f, "rev1")
    first = settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    second = settle(loop, f, d, "rev2", ["passed"], 4_000.0)
    go_to(loop, f, "rev1")
    rerun = score_last(f, d, "rev1", ["failed"], 5_000.0)
    loop.run_once()
    loop.run_once()

    assert set(f.batch.jobs) == {other_job, second, rerun}
    assert first in f.batch.deleted
    # No result ConfigMap is left without its Job.
    assert {name for (_, name) in result_configmaps(f)} == {
        result_configmap_name(j) for j in (other_job, second, rerun)
    }
    # The pruned first run does not make the re-run look stale: nothing reruns.
    assert len(f.batch.created) == 4
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 0.0
    assert sample(metrics, "oap_agent_harness_pass_ratio", agent="other") == 1.0
    assert {(l["agent"], l["revision"], l["trigger"]) for l in info_labels(metrics)} == {
        ("assistant", "rev1", "rollback"),
        ("other", "o1", "first-revision"),
    }


def test_second_rollback_gets_the_next_run_number(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    for i, rev in enumerate(("rev1", "rev2", "rev1", "rev2", "rev1")):
        go_to(loop, f, rev)
        score_last(f, d, rev, ["passed"], 3_000.0 + i)
        loop.run_once()

    runs = [
        (env_of(b)["HARNESS_REVISION"], b["metadata"]["labels"]["harness.oap.io/run-number"],
         b["metadata"]["annotations"]["harness.oap.io/trigger"])
        for b in f.batch.created
    ]
    assert runs == [
        ("rev1", "1", "first-revision"),
        ("rev2", "1", "new-revision"),
        ("rev1", "2", "rollback"),
        ("rev2", "2", "rollback"),
        ("rev1", "3", "rollback"),
    ]
    assert f.batch.created[-1]["metadata"]["name"] == harness_job_name(
        "team-a", "assistant", "rev1", "example-smoke-v1", run=3
    )


def test_rerun_is_stopped_when_the_preview_moves_while_active(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    settle(loop, f, d, "rev2", ["passed"], 4_000.0)
    go_to(loop, f, "rev1")
    rerun = f.batch.created[-1]["metadata"]["name"]

    go_to(loop, f, "rev2")  # D5: the active re-run is stopped

    assert rerun in f.batch.deleted
    superseded = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_SUPERSEDED]
    assert len(superseded) == 1 and "rev1" in superseded[0] and "rev2" in superseded[0]
    # rev2 came back after rev1's re-run started, so it is re-run too.
    assert env_of(f.batch.created[-1])["HARNESS_REVISION"] == "rev2"
    assert f.batch.created[-1]["metadata"]["annotations"]["harness.oap.io/trigger"] == "rollback"


def test_rerun_that_finished_after_the_preview_moved_is_discarded(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    settle(loop, f, d, "rev2", ["failed"], 4_000.0)
    go_to(loop, f, "rev1")
    rerun = score_last(f, d, "rev1", ["passed"], 5_000.0)

    go_to(loop, f, "rev2")  # D5: finished unseen after the preview moved

    assert rerun in f.batch.deleted
    assert (PLATFORM_NS, result_configmap_name(rerun)) not in f.core.configmaps
    assert REASON_HARNESS_RUN_SUPERSEDED in [n[2] for n in f.events.normals]
    assert not [l for l in info_labels(metrics) if l["revision"] == "rev1"]


def test_job_without_run_number_label_counts_as_run_one(settings, harness_settings, catalog, metrics):
    # Jobs created by a reconciler from before re-runs carry no run-number label.
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    first = settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    del f.batch.jobs[first].metadata.labels["harness.oap.io/run-number"]
    go_to(loop, f, "rev2")
    settle(loop, f, d, "rev2", ["passed"], 4_000.0)
    go_to(loop, f, "rev1")

    assert f.batch.created[-1]["metadata"]["name"] == harness_job_name(
        "team-a", "assistant", "rev1", "example-smoke-v1", run=2
    )
    assert f.batch.created[-1]["metadata"]["labels"]["harness.oap.io/run-number"] == "2"


def test_rollback_rerun_is_created_before_the_run_it_replaces_is_stopped(
    settings, harness_settings, catalog, metrics
):
    # Reviewer repro (C6): A -> B (B active) -> A. If the rollback create
    # fails, B's Job must survive: it is the record the re-run is decided from.
    from kubernetes.client.rest import ApiException

    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    go_to(loop, f, "rev1")
    settle(loop, f, d, "rev1", ["passed"], 3_000.0)
    go_to(loop, f, "rev2")
    b_job = f.batch.created[-1]["metadata"]["name"]

    real_create = f.batch.create_namespaced_job
    failures = [ApiException(status=500, reason="InternalError")]

    def flaky_create(namespace, body):
        if failures:
            raise failures.pop()
        return real_create(namespace, body)

    f.batch.create_namespaced_job = flaky_create
    f.custom.set_rollout("team-a", "assistant", "rev1")
    assert loop.run_once() is False
    assert b_job in f.batch.jobs and b_job not in f.batch.deleted
    assert REASON_HARNESS_RUN_SUPERSEDED not in [n[2] for n in f.events.normals]

    assert loop.run_once()
    rerun = f.batch.created[-1]
    assert rerun["metadata"]["annotations"]["harness.oap.io/trigger"] == "rollback"
    assert b_job in f.batch.deleted
    # Created first, then stopped: superseded is recorded before the start Event.
    assert [n[2] for n in f.events.normals][-2:] == [
        REASON_HARNESS_RUN_SUPERSEDED,
        REASON_HARNESS_RUN_STARTED,
    ]


def test_no_free_slot_still_stops_the_replaced_run(settings, harness_settings, catalog, metrics):
    # Two runs active (assistant, other), then the limit drops to 1: the
    # replaced run is stopped as before, and the new revision waits.
    d, other = decl(), decl(agent="other")
    loop, f = build(settings, harness_settings, catalog, metrics, [d, other])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    f.custom.set_rollout("team-a", "other", "o1")
    loop.run_once()
    old = f.batch.created[0]["metadata"]["name"]
    one = dataclasses.replace(harness_settings, max_concurrent_jobs=1)
    restarted, _ = build(settings, one, catalog, metrics, [d, other], fakes=f)
    f.custom.set_rollout("team-a", "assistant", "rev2")
    restarted.run_once()
    assert old in f.batch.deleted
    assert len(f.batch.created) == 2
    assert REASON_HARNESS_RUN_SUPERSEDED in [n[2] for n in f.events.normals]


def test_aborted_rollout_stops_the_active_run_and_publishes_nothing(
    settings, harness_settings, catalog, metrics
):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]

    f.custom.set_rollout("team-a", "assistant", "rev1", abort=True)
    loop.run_once()

    assert name in f.batch.deleted
    superseded = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_SUPERSEDED]
    assert superseded and "aborted" in superseded[0]
    assert len(f.batch.created) == 1  # nothing starts while aborted
    loop.run_once()
    assert len(f.batch.created) == 1
    assert info_labels(metrics) == []


def test_run_that_finished_after_an_abort_is_discarded(
    settings, harness_settings, catalog, metrics
):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.set_rollout("team-a", "assistant", "rev1")
    loop.run_once()
    name = f.batch.created[0]["metadata"]["name"]
    write_result(f.core, name, d, "rev1", ["passed", "passed"])
    f.batch.finish(name)
    f.custom.set_rollout("team-a", "assistant", "rev1", abort=True)
    loop.run_once()

    assert name in f.batch.deleted
    assert REASON_HARNESS_RUN_COMPLETED not in [n[2] for n in f.events.normals]
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
