"""The harness judge key: minted once, reused by exact name, stale keys deleted."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.bifrost_admin import BifrostAdmin, BifrostAdminError, vk_name

ADMIN = "http://bifrost.bifrost.svc.cluster.local:8080"
KEYS = f"{ADMIN}/api/governance/virtual-keys"
ARGS = (("us.amazon.nova-pro-v1:0",), 20.0, "1M", 2000, "1h")
NAME = vk_name(*ARGS)


def admin(client):
    return BifrostAdmin(client, ADMIN)


def test_name_is_prefixed_and_config_hashed():
    assert NAME.startswith("oap-harness-judge-")
    assert len(NAME) == len("oap-harness-judge-") + 8
    assert vk_name(("us.amazon.nova-pro-v1:0",), 30.0, "1M", 2000, "1h") != NAME


@respx.mock
def test_creates_the_key_when_absent():
    respx.get(KEYS).mock(return_value=httpx.Response(200, json={"virtual_keys": []}))
    create = respx.post(KEYS).mock(
        return_value=httpx.Response(
            200, json={"virtual_key": {"id": "vk1", "name": NAME, "value": "sk-bf-new"}}
        )
    )
    with httpx.Client() as client:
        assert admin(client).ensure_judge_key(*ARGS) == "sk-bf-new"
    body = json.loads(create.calls.last.request.content)
    assert body["name"] == NAME
    assert body["provider_configs"] == [
        {"provider": "bedrock", "weight": 1, "allowed_models": ["us.amazon.nova-pro-v1:0"]}
    ]
    assert body["budgets"] == [{"max_limit": 20.0, "reset_duration": "1M"}]
    assert body["rate_limit"] == {"request_max_limit": 2000, "request_reset_duration": "1h"}


@respx.mock
def test_reuses_the_key_with_the_exact_name():
    respx.get(KEYS).mock(
        return_value=httpx.Response(
            200, json={"virtual_keys": [{"id": "vk1", "name": NAME, "value": "sk-bf-old"}]}
        )
    )
    create = respx.post(KEYS)
    with httpx.Client() as client:
        assert admin(client).ensure_judge_key(*ARGS) == "sk-bf-old"
    assert not create.called


@respx.mock
def test_stale_prefixed_key_is_deleted_and_others_left_alone():
    respx.get(KEYS).mock(
        return_value=httpx.Response(
            200,
            json={
                "virtual_keys": [
                    {"id": "vk1", "name": NAME, "value": "sk-bf-old"},
                    {"id": "vk2", "name": "oap-harness-judge-deadbeef", "value": "sk-bf-x"},
                    {"id": "vk3", "name": "team-key-oap-harness-judge-", "value": "sk-bf-y"},
                ]
            },
        )
    )
    stale = respx.delete(f"{KEYS}/vk2").mock(return_value=httpx.Response(200, json={}))
    other = respx.delete(f"{KEYS}/vk3")
    with httpx.Client() as client:
        assert admin(client).ensure_judge_key(*ARGS) == "sk-bf-old"
    assert stale.called
    assert not other.called


@respx.mock
def test_missing_virtual_keys_member_is_an_error():
    respx.get(KEYS).mock(return_value=httpx.Response(200, json={"keys": []}))
    with httpx.Client() as client, pytest.raises(BifrostAdminError):
        admin(client).ensure_judge_key(*ARGS)


@respx.mock
def test_http_error_is_an_error():
    respx.get(KEYS).mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(BifrostAdminError):
        admin(client).ensure_judge_key(*ARGS)
