"""Unit tests for MODEL_TEMPERATURE.

Newer Bedrock models (e.g. Claude Sonnet 5) reject non-default ``temperature``
with a 400, so it is omitted unless MODEL_TEMPERATURE is set, and an invalid
value must fail at import (pod startup) rather than on the first request.
"""

import importlib.util
from pathlib import Path

import pytest

from app import agent as agent_mod

_CONFIG_PATH = Path(agent_mod.__file__).with_name("config.py")


def _load_config(monkeypatch, value):
    """Import a fresh copy of config.py with MODEL_TEMPERATURE set to ``value``."""
    if value is None:
        monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
    else:
        monkeypatch.setenv("MODEL_TEMPERATURE", value)
    spec = importlib.util.spec_from_file_location("_config_under_test", _CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config


@pytest.mark.parametrize(
    "value, expected",
    [(None, None), ("", None), ("none", None), ("NONE", None), (" 0.2 ", 0.2)],
)
def test_parses_temperature(monkeypatch, value, expected):
    assert _load_config(monkeypatch, value).MODEL_TEMPERATURE == expected


def test_invalid_temperature_fails_at_import(monkeypatch):
    with pytest.raises(ValueError):
        _load_config(monkeypatch, "0,7")


@pytest.mark.parametrize("temperature", [None, 0.2])
def test_model_params(monkeypatch, temperature):
    captured = {}
    monkeypatch.setattr(agent_mod, "_model", None)
    monkeypatch.setattr(agent_mod.config, "MODEL_TEMPERATURE", temperature)
    monkeypatch.setattr(agent_mod, "OpenAIModel", lambda **kw: captured.update(kw))

    agent_mod._get_model()

    params = captured["params"]
    if temperature is None:
        assert "temperature" not in params
    else:
        assert params["temperature"] == temperature
