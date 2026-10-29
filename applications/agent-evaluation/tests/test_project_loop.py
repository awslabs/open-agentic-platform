"""The hub's ownership of the project-wide objects, and the write-on-change rule."""

from __future__ import annotations

import dataclasses

import pytest

from app.langfuse_client import ProjectWriteForbidden
from app.project_loop import JUDGE_PROVIDER, ProjectLoop, connection_payload
from tests.conftest import FakeLangfuse


def build(settings, catalog, metrics, *, hub=True, published=None):
    langfuse = FakeLangfuse(allow_project_writes=hub)
    langfuse.evaluators = list(published or [])
    loop = ProjectLoop(
        dataclasses.replace(settings, role="hub" if hub else "spoke"),
        catalog,
        langfuse,
        metrics,
    )
    return loop, langfuse


def test_hub_publishes_one_evaluator_per_catalog_entry_per_judge_alias(
    settings, catalog, metrics
):
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()

    names = sorted(e["name"] for e in langfuse.evaluators)
    # 3 catalog entries x 2 aliases; the default judge takes the bare name.
    assert names == [
        "goal-completion",
        "goal-completion--judge-strong",
        "helpfulness",
        "helpfulness--judge-strong",
        "safety",
        "safety--judge-strong",
    ]
    for record in langfuse.evaluators:
        expected = (
            "judge-strong" if record["name"].endswith("--judge-strong")
            else "judge-default"
        )
        assert record["modelConfig"]["model"] == expected
        assert record["modelConfig"]["provider"] == JUDGE_PROVIDER


def test_unchanged_catalog_writes_nothing_on_the_second_pass(
    settings, catalog, metrics
):
    """Evaluators have no PATCH: an unconditional POST would mint a version
    every 60 seconds and straddle every cluster's scores across versions."""
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()
    first = [c for c in langfuse.calls if c.startswith("create_evaluator")]
    assert len(first) == 6

    langfuse.calls.clear()
    assert loop.run_once()
    assert not [c for c in langfuse.calls if c.startswith("create_evaluator")]


def test_a_changed_prompt_publishes_exactly_one_new_version(
    settings, catalog, metrics
):
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()
    langfuse.calls.clear()

    changed = dataclasses.replace(
        catalog.evaluators["safety"], prompt="A stricter safety prompt"
    )
    loop._catalog = dataclasses.replace(
        catalog, evaluators={**catalog.evaluators, "safety": changed}
    )
    assert loop.run_once()

    created = [c for c in langfuse.calls if c.startswith("create_evaluator")]
    assert sorted(created) == [
        "create_evaluator:safety",
        "create_evaluator:safety--judge-strong",
    ]
    versions = [e for e in langfuse.evaluators if e["name"] == "safety"]
    assert [v["version"] for v in versions] == [1, 2]


def test_only_the_newest_version_is_diffed(settings, catalog, metrics):
    """An old version lingering in the list must not look like a change."""
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()
    # Simulate an older version still present in the listing.
    stale = dict(langfuse.evaluators[0])
    stale["version"] = 0
    stale["prompt"] = "an older prompt"
    langfuse.evaluators.append(stale)

    langfuse.calls.clear()
    assert loop.run_once()
    assert not [c for c in langfuse.calls if c.startswith("create_evaluator")]


def test_a_spoke_is_refused_at_the_client(settings, catalog, metrics):
    """The guarantee is the code path, because the shared key pair would allow it."""
    loop, langfuse = build(settings, catalog, metrics, hub=False)
    with pytest.raises(ProjectWriteForbidden):
        langfuse.create_evaluator_version({"name": "safety"})


def test_dropped_catalog_entry_is_reported_not_deleted(
    settings, catalog, metrics, caplog
):
    """Deleting it would break another cluster's rules, which this loop cannot see."""
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()

    loop._catalog = dataclasses.replace(
        catalog,
        evaluators={k: v for k, v in catalog.evaluators.items() if k != "safety"},
    )
    langfuse.calls.clear()
    with caplog.at_level("WARNING"):
        assert loop.run_once()

    assert not [c for c in langfuse.calls if c.startswith("delete_evaluator")]
    assert "were NOT deleted" in caplog.text
    assert "safety" in caplog.text


def test_evaluators_made_by_hand_are_left_alone(settings, catalog, metrics, caplog):
    loop, langfuse = build(
        settings,
        catalog,
        metrics,
        published=[{"name": "someones-ui-experiment", "version": 1}],
    )
    with caplog.at_level("WARNING"):
        assert loop.run_once()
    assert "someones-ui-experiment" not in caplog.text


def test_judge_connection_carries_the_virtual_key_in_the_header(
    settings, catalog, metrics
):
    """Bifrost reads x-bf-vk from the header; the bearer token is not used."""
    payload = connection_payload(settings, catalog.judge_aliases)
    assert payload["extraHeaders"]["x-bf-vk"] == "vk-judge"
    assert payload["baseURL"] == settings.bifrost_url
    assert sorted(payload["customModels"]) == ["judge-default", "judge-strong"]


def test_missing_virtual_key_warns_but_still_connects(settings, catalog, metrics, caplog):
    keyless = dataclasses.replace(settings, judge_virtual_key="", role="hub")
    loop, langfuse = build(keyless, catalog, metrics)
    loop._settings = keyless
    with caplog.at_level("WARNING"):
        assert loop.run_once()

    connection = langfuse.connections[0]
    assert "x-bf-vk" not in connection["extraHeaders"]
    assert "un-budgeted" in caplog.text or "not be attributed" in caplog.text


def test_connection_is_upserted_once_when_unchanged(settings, catalog, metrics):
    loop, langfuse = build(settings, catalog, metrics)
    assert loop.run_once()
    langfuse.calls.clear()
    assert loop.run_once()
    assert not [c for c in langfuse.calls if c.startswith("upsert_connection")]
