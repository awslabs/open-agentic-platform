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

    def __init__(self, stored=("", ""), active=False):
        self.stored = stored
        self.active = active
        self.writes = []

    def read_secret(self):
        return self.stored

    def harness_job_active(self):
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
        self.calls.append(dict(method=method, path=path, body=body, content_type=content_type))
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
