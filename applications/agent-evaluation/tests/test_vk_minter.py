"""Decision logic of the bifrost chart's judge key minter, with fake clients."""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "gitops/addons/charts/bifrost/files/vk-minter.py"
spec = importlib.util.spec_from_file_location("vk_minter", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

CONFIG = {
    "models": ["judge-default"], "budget_usd": "20", "budget_reset": "1M",
    "rl_requests": "2000", "rl_reset": "1h",
}
NAME = m.vk_name(["judge-default"], "20", "1M", "2000", "1h")
VALUE = "sk-bf-abc123"


class FakeBifrost:
    def __init__(self, keys=None, fail=None):
        self.keys = list(keys or [])
        self.fail = fail
        self.created = []
        self.deleted = []

    def list_keys(self):
        if self.fail:
            raise m.MinterError("GET /x returned HTTP 500", 500)
        return list(self.keys)

    def create_key(self, name, *a):
        if self.fail == "create":
            raise m.MinterError("POST returned HTTP 500", 500)
        self.created.append(name)
        return {"id": "new", "name": name, "value": VALUE}

    def delete_key(self, key_id):
        self.deleted.append(key_id)


class FakeKube:
    """The pre-created Secret: ("", "") while empty."""

    def __init__(self, stored=("", ""), active=False, unfinished=None):
        self.stored = stored
        self.active = active
        # Answers for successive unfinished_harness_job() calls; then `active`.
        self.unfinished = list(unfinished or [])
        self.job_checks = 0
        self.writes = []

    def read_secret(self):
        return self.stored

    def unfinished_harness_job(self):
        self.job_checks += 1
        if self.unfinished:
            return self.unfinished.pop(0)
        return self.active

    def write_secret(self, value, name):
        self.writes.append((value, name))


def test_creates_key_and_writes_secret_when_missing():
    bf, kube = FakeBifrost(), FakeKube()
    assert m.run(bf, kube, CONFIG) == 0
    assert bf.created == [NAME]
    assert kube.writes == [(VALUE, NAME)]


def test_reuses_existing_key_and_does_not_rewrite_current_secret():
    bf = FakeBifrost([{"id": "1", "name": NAME, "value": VALUE}])
    kube = FakeKube(stored=(VALUE, NAME))
    assert m.run(bf, kube, CONFIG) == 0
    assert not bf.created and not kube.writes


def test_value_withheld_reuses_the_secret_value():
    bf = FakeBifrost([{"id": "1", "name": NAME}])
    kube = FakeKube(stored=(VALUE, NAME))
    assert m.run(bf, kube, CONFIG) == 0
    assert not bf.created and not bf.deleted


def test_value_withheld_and_unknown_replaces_key_when_no_job_active():
    bf = FakeBifrost([{"id": "1", "name": NAME}])
    kube = FakeKube()
    assert m.run(bf, kube, CONFIG) == 0
    assert bf.deleted == ["1"] and bf.created == [NAME]


def test_value_withheld_and_unknown_waits_while_a_job_is_active():
    bf = FakeBifrost([{"id": "1", "name": NAME}])
    kube = FakeKube(active=True)
    assert m.run(bf, kube, CONFIG) == 1
    assert not bf.deleted and not bf.created and not kube.writes


def test_bifrost_error_leaves_the_secret_untouched_and_fails():
    kube = FakeKube(stored=(VALUE, NAME))
    assert m.run(FakeBifrost(fail=True), kube, CONFIG) == 1
    assert not kube.writes


def test_create_error_leaves_the_secret_untouched_and_fails():
    kube = FakeKube()
    assert m.run(FakeBifrost(fail="create"), kube, CONFIG) == 1
    assert not kube.writes


def test_stale_keys_are_deleted_only_when_no_job_is_active():
    old = {"id": "old", "name": m.PREFIX + "deadbeef", "value": "sk-bf-old"}
    cur = {"id": "cur", "name": NAME, "value": VALUE}
    bf = FakeBifrost([old, cur])
    assert m.run(bf, FakeKube(stored=(VALUE, NAME), active=True), CONFIG) == 0
    assert bf.deleted == []
    bf = FakeBifrost([old, cur])
    assert m.run(bf, FakeKube(stored=(VALUE, NAME), active=False), CONFIG) == 0
    assert bf.deleted == ["old"]


def test_changed_config_gets_a_new_name():
    assert m.vk_name(["other"], "20", "1M", "2000", "1h") != NAME


def test_output_never_contains_the_key_value(capsys):
    m.run(FakeBifrost(), FakeKube(), CONFIG)
    assert VALUE not in capsys.readouterr().out


class RecordingHttp:
    """Kubernetes API calls, with a fixed status per (method, path) when given."""

    def __init__(self, replies=None, statuses=None):
        self.calls = []
        self.replies = replies or {}
        self.statuses = statuses or {}

    def request(self, method, path, body=None, params=None, content_type="application/json"):
        self.calls.append(dict(method=method, path=path, body=body, params=params, content_type=content_type))
        status = self.statuses.get((method, path))
        if status:
            raise m.MinterError(f"{method} {path} returned HTTP {status}", status)
        return self.replies.get((method, path), {})


SECRET_PATH = "/api/v1/namespaces/ns/secrets/sec"
JOBS_PATH = "/apis/batch/v1/namespaces/ns/jobs"


def test_secret_is_merge_patched_never_created():
    http = RecordingHttp()
    m.Kube(http, "ns", "sec").write_secret(VALUE, NAME)
    [call] = http.calls
    assert call["method"] == "PATCH" and call["path"] == SECRET_PATH
    assert call["content_type"] == "application/merge-patch+json"
    assert base64.b64decode(call["body"]["data"][m.SECRET_KEY]).decode() == VALUE
    assert call["body"]["metadata"] == {"annotations": {m.ANN_KEY_NAME: NAME}}
    assert "kind" not in call["body"]


def test_empty_pre_created_secret_reads_as_no_key():
    http = RecordingHttp(replies={("GET", SECRET_PATH): {"metadata": {"name": "sec"}}})
    assert m.Kube(http, "ns", "sec").read_secret() == ("", "")


def test_missing_secret_is_an_error_and_nothing_is_created():
    bf = FakeBifrost()
    http = RecordingHttp(statuses={("GET", SECRET_PATH): 404})
    assert m.run(bf, m.Kube(http, "ns", "sec"), CONFIG) == 1
    assert not bf.created
    assert [c["method"] for c in http.calls] == ["GET"]


def test_full_run_patches_the_empty_secret_and_never_posts():
    http = RecordingHttp(
        replies={("GET", SECRET_PATH): {"metadata": {"name": "sec"}}, ("GET", JOBS_PATH): {"items": []}}
    )
    assert m.run(FakeBifrost(), m.Kube(http, "ns", "sec"), CONFIG) == 0
    methods = [c["method"] for c in http.calls]
    assert "POST" not in methods and "PUT" not in methods and methods.count("PATCH") == 1


def test_a_job_that_starts_after_the_first_check_still_blocks_the_delete():
    # The old code checked once at the start and deleted on that answer.
    old = {"id": "old", "name": m.PREFIX + "deadbeef", "value": "sk-bf-old"}
    bf = FakeBifrost([old])  # no current key: one is created, the Secret written
    kube = FakeKube(unfinished=[True])
    assert m.run(bf, kube, CONFIG) == 0
    assert kube.writes == [(VALUE, NAME)]
    assert bf.deleted == []


def test_the_job_check_runs_after_the_secret_write():
    order = []
    old = {"id": "old", "name": m.PREFIX + "deadbeef", "value": "sk-bf-old"}

    class Ordered(FakeKube):
        def write_secret(self, value, name):
            order.append("write")
            super().write_secret(value, name)

        def unfinished_harness_job(self):
            order.append("check")
            return False

    bf = FakeBifrost([old])
    assert m.run(bf, Ordered(), CONFIG) == 0
    assert order == ["write", "check"] and bf.deleted == ["old"]


def test_no_job_check_when_there_is_nothing_to_delete():
    bf = FakeBifrost([{"id": "cur", "name": NAME, "value": VALUE}])
    kube = FakeKube(stored=(VALUE, NAME))
    assert m.run(bf, kube, CONFIG) == 0
    assert kube.job_checks == 0


def _job(**status):
    return {"metadata": {"name": "j"}, "status": status}


@pytest.mark.parametrize("job,unfinished", [
    (_job(), True),  # suspended: no pods, no conditions
    (_job(active=1), True),
    (_job(conditions=[{"type": "Suspended", "status": "True"}]), True),
    (_job(conditions=[{"type": "Complete", "status": "True"}]), False),
    (_job(conditions=[{"type": "Failed", "status": "True"}]), False),
])
def test_unfinished_means_no_complete_or_failed_condition(job, unfinished):
    http = RecordingHttp(replies={("GET", JOBS_PATH): {"items": [job]}})
    assert m.Kube(http, "ns", "sec").unfinished_harness_job() is unfinished
    assert http.calls[0]["params"] == {"labelSelector": m.LABEL_RUN}


def test_harness_not_enabled_is_a_no_op(capsys):
    bf = FakeBifrost()
    http = RecordingHttp(statuses={("GET", SECRET_PATH): 403})
    assert m.run(bf, m.Kube(http, "ns", "sec"), CONFIG) == 0
    assert not bf.created and not bf.deleted
    assert [c["method"] for c in http.calls] == ["GET"]
    assert "harness not enabled in ns" in capsys.readouterr().out


def test_other_secret_errors_still_fail():
    for status in (401, 500):
        http = RecordingHttp(statuses={("GET", SECRET_PATH): status})
        assert m.run(FakeBifrost(), m.Kube(http, "ns", "sec"), CONFIG) == 1
