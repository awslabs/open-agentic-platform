"""PROPAGATE_CALLER_TOKEN defaults to the agent's own identity.

The platform chart always sets it; this guards images run without the chart (a
hand-written Deployment, an older chart on a newer image) from silently falling back
to forwarding the caller's token.
"""

import importlib

import pytest

import app.config as cfg


@pytest.fixture(autouse=True)
def restore_config_module():
    yield
    importlib.reload(cfg)  # back to the process environment for later tests


def _propagate(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROPAGATE_CALLER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PROPAGATE_CALLER_TOKEN", value)
    return importlib.reload(cfg).Config().PROPAGATE_CALLER_TOKEN


@pytest.mark.parametrize("value", [None, "", "false", "False", "0", "no", "off", "ture", " "])
def test_anything_but_an_explicit_true_keeps_workload_identity(monkeypatch, value):
    assert _propagate(monkeypatch, value) is False


@pytest.mark.parametrize("value", ["true", "TRUE", " true ", "1", "yes"])
def test_explicit_true_forwards_the_caller_token(monkeypatch, value):
    assert _propagate(monkeypatch, value) is True
