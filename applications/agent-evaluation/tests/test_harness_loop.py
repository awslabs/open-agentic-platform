"""The harness reader: metrics and Events from finished AnalysisRuns.

Argo Rollouts decides when a revision is scored and the verdict endpoint starts
the Job (test_harness_api.py). The reader only publishes, announces once and
cleans up.
"""
from __future__ import annotations
import json
from types import SimpleNamespace

from app.events import (
    REASON_HARNESS_GATE_ABORTED,
    REASON_HARNESS_RUN_COMPLETED,
    REASON_HARNESS_RUN_FAILED,
    REASON_HARNESS_RUN_SUPERSEDED,
    REASON_HARNESS_STABLE_UNVERIFIED,
    REASON_INVALID_HARNESS_DECLARATION,
)
from app.harness_declarations import HarnessDeclaration
from app.harness_job import JobFactory
from app.harness_loop import HarnessLoop
from app.harness_results import CaseResult, summarize, to_configmap_data
from app.main import _run_loop
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
KEY_SECRET = "agent-evaluation-harness-judge-key"


def decl(agent="assistant", namespace="team-a", dataset="example-smoke-v1", threshold=0.5, mode="report"):
    return HarnessDeclaration(
        agent=agent,
        namespace=namespace,
        configmap=f"{agent}-harness",
        dataset=dataset,
        checks=("expected-answer",),
        threshold=threshold,
        mode=mode,
    )


def write_result(
    core,
    job_name,
    d,
    revision,
    outcomes,
    finished_at=3_000.0,
    trigger="new-revision",
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
        mode=d.mode,
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


def result_configmaps(f) -> dict:
    """The ConfigMaps in the fake, minus the chart's catalog ConfigMap."""
    return {k: v for k, v in f.core.configmaps.items() if k[1] != CATALOG_NAME}


def sample(metrics, name, agent="assistant", namespace="team-a", dataset="example-smoke-v1"):
    return metrics.registry.get_sample_value(
        name, {"cluster": "dev", "namespace": namespace, "agent": agent, "dataset": dataset}
    )


def build(settings, harness_settings, catalog, metrics, declarations, *, fakes=None, malformed=None, key=True):
    if fakes is None:
        custom = FakeCustomObjects()
        fakes = SimpleNamespace(
            batch=FakeBatch(), custom=custom, core=FakeCore(custom), events=FakeEvents()
        )
    fakes.reader = FakeReader(declarations, malformed)
    fakes.factory = JobFactory(settings, harness_settings, catalog)
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
    if key and (PLATFORM_NS, KEY_SECRET) not in fakes.core.secrets:
        fakes.core.create_namespaced_secret(
            PLATFORM_NS,
            {
                "metadata": {"name": KEY_SECRET, "namespace": PLATFORM_NS},
                "data": {"BIFROST_VIRTUAL_KEY": "c2stYmYtanVkZ2U="},
            },
        )
    loop = HarnessLoop(
        settings, harness_settings, metrics, fakes.reader, fakes.events,
        fakes.batch, fakes.custom, fakes.core,
    )
    return loop, fakes


def make_run(
    f,
    d,
    number,
    pod_hash,
    outcomes=("passed", "passed"),
    *,
    state="pass",
    phase="Successful",
    terminate=False,
    finish=True,
    result=True,
    message="ok",
    job_state="Complete",
    uid=None,
    **extra,
):
    """A Job, its result and the AnalysisRun Rollouts keeps for one revision."""
    name = harness_job_name(d.namespace, d.agent, str(number), pod_hash, d.dataset)
    body = f.factory.job_body(d, pod_hash, str(number), name, owner=None, suspend=False)
    f.batch.create_namespaced_job(PLATFORM_NS, body)
    if result:
        write_result(f.core, name, d, pod_hash, list(outcomes), **extra)
    if finish:
        f.batch.finish(name, state=job_state)
    value = json.dumps({"state": state, "message": message, "job": name})
    f.custom.analysis_runs.append(
        {
            "metadata": {
                "name": f"{d.agent}-{number}",
                "namespace": d.namespace,
                "uid": uid or f"ar-{d.agent}-{number}",
                "labels": {"harness.oap.io/agent": d.agent, "rollouts-pod-template-hash": pod_hash},
                "annotations": {"rollout.argoproj.io/revision": str(number)},
            },
            "spec": {"terminate": True} if terminate else {},
            "status": {
                "phase": phase,
                "metricResults": [
                    {"name": "oap-harness", "measurements": [{"phase": "Successful", "value": value}]}
                ],
            },
        }
    )
    return name




def info_statuses(metrics):
    info = [s for s in metrics.registry.collect() if s.name == "oap_agent_harness_info"][0].samples
    return [s.labels["status"] for s in info]


def test_finished_run_publishes_metrics_and_announces_once(settings, harness_settings, catalog, metrics):
    d = decl(threshold=0.5)
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(
        f, d, 2, "rev2", ["passed", "passed", "failed", "error"],
        dataset_digest="0123456789ab", checks_digest="ba9876543210", min_judged_ratio=0.5,
    )
    assert loop.run_once()
    assert loop.run_once()  # the completion is announced once

    assert sample(metrics, "oap_agent_harness_cases") == 4
    assert sample(metrics, "oap_agent_harness_passed") == 2
    assert sample(metrics, "oap_agent_harness_failed") == 1
    assert sample(metrics, "oap_agent_harness_case_errors") == 1
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 2 / 3
    assert sample(metrics, "oap_agent_harness_threshold_met") == 1
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_info",
        {
            "cluster": "dev", "namespace": "team-a", "agent": "assistant",
            "dataset": "example-smoke-v1", "revision": "rev2", "mode": "report",
            "judge_model": "us.amazon.nova-pro-v1:0", "trigger": "new-revision",
            "status": "completed", "dataset_digest": "0123456789ab",
            "checks_digest": "ba9876543210",
        },
    ) == 1
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) == 1
    completed = [n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert len(completed) == 1
    assert "2/4 passed" in completed[0][3] and "promotion not affected" in completed[0][3]
    assert f.events.warnings == []


def test_failed_dry_run_is_read_from_the_verdict_not_the_run_phase(
    settings, harness_settings, catalog, metrics
):
    # A failed metric under dryRun leaves the AnalysisRun Successful.
    d = decl(threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["passed", "failed"], state="fail", phase="Successful")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 0.5
    assert sample(metrics, "oap_agent_harness_threshold_met") == 0


def test_completed_event_names_the_failed_case_ids(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["passed", "failed", "failed"], state="fail")
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED][0]
    assert "failed: c1, c2" in completed


def test_completed_event_caps_the_failed_list_at_1000_chars(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["failed"] * 200, state="fail")
    loop.run_once()
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED][0]
    assert len(completed) <= 1000
    listed, more = completed.split("; failed: ", 1)[1].rsplit(" +", 1)
    assert more.endswith(" more")
    assert len(listed.split(", ")) + int(more.split()[0]) == 200


def test_run_cut_short_by_the_deadline_and_expect_tools_are_named(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(
        f, d, 2, "rev2", ["passed", "error"],
        error="run deadline reached: 1 of 2 cases not run", expect_tools_ignored=True,
        min_judged_ratio=0.5,
    )
    loop.run_once()
    text = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED][0]
    assert "run deadline reached: 1 of 2 cases not run" in text
    assert "expectTools in this dataset are not checked yet" in text


def test_run_where_every_case_errored_publishes_no_counts(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["error"] * 4, state="error")
    loop.run_once()
    for metric in ("cases", "passed", "failed", "case_errors", "pass_ratio", "threshold_met"):
        assert sample(metrics, f"oap_agent_harness_{metric}") is None, metric
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    assert info_statuses(metrics) == ["error"]
    assert REASON_HARNESS_RUN_FAILED in f.events.reasons()
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "error"}
    ) == 1


def test_insufficient_run_publishes_counts_but_no_ratio(settings, harness_settings, catalog, metrics):
    d = decl(threshold=0.5)
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["passed", "error", "error", "error"], state="insufficient")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_cases") == 4
    assert sample(metrics, "oap_agent_harness_passed") == 1
    assert sample(metrics, "oap_agent_harness_case_errors") == 3
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert sample(metrics, "oap_agent_harness_threshold_met") is None
    assert info_statuses(metrics) == ["insufficient"]
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "insufficient"}
    ) == 1
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert "insufficient: 1 of 4 cases judged" in completed[0]


def test_threshold_change_publishes_the_declared_threshold(settings, harness_settings, catalog, metrics):
    strict = decl(threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [strict])
    make_run(f, strict, 2, "rev2", ["passed", "passed", "failed"], state="fail")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_threshold") == 0.9
    assert sample(metrics, "oap_agent_harness_threshold_met") == 0
    relaxed, _ = build(settings, harness_settings, catalog, metrics, [decl(threshold=0.5)], fakes=f)
    relaxed.run_once()
    assert sample(metrics, "oap_agent_harness_threshold") == 0.5
    assert sample(metrics, "oap_agent_harness_threshold_met") == 1


def test_series_follow_the_declared_dataset(settings, harness_settings, catalog, metrics, harness_dirs):
    smoke_text = (harness_dirs.datasets / "example-smoke-v1.yaml").read_text()
    (harness_dirs.datasets / "other-v1.yaml").write_text(smoke_text.replace("example-smoke-v1", "other-v1"))
    smoke = decl(threshold=0.8)
    other = decl(dataset="other-v1", threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [smoke])
    make_run(f, smoke, 2, "rev2", ["passed", "passed"], finished_at=3_000.0)
    make_run(f, other, 3, "rev3", ["passed", "failed"], finished_at=4_000.0, state="fail")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert sample(metrics, "oap_agent_harness_pass_ratio", dataset="other-v1") is None
    on_other, _ = build(settings, harness_settings, catalog, metrics, [other], fakes=f)
    on_other.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio", dataset="other-v1") == 0.5
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None


def test_failed_job_without_result_is_a_run_error(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", result=False, state="error", job_state="Failed")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    assert info_statuses(metrics) == ["error"]
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "without writing a result" in failed[0]


def test_result_with_unknown_schema_is_a_run_error(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    name = make_run(f, d, 2, "rev2", result=False, state="error")
    f.core.create_namespaced_config_map(
        PLATFORM_NS,
        {
            "metadata": {
                "name": result_configmap_name(name), "namespace": PLATFORM_NS,
                "labels": {"harness.oap.io/result": "true", "harness.oap.io/job": name},
                "ownerReferences": [{"uid": FakeBatch.uid_for(name)}],
            },
            "data": {"result.json": '{"schema": "something/v9"}'},
        },
    )
    loop.run_once()
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "result not understood" in failed[0]


def test_result_owned_by_another_job_is_an_errored_run(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", owner_uid="uid-of-an-older-job", state="error")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") is None
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "from another Job with this name was ignored" in failed[0]


def test_previous_revision_stays_published_while_the_next_run_goes(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["passed", "passed"])
    # Revision 3 is running: its AnalysisRun is not finished and its Job is active.
    name3 = make_run(f, d, 3, "rev3", finish=False, result=False, phase="Running")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert name3 in f.batch.jobs
    # Only Jobs below the published run are deleted: here none.
    assert f.batch.deleted == []


def test_highest_revision_wins_and_older_jobs_are_deleted(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    old = make_run(f, d, 2, "rev2", ["passed", "passed"])
    new = make_run(f, d, 3, "rev3", ["passed", "failed"], state="fail")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 0.5
    assert old not in f.batch.jobs and new in f.batch.jobs
    assert (PLATFORM_NS, result_configmap_name(old)) not in f.core.configmaps


def test_terminated_run_is_superseded_once_and_publishes_nothing(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    name = make_run(f, d, 2, "rev2", terminate=True, finish=False, result=False, phase="Successful")
    loop.run_once()
    loop.run_once()
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "superseded"}
    ) == 1
    superseded = [n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_SUPERSEDED]
    assert len(superseded) == 1
    assert sample(metrics, "oap_agent_harness_cases") is None
    assert name not in f.batch.jobs  # its cases may have reached newer pods


def test_two_passes_over_one_finished_run_add_one_to_the_counter(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2")
    loop.run_once()
    loop.run_once()
    assert metrics.registry.get_sample_value(
        "oap_agent_harness_runs_total", {"cluster": "dev", "result": "completed"}
    ) == 1


def test_restart_republishes_the_series(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2")
    loop.run_once()
    restarted, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    restarted.run_once()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0


def test_pruned_analysis_run_deletes_its_finished_job(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    old = make_run(f, d, 2, "rev2")
    new = make_run(f, d, 3, "rev3")
    f.custom.analysis_runs[:] = [r for r in f.custom.analysis_runs if r["metadata"]["name"] != "assistant-3"]
    loop.run_once()
    # Revision 3's AnalysisRun is gone and revision 2's run is the highest left.
    assert new not in f.batch.jobs
    assert old in f.batch.jobs


def test_gate_run_that_did_not_pass_records_the_abort(settings, harness_settings, catalog, metrics):
    d = decl(threshold=0.9, mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2", ["passed", "failed"], state="fail", phase="Failed")
    loop.run_once()
    aborted = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_GATE_ABORTED]
    assert len(aborted) == 1 and "fail" in aborted[0]
    completed = [n[3] for n in f.events.normals if n[2] == REASON_HARNESS_RUN_COMPLETED]
    assert "gate mode" in completed[0] and "promotion not affected" not in completed[0]
    assert sample(metrics, "oap_agent_harness_threshold_met") == 0


def test_gate_run_that_passed_records_no_abort(settings, harness_settings, catalog, metrics):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2")
    loop.run_once()
    assert REASON_HARNESS_GATE_ABORTED not in f.events.reasons()


def test_run_with_no_verdict_is_a_failed_run(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    f.custom.analysis_runs.append(
        {
            "metadata": {
                "name": "assistant-2", "namespace": "team-a", "uid": "ar-x",
                "labels": {"harness.oap.io/agent": "assistant"},
                "annotations": {"rollout.argoproj.io/revision": "2"},
            },
            "spec": {},
            "status": {"phase": "Error", "metricResults": [{"name": "oap-harness", "measurements": []}]},
        }
    )
    loop.run_once()
    failed = [w[3] for w in f.events.warnings if w[2] == REASON_HARNESS_RUN_FAILED]
    assert failed and "no verdict" in failed[0]


def test_declaration_removed_deletes_jobs_results_and_series(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2")
    loop.run_once()
    assert sample(metrics, "oap_agent_harness_cases") is not None
    gone, _ = build(settings, harness_settings, catalog, metrics, [], fakes=f)
    gone.run_once()
    assert f.batch.jobs == {}
    assert result_configmaps(f) == {}
    assert sample(metrics, "oap_agent_harness_cases") is None


def test_malformed_declaration_is_reported_and_keeps_history(settings, harness_settings, catalog, metrics):
    d = decl()
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "rev2")
    loop.run_once()
    bad = make_configmap("team-a", "assistant-harness", {}, {"harness.oap.io/agent": "assistant"})
    held, _ = build(settings, harness_settings, catalog, metrics, [], fakes=f, malformed=[(bad, "bad dataset")])
    held.run_once()
    assert REASON_INVALID_HARNESS_DECLARATION in f.events.reasons()
    assert sample(metrics, "oap_agent_harness_pass_ratio") == 1.0
    assert len(f.batch.jobs) == 1


def test_judge_key_present_metric_and_required_failure(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    assert loop.run_once() is True
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 1
    del f.core.secrets[(PLATFORM_NS, KEY_SECRET)]
    assert loop.run_once() is False
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 0


def test_empty_pre_created_judge_key_secret_reads_as_missing(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()], key=False)
    f.core.create_namespaced_secret(PLATFORM_NS, {"metadata": {"name": KEY_SECRET, "namespace": PLATFORM_NS}})
    assert loop.run_once() is False
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 0


def test_optional_missing_judge_key_counts_no_error(settings, harness_settings, catalog, metrics):
    import dataclasses
    optional = dataclasses.replace(harness_settings, judge_key_required=False)
    loop, f = build(settings, optional, catalog, metrics, [decl()], key=False)
    assert loop.run_once() is True
    assert metrics.registry.get_sample_value("oap_agent_harness_judge_key_present", {"cluster": "dev"}) == 0
    assert metrics.registry.get_sample_value("oap_agent_eval_errors_total", {"cluster": "dev", "loop": "harness"}) in (None, 0)


def test_the_reader_never_writes_the_judge_key_secret(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])
    loop.run_once()
    assert f.core.secret_writes == ["create"]  # only the fixture's own create


def test_the_reader_no_longer_imports_the_bifrost_admin_client():
    import app.harness_loop as module
    assert not hasattr(module, "BifrostAdmin")
    import importlib.util
    assert importlib.util.find_spec("app.bifrost_admin") is None


def test_listing_failure_counts_a_harness_error(settings, harness_settings, catalog, metrics):
    loop, f = build(settings, harness_settings, catalog, metrics, [decl()])

    def boom(*a, **k):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=500, reason="boom")

    f.batch.list_namespaced_job = boom
    assert loop.run_once() is False
    assert metrics.registry.get_sample_value(
        "oap_agent_eval_errors_total", {"cluster": "dev", "loop": "harness"}
    ) == 1


# ── a gate's stable revision with no pass (fast-track promotion) ──────────


def _stable(f, d, stable_hash, uid="ro-uid-1"):
    f.custom.set_rollout(d.namespace, d.agent, current=stable_hash)
    ro = f.custom.rollouts[(d.namespace, d.agent)]
    ro["status"]["stableRS"] = stable_hash
    ro["metadata"]["uid"] = uid


def unverified(metrics, d, revision):
    return metrics.registry.get_sample_value(
        "oap_agent_harness_unverified_stable",
        {"cluster": "dev", "namespace": d.namespace, "agent": d.agent, "revision": revision},
    )


def unverified_events(f):
    return [w for w in f.events.warnings if w[2] == REASON_HARNESS_STABLE_UNVERIFIED]


def test_gate_stable_revision_with_only_a_failed_run_is_flagged_once_per_process(
    settings, harness_settings, catalog, metrics
):
    d = decl(mode="gate", threshold=0.9)
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", ["passed", "failed"], state="fail", phase="Failed")
    _stable(f, d, "h2")
    assert loop.run_once()
    assert unverified(metrics, d, "h2") == 1
    found = unverified_events(f)
    assert len(found) == 1
    assert found[0][1] == "assistant-harness" and "h2" in found[0][3] and "fail" in found[0][3]
    loop.run_once()
    assert len(unverified_events(f)) == 1
    assert unverified(metrics, d, "h2") == 1
    restarted, _ = build(settings, harness_settings, catalog, metrics, [d], fakes=f)
    restarted.run_once()
    assert len(unverified_events(f)) == 2


def test_gate_stable_revision_that_passed_is_not_flagged(settings, harness_settings, catalog, metrics):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", ["passed", "failed"], state="fail", phase="Failed", uid="ar-a")
    make_run(f, d, 3, "h2", uid="ar-b")  # a retry that passed
    _stable(f, d, "h2")
    loop.run_once()
    assert unverified(metrics, d, "h2") is None
    assert not unverified_events(f)


def test_gate_stable_revision_with_a_terminated_unscored_run_is_flagged(
    settings, harness_settings, catalog, metrics
):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", terminate=True, finish=False, result=False, state="pending", phase="Successful")
    _stable(f, d, "h2")
    loop.run_once()
    assert unverified(metrics, d, "h2") == 1
    assert "no verdict" in unverified_events(f)[0][3]


def test_flag_clears_when_a_passing_revision_becomes_stable(settings, harness_settings, catalog, metrics):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", state="fail", phase="Failed")
    _stable(f, d, "h2")
    loop.run_once()
    assert unverified(metrics, d, "h2") == 1
    make_run(f, d, 3, "h3")
    _stable(f, d, "h3")
    loop.run_once()
    assert unverified(metrics, d, "h2") is None and unverified(metrics, d, "h3") is None


def test_report_mode_stable_revision_is_not_flagged(settings, harness_settings, catalog, metrics):
    d = decl(mode="report")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", state="fail", phase="Successful")
    _stable(f, d, "h2")
    loop.run_once()
    assert unverified(metrics, d, "h2") is None
    assert not unverified_events(f)


def test_stable_revision_with_no_run_is_not_flagged(settings, harness_settings, catalog, metrics):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", state="fail", phase="Failed")
    _stable(f, d, "h-before-the-harness")
    loop.run_once()
    assert unverified(metrics, d, "h-before-the-harness") is None
    assert not unverified_events(f)


def test_gate_with_no_rollout_is_skipped(settings, harness_settings, catalog, metrics):
    d = decl(mode="gate")
    loop, f = build(settings, harness_settings, catalog, metrics, [d])
    make_run(f, d, 2, "h2", state="fail", phase="Failed")
    assert loop.run_once()
    assert not unverified_events(f)
