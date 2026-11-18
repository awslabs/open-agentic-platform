"""The harness verdict endpoint: every step of Contract 6 with the existing fakes."""
from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.events import (
    REASON_HARNESS_JUDGE_KEY_UNAVAILABLE,
    REASON_HARNESS_PREVIEW_UNSUPPORTED,
    REASON_HARNESS_RUN_STARTED,
    REASON_INVALID_HARNESS_DECLARATION,
    REASON_UNKNOWN_DATASET,
)
from app.harness_api import InvalidRequest, VerdictService, make_handler, serve, validate
from app.harness_job import JobFactory
from app.naming import harness_job_name, result_configmap_name
from tests.conftest import FakeBatch, FakeCore, FakeCustomObjects, FakeEvents, FakeReader, make_configmap
from tests.test_harness_loop import CATALOG_NAME, CATALOG_UID, KEY_SECRET, PLATFORM_NS, decl, write_result

# The fake API server stamps Jobs 1001, 1002 ... so the clock starts just after.
NOW = 1_010.0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def make(settings, harness_settings, catalog, declarations=(), malformed=(), key=True, hs=None):
    hs = hs or harness_settings
    custom = FakeCustomObjects()
    f = SimpleNamespace(batch=FakeBatch(), custom=custom, core=FakeCore(custom), events=FakeEvents(), clock=Clock())
    f.reader = FakeReader(list(declarations), list(malformed))
    f.core.create_namespaced_config_map(
        PLATFORM_NS,
        {"metadata": {"name": CATALOG_NAME, "namespace": PLATFORM_NS, "uid": CATALOG_UID, "labels": {}}, "data": {}},
    )
    if key:
        f.core.create_namespaced_secret(
            PLATFORM_NS,
            {"metadata": {"name": KEY_SECRET, "namespace": PLATFORM_NS}, "data": {"BIFROST_VIRTUAL_KEY": "c2s="}},
        )
    f.service = VerdictService(
        settings, hs, JobFactory(settings, hs, catalog, clock=f.clock), f.reader, f.events,
        f.batch, f.custom, f.core, clock=f.clock,
    )
    return f


def rollout(f, current="hash2", revision="2", **kw):
    f.custom.set_rollout("team-a", "assistant", current, revision=revision, **kw)


def poll(f, revision="2", pod_hash="hash2", agent="assistant", namespace="team-a"):
    return f.service.verdict(namespace, agent, revision, pod_hash)


def job_name(d, revision="2", pod_hash="hash2"):
    return harness_job_name(d.namespace, d.agent, revision, pod_hash, d.dataset)


def age_job(f, name, seconds):
    f.batch.jobs[name].metadata.creation_timestamp = datetime.fromtimestamp(NOW - seconds, timezone.utc)


def test_first_poll_creates_a_suspended_job_and_answers_pending(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    answer = poll(f)
    assert answer["state"] == "pending" and answer["job"] == job_name(d)
    body = f.batch.created[0]
    assert body["metadata"]["namespace"] == PLATFORM_NS
    assert body["metadata"]["ownerReferences"][0]["uid"] == CATALOG_UID
    assert body["metadata"]["labels"]["harness.oap.io/run"] == "true"
    assert body["metadata"]["labels"]["harness.oap.io/revision"] == "hash2"
    assert body["metadata"]["labels"]["harness.oap.io/revision-number"] == "2"
    assert "harness.oap.io/first-seen" in body["metadata"]["annotations"]
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["activeDeadlineSeconds"] == harness_settings.run_timeout
    env = {e["name"]: e.get("value") for e in body["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HARNESS_TARGET_URL"] == "http://assistant-preview.team-a.svc.cluster.local:8083/"
    assert env["HARNESS_REVISION"] == "hash2"
    assert REASON_HARNESS_RUN_STARTED in [n[2] for n in f.events.normals]
    # A slot is free, so the same poll unsuspended it.
    assert f.batch.jobs[job_name(d)].spec.suspend is False


def test_repeated_poll_is_idempotent(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f)
    poll(f)
    first = list(f.batch.created)
    assert poll(f)["state"] == "pending"
    assert f.batch.created == first
    assert len([n for n in f.events.normals if n[2] == REASON_HARNESS_RUN_STARTED]) == 1


def test_create_conflict_answers_pending(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f)
    real = f.batch.create_namespaced_job

    def racing(namespace, body):
        real(namespace, body)
        raise ApiException(status=409, reason="AlreadyExists")

    f.batch.create_namespaced_job = racing
    assert poll(f)["state"] == "pending"


def test_missing_rollout_and_future_revision_answer_not_current_with_no_side_effect(
    settings, harness_settings, catalog
):
    f = make(settings, harness_settings, catalog, [decl()])
    assert poll(f) == {"state": "error", "message": "not the current revision"}
    rollout(f, revision="2")
    assert poll(f, revision="3")["message"] == "not the current revision"
    rollout(f, current="other", revision="2")
    assert poll(f)["message"] == "not the current revision"
    assert f.batch.created == [] and f.events.normals == [] and f.events.warnings == []


def test_stale_revision_poll_answers_pending_with_no_side_effect(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f, current="hash3", revision="3")
    assert poll(f)["state"] == "pending"
    assert f.batch.created == [] and f.batch.deleted == [] and f.events.normals == []


def test_no_event_for_another_teams_agent_before_the_rollout_check(settings, harness_settings, catalog):
    bad = make_configmap("team-a", "assistant-harness", {}, {"harness.oap.io/agent": "assistant"})
    f = make(settings, harness_settings, catalog, malformed=[(bad, "broken")])
    assert poll(f)["message"] == "not the current revision"
    assert f.events.warnings == []


def test_invalid_declaration_answers_error_and_records_an_event(settings, harness_settings, catalog):
    bad = make_configmap("team-a", "assistant-harness", {}, {"harness.oap.io/agent": "assistant"})
    f = make(settings, harness_settings, catalog, malformed=[(bad, "broken")])
    rollout(f)
    assert poll(f)["state"] == "error"
    assert REASON_INVALID_HARNESS_DECLARATION in f.events.reasons()
    assert f.batch.created == []


def test_missing_declaration_unknown_dataset_and_preview_refusals(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog)
    rollout(f)
    assert poll(f)["state"] == "error" and f.batch.created == []

    f = make(settings, harness_settings, catalog, [decl(dataset="not-shipped")])
    rollout(f)
    assert poll(f)["state"] == "error"
    assert REASON_UNKNOWN_DATASET in f.events.reasons()

    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f)
    f.core.set_service("team-a", "assistant-preview", type="ExternalName")
    assert poll(f)["state"] == "error"
    assert REASON_HARNESS_PREVIEW_UNSUPPORTED in f.events.reasons()

    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f)
    f.core.set_service("team-a", "assistant-preview", selector={"rollouts-pod-template-hash": "other"})
    assert poll(f)["message"] == "the preview Service does not select this revision"
    assert f.batch.created == []


def test_dataset_comes_from_the_declaration_not_the_request(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl(dataset="example-smoke-v1", threshold=0.5)])
    rollout(f)
    poll(f)
    env = {e["name"]: e.get("value") for e in f.batch.created[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HARNESS_DATASET"] == "example-smoke-v1" and env["HARNESS_THRESHOLD"] == "0.5"


def test_missing_judge_key_answers_error_and_records_an_event(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl()], key=False)
    rollout(f)
    assert poll(f) == {"state": "error", "message": "the platform judge-key Secret is missing"}
    assert REASON_HARNESS_JUDGE_KEY_UNAVAILABLE in f.events.reasons()
    assert "minted" not in f.events.warnings[0][3]
    assert f.batch.created == []


def test_gate_agent_with_an_optional_key_answers_error(settings, harness_settings, catalog):
    hs = dataclasses.replace(harness_settings, judge_key_required=False)
    f = make(settings, harness_settings, catalog, [decl(mode="gate")], hs=hs)
    rollout(f)
    assert "gate mode needs a required judge key" in poll(f)["message"]
    assert f.batch.created == []


def test_report_agent_with_an_optional_key_still_runs(settings, harness_settings, catalog):
    hs = dataclasses.replace(harness_settings, judge_key_required=False)
    f = make(settings, harness_settings, catalog, [decl()], key=False, hs=hs)
    rollout(f)
    assert poll(f)["state"] == "pending" and len(f.batch.created) == 1


def test_lower_revision_job_is_deleted_only_after_the_checks_pass(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f, current="hash1", revision="1")
    poll(f, revision="1", pod_hash="hash1")
    old = job_name(d, "1", "hash1")
    assert old in f.batch.jobs
    # A bad call for revision 2 (unknown hash) must not touch revision 1's Job.
    rollout(f, current="hash2", revision="2")
    f.core.set_service("team-a", "assistant-preview", selector={"rollouts-pod-template-hash": "wrong"})
    poll(f)
    assert old in f.batch.jobs
    f.core.services.clear()
    assert poll(f)["state"] == "pending"
    assert old not in f.batch.jobs and old in f.batch.deleted


def test_higher_revision_job_exists_answers_superseded(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f, current="hash3", revision="3")
    poll(f, revision="3", pod_hash="hash3")
    # A racing poll of revision 2 that passed step 2 earlier.
    rollout(f, current="hash2", revision="2")
    assert poll(f)["message"] == "superseded by a newer revision"


def test_job_waits_for_a_slot_then_is_unsuspended(settings, harness_settings, catalog):
    hs = dataclasses.replace(harness_settings, max_concurrent_jobs=1)
    a, b = decl("assistant"), decl("helper")
    f = make(settings, harness_settings, catalog, [a, b], hs=hs)
    rollout(f)
    f.custom.set_rollout("team-a", "helper", "hash2", revision="2")
    assert poll(f)["message"] == "the run started"
    waiting = poll(f, agent="helper")
    assert waiting == {"state": "pending", "message": "waiting for a free slot", "job": job_name(b)}
    assert f.batch.jobs[job_name(b)].spec.suspend is True
    f.batch.finish(job_name(a))
    assert poll(f, agent="helper")["state"] == "pending"
    assert f.batch.jobs[job_name(b)].spec.suspend is False


def test_slot_wait_past_the_cap_errors_and_deletes_the_job(settings, harness_settings, catalog):
    hs = dataclasses.replace(harness_settings, max_concurrent_jobs=1)
    a, b = decl("assistant"), decl("helper")
    f = make(settings, harness_settings, catalog, [a, b], hs=hs)
    rollout(f)
    f.custom.set_rollout("team-a", "helper", "hash2", revision="2")
    poll(f)
    poll(f, agent="helper")
    age_job(f, job_name(b), hs.run_timeout + hs.slot_wait_seconds + 1)
    answer = poll(f, agent="helper")
    assert answer["state"] == "error" and answer["message"] == "no slot in time"
    assert job_name(b) in f.batch.deleted


def test_pending_for_the_whole_poll_count_never_ends_as_a_pass(settings, harness_settings, catalog):
    # The AnalysisTemplate polls 160 times, 15 s apart. The endpoint owns the
    # clock and answers error before the last poll.
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    answers = []
    for i in range(160):
        f.clock.now = NOW + i * 15
        answers.append(poll(f)["state"])
        if answers[-1] == "error":
            break  # the failure condition ends the AnalysisRun at the first error
        if i == 0:
            # the Job is never finished and never unsuspended by anything else
            f.batch.jobs[job_name(d)].spec.suspend = True
            f.batch.jobs[job_name(d)].metadata.creation_timestamp = datetime.fromtimestamp(NOW, timezone.utc)
    assert answers[-1] == "error"
    assert len(answers) < 160


def test_active_job_past_the_cap_errors(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    poll(f)
    age_job(f, job_name(d), harness_settings.run_timeout + harness_settings.slot_wait_seconds + 1)
    assert poll(f)["state"] == "error"


def test_failed_job_answers_error(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    poll(f)
    f.batch.finish(job_name(d), state="Failed")
    answer = poll(f)
    assert answer["state"] == "error" and "failed" in answer["message"]


@pytest.mark.parametrize(
    "outcomes,threshold,expected",
    [
        (["passed", "passed", "failed", "passed"], 0.5, "pass"),
        (["passed", "failed", "failed", "passed"], 0.8, "fail"),
        (["passed", "error", "error", "error"], 0.5, "insufficient"),
        (["error", "error", "error", "error"], 0.5, "error"),
    ],
)
def test_complete_job_maps_the_result_to_a_verdict(settings, harness_settings, catalog, outcomes, threshold, expected):
    d = decl(threshold=threshold)
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    poll(f)
    name = job_name(d)
    write_result(f.core, name, d, "hash2", outcomes)
    f.batch.finish(name)
    answer = poll(f)
    assert answer["state"] == expected
    assert poll(f)["state"] == expected  # a repeated poll returns the same verdict
    assert "ratio" not in json.dumps(answer) and "sk-" not in json.dumps(answer)


def test_complete_job_without_result_or_with_foreign_result_errors(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    poll(f)
    name = job_name(d)
    f.batch.finish(name)
    assert "without writing a result" in poll(f)["message"]
    write_result(f.core, name, d, "hash2", ["passed"], owner_uid="uid-of-another-job")
    assert poll(f)["message"] == "the result does not belong to this Job"


def test_end_check_wrong_hash_after_the_run_errors(settings, harness_settings, catalog):
    d = decl()
    f = make(settings, harness_settings, catalog, [d])
    rollout(f)
    poll(f)
    name = job_name(d)
    write_result(f.core, name, d, "hash2", ["passed", "passed"])
    f.batch.finish(name)
    f.core.set_service("team-a", "assistant-preview", selector={"rollouts-pod-template-hash": "moved"})
    assert poll(f)["state"] == "error"


def test_two_concurrent_polls_unsuspend_one_job_with_one_slot(settings, harness_settings, catalog):
    hs = dataclasses.replace(harness_settings, max_concurrent_jobs=1)
    a, b = decl("assistant"), decl("helper")
    f = make(settings, harness_settings, catalog, [a, b], hs=hs)
    rollout(f)
    f.custom.set_rollout("team-a", "helper", "hash2", revision="2")
    barrier = threading.Barrier(2)
    results = {}

    def go(agent):
        barrier.wait()
        results[agent] = poll(f, agent=agent)

    threads = [threading.Thread(target=go, args=(n,)) for n in ("assistant", "helper")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    unsuspended = [j for j in f.batch.jobs.values() if j.spec.suspend is False]
    assert len(unsuspended) == 1


# ── input validation and HTTP ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "",
        "namespace=team-a&agent=assistant&revision=2",
        "namespace=Team&agent=assistant&revision=2&hash=h1",
        "namespace=team-a&agent=assistant&revision=abc&hash=h1",
        "namespace=team-a&agent=assistant&revision=1234567890&hash=h1",
        "namespace=team-a&agent=assistant&revision=2&hash=H1",
        "namespace=team-a&agent=assistant&revision=2&hash=h1&extra=1",
        "namespace=team-a&namespace=team-b&agent=assistant&revision=2&hash=h1",
        "namespace=team-a&agent=" + "a" * 60 + "&revision=2&hash=h1",
        "namespace=team-a&agent=assistant&revision=2&hash=" + "a" * 600,
    ],
)
def test_invalid_input_is_rejected(query):
    with pytest.raises((InvalidRequest, ValueError)):
        validate(query)


def test_valid_input_is_accepted():
    assert validate("namespace=team-a&agent=assistant&revision=2&hash=5ff64c796b") == {
        "namespace": "team-a", "agent": "assistant", "revision": "2", "hash": "5ff64c796b",
    }


@pytest.fixture
def http(settings, harness_settings, catalog):
    f = make(settings, harness_settings, catalog, [decl()])
    rollout(f)
    server = serve(f.service, 0)
    port = server.server_address[1]

    def call(path, method="POST"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield f, call
    server.shutdown()
    server.server_close()


OK = "/v1/verdict?namespace=team-a&agent=assistant&revision=2&hash=hash2"


def test_http_answers_json_and_rejects_other_paths_and_methods(http):
    f, call = http
    status, body = call(OK)
    assert status == 200 and body["state"] == "pending"
    assert call("/other")[0] == 404
    assert call(OK, method="GET")[0] == 405
    assert call("/v1/verdict?namespace=Bad&agent=assistant&revision=2&hash=h")[0] == 400
    assert call("/v1/verdict?" + "a=b&" * 200)[0] == 400


def test_http_kubernetes_failure_is_503(http):
    f, call = http

    def boom(*a, **k):
        raise ApiException(status=500, reason="boom")

    f.custom.get_namespaced_custom_object = boom
    status, body = call(OK)
    assert status == 503 and body["state"] == "error"
