"""Readiness must not report green while a loop this instance owns is failing.

Found on a live hub: the pod reported Ready while the project loop could not
publish a single evaluator, because readiness rested on the rules loop alone. The
addon looked healthy while nothing could be scored anywhere in the fleet.
"""

from app.main import _State


def test_spoke_is_ready_on_the_rules_loop_alone():
    state = _State(("rules",))
    ready, detail = state.snapshot()
    assert not ready
    assert "rules" in detail["waitingOn"]

    state.loop_ready("rules", agents=2)
    ready, detail = state.snapshot()
    assert ready
    assert detail["status"] == "ready"
    assert detail["agents"] == 2
    assert "waitingOn" not in detail


def test_hub_is_not_ready_until_the_project_loop_works():
    state = _State(("rules", "project"))

    # The rules loop succeeding is not enough on a hub: only the hub can publish
    # the catalog evaluators and the judge connection.
    state.loop_ready("rules", agents=0)
    ready, detail = state.snapshot()
    assert not ready
    assert "project" in detail["waitingOn"]
    assert "rules" not in detail["waitingOn"]

    state.loop_ready("project", evaluators=4)
    ready, _ = state.snapshot()
    assert ready


def test_a_loop_that_starts_failing_takes_readiness_away():
    state = _State(("rules", "project"))
    state.loop_ready("rules")
    state.loop_ready("project")
    assert state.snapshot()[0]

    state.loop_failing("project", "cannot publish the catalog evaluators")
    ready, detail = state.snapshot()
    assert not ready
    assert detail["waitingOn"]["project"] == "cannot publish the catalog evaluators"


def test_the_reason_is_reported_so_readyz_says_why():
    state = _State(("rules",))
    state.loop_failing("rules", "waiting for Kubernetes credentials")
    ready, detail = state.snapshot()
    assert not ready
    assert detail["waitingOn"]["rules"] == "waiting for Kubernetes credentials"
