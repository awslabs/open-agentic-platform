"""AgentCore session ids derived from caller-supplied context ids."""

import re

import pytest

from app.agent import _AGENTCORE_ID_MAX_LEN, _sanitize_agentcore_id

# SessionId pattern from the botocore bedrock-agentcore model.
SESSION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-_]*$")


@pytest.mark.parametrize("raw", ["ctx-1", "a2a1791936958", "3f18797a-3f7e-40ea"])
def test_valid_ids_are_unchanged(raw):
    assert _sanitize_agentcore_id(raw) == raw


@pytest.mark.parametrize("raw", ["PodOOMKilled|spoke-dev|ns|pod|hog", "_x", "", "x" * 300, "a b/c"])
def test_other_ids_become_valid_session_ids(raw):
    out = _sanitize_agentcore_id(raw)
    assert SESSION_ID.match(out) and len(out) <= _AGENTCORE_ID_MAX_LEN


@pytest.mark.parametrize("a, b", [("a|b", "a-b"), ("a|b", "a/b"), ("x" * 200, "x" * 201)])
def test_ids_that_clean_to_the_same_string_stay_distinct(a, b):
    assert _sanitize_agentcore_id(a) != _sanitize_agentcore_id(b)
