"""Unit tests for caller credential propagation."""

import pytest

from app.identity import WORKLOAD_KEY, inbound_auth, outbound, workload_headers


@pytest.fixture(autouse=True)
def clear_context():
    token = inbound_auth.set(None)
    yield
    inbound_auth.reset(token)


@pytest.fixture
def sa_token(tmp_path, monkeypatch):
    """Mount a fake projected ServiceAccount token and return a rewrite helper."""
    path = tmp_path / "token"
    path.write_text("sa-token-v1")
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(path))
    return path


def test_caller_bearer_is_propagated_verbatim(sa_token):
    inbound_auth.set("Bearer caller.jwt.value")
    headers, key = outbound()
    assert headers() == {"Authorization": "Bearer caller.jwt.value"}
    assert key != WORKLOAD_KEY


def test_falls_back_to_service_account_when_no_caller_token(sa_token):
    headers, key = outbound()
    assert headers() == {"Authorization": "Bearer sa-token-v1"}
    assert key == WORKLOAD_KEY


def test_propagation_can_be_disabled(sa_token):
    inbound_auth.set("Bearer caller.jwt.value")
    headers, key = outbound(propagate=False)
    assert headers() == {"Authorization": "Bearer sa-token-v1"}
    assert key == WORKLOAD_KEY


@pytest.mark.parametrize(
    "header",
    ["Basic dXNlcjpwYXNz", "Negotiate abc", "bearer", "Bearer    ", "", None],
)
def test_non_bearer_or_empty_is_not_propagated(header, sa_token):
    inbound_auth.set(header)
    headers, key = outbound()
    assert headers() == {"Authorization": "Bearer sa-token-v1"}
    assert key == WORKLOAD_KEY


def test_bearer_scheme_match_is_case_insensitive(sa_token):
    inbound_auth.set("bEaReR caller.jwt.value")
    headers, _ = outbound()
    assert headers() == {"Authorization": "bEaReR caller.jwt.value"}


def test_distinct_callers_get_distinct_pool_keys(sa_token):
    inbound_auth.set("Bearer alice.jwt")
    _, alice = outbound()
    inbound_auth.set("Bearer bob.jwt")
    _, bob = outbound()
    assert alice != bob
    assert WORKLOAD_KEY not in (alice, bob)


def test_same_caller_token_gives_stable_pool_key(sa_token):
    inbound_auth.set("Bearer alice.jwt")
    _, first = outbound()
    _, second = outbound()
    assert first == second


def test_refreshed_caller_token_gives_a_new_pool_key(sa_token):
    """A new credential must not reuse a pool whose connection bound the old one."""
    inbound_auth.set("Bearer alice.jwt.v1")
    _, before = outbound()
    inbound_auth.set("Bearer alice.jwt.v2")
    _, after = outbound()
    assert before != after


def test_pool_key_does_not_leak_the_credential(sa_token):
    inbound_auth.set("Bearer secret.jwt.value")
    _, key = outbound()
    assert "secret.jwt.value" not in key


def test_workload_token_is_read_at_call_time_not_captured(sa_token):
    """Rotation is picked up: the provider re-reads rather than caching."""
    headers, _ = outbound()
    assert headers() == {"Authorization": "Bearer sa-token-v1"}
    sa_token.write_text("sa-token-v2")
    assert headers() == {"Authorization": "Bearer sa-token-v2"}


def test_no_file_read_on_the_propagated_path(sa_token, monkeypatch):
    """The ServiceAccount token must not be touched when a caller token exists."""
    monkeypatch.setattr("app.identity.open", _fail_on_open, raising=False)
    inbound_auth.set("Bearer caller.jwt.value")
    headers, _ = outbound()
    assert headers() == {"Authorization": "Bearer caller.jwt.value"}


def _fail_on_open(*args, **kwargs):
    raise AssertionError("ServiceAccount token was read on the propagated path")


def test_missing_workload_token_path_yields_no_credential(monkeypatch):
    monkeypatch.delenv("WORKLOAD_TOKEN_PATH", raising=False)
    headers, key = outbound()
    assert headers() == {}
    assert key == WORKLOAD_KEY


def test_unreadable_workload_token_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(tmp_path / "absent"))
    assert workload_headers() == {}
