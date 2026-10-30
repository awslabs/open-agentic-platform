"""The cluster-scoped trace tag continuous agent evaluation filters on.

Langfuse evaluation rules select an agent's traces by
`agent:<cluster>/<namespace>/<name>`. If the tag is missing the rule matches
nothing, so the agent is never judged — and nothing reports an error: the rule
is valid, the agent is healthy, there are simply no scores.

These tests exist because that failure is silent, and because the tag is
produced by the IMAGE while the rule is produced by the PLATFORM, so the two
can drift apart: an older image on a newer platform is configured for
evaluation and quietly never scored.
"""

import importlib

import pytest

import app.config as cfg


@pytest.fixture(autouse=True)
def restore_config_module():
    yield
    importlib.reload(cfg)  # back to the process environment for later tests


def _tags(monkeypatch, **env) -> list[str]:
    """Rebuild the tag list the agent puts on a trace.

    Mirrors app/agent.py `_trace_attributes`, which cannot be imported here: it
    pulls in the model and AWS SDK stack. The part under test — deciding the
    scoped tag — lives in config precisely so it is reachable without that.
    """
    for key in ("CLUSTER_NAME", "AGENT_NAMESPACE", "AGENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    importlib.reload(cfg)
    config = cfg.Config()
    tags = ["source:chat", config.AGENT_NAME]
    scoped = config.scoped_agent_tag()
    if scoped:
        tags.append(scoped)
    return tags


def test_scoped_tag_is_emitted_on_the_platform(monkeypatch):
    tags = _tags(
        monkeypatch,
        CLUSTER_NAME="hub",
        AGENT_NAMESPACE="agents",
        AGENT_NAME="support",
    )
    assert "agent:hub/agents/support" in tags


def test_the_bare_agent_name_tag_is_kept(monkeypatch):
    """Existing Langfuse filters and dashboards select on the bare name.

    The scoped tag is added ALONGSIDE it, never instead of it, so turning on
    evaluation cannot break a view somebody already built.
    """
    tags = _tags(
        monkeypatch,
        CLUSTER_NAME="hub",
        AGENT_NAMESPACE="agents",
        AGENT_NAME="support",
    )
    assert "support" in tags
    assert "source:chat" in tags


def test_no_scoped_tag_outside_the_platform(monkeypatch):
    """A local run has no cluster, so the tag is omitted rather than malformed.

    Emitting `agent://support` would be worse than emitting nothing: it would
    match a rule's tag filter for an agent on no cluster.
    """
    tags = _tags(monkeypatch, AGENT_NAME="support")
    assert not [t for t in tags if t.startswith("agent:")]
    assert "support" in tags


@pytest.mark.parametrize(
    "env",
    [
        {"CLUSTER_NAME": "hub"},  # namespace missing
        {"AGENT_NAMESPACE": "agents"},  # cluster missing
    ],
)
def test_a_half_configured_environment_emits_no_scoped_tag(monkeypatch, env):
    """Both halves are required. One of them alone cannot identify an agent."""
    tags = _tags(monkeypatch, AGENT_NAME="support", **env)
    assert not [t for t in tags if t.startswith("agent:")]


def test_the_tag_distinguishes_the_same_agent_on_two_clusters(monkeypatch):
    """The whole reason the tag is scoped: every cluster shares ONE Langfuse
    project, so the same Application on dev and prod must not collapse into a
    single series.
    """
    dev = _tags(
        monkeypatch, CLUSTER_NAME="dev", AGENT_NAMESPACE="team-a", AGENT_NAME="support"
    )
    prod = _tags(
        monkeypatch, CLUSTER_NAME="prod", AGENT_NAMESPACE="team-a", AGENT_NAME="support"
    )
    assert [t for t in dev if t.startswith("agent:")] == ["agent:dev/team-a/support"]
    assert [t for t in prod if t.startswith("agent:")] == ["agent:prod/team-a/support"]
