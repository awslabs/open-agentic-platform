"""Ownership is the one thing here that can destroy another cluster's state."""

from app.naming import (
    config_hash,
    evaluator_name,
    is_platform_rule,
    owner_token,
    owns,
    rule_name,
)


def test_owner_token_is_fixed_length_hex():
    token = owner_token("dev")
    assert len(token) == 8
    assert all(c in "0123456789abcdef" for c in token)


def test_prefix_ambiguity_is_closed():
    """The nitpick this naming scheme exists for.

    A cluster named `dev` must never see a rule owned by `dev-2` as its own.
    With the design's `oap-<cluster>-` prefix, `oap-dev-` is a prefix of
    `oap-dev-2-safety-abcd1234`, so the `dev` reconciler would find no ConfigMap
    for it and delete another cluster's rule.
    """
    dev_rule = rule_name("dev", "safety", "judge-default", 0.1)
    dev2_rule = rule_name("dev-2", "safety", "judge-default", 0.1)

    assert dev_rule != dev2_rule
    assert owns(dev_rule, "dev")
    assert owns(dev2_rule, "dev-2")
    # The point: neither cluster claims the other's rule.
    assert not owns(dev2_rule, "dev")
    assert not owns(dev_rule, "dev-2")
    # Both are still recognisably platform-owned, so they are never mistaken
    # for a rule a human made in the Langfuse UI.
    assert is_platform_rule(dev_rule) and is_platform_rule(dev2_rule)


def test_underscore_and_hyphen_cluster_names_stay_distinct():
    names = ["prod", "prod-1", "prod_1", "prod-1-a", "a", "ab"]
    tokens = {n: owner_token(n) for n in names}
    assert len(set(tokens.values())) == len(names)
    for name in names:
        rule = rule_name(name, "safety", "judge-default", 0.1)
        assert owns(rule, name)
        for other in names:
            if other != name:
                assert not owns(rule, other)


def test_evaluator_names_in_rules_may_contain_hyphens():
    rule = rule_name("dev", "goal-completion", "judge-default", 0.5)
    assert owns(rule, "dev")
    assert "goal-completion" in rule


def test_non_platform_names_are_never_owned():
    for name in ["", "safety", "oap", "oap-dev", "my-rule", "oap-ZZZZZZZZ-x-y"]:
        assert not owns(name, "dev")
        assert not is_platform_rule(name)


def test_rule_name_is_stable_and_groups_by_judge_and_sampling():
    a = rule_name("dev", "safety", "judge-default", 0.1)
    assert a == rule_name("dev", "safety", "judge-default", 0.1)
    # Same evaluator, different judge or sampling -> a different rule.
    assert a != rule_name("dev", "safety", "judge-strong", 0.1)
    assert a != rule_name("dev", "safety", "judge-default", 0.5)


def test_sampling_is_compared_at_fixed_precision():
    """0.1 and 0.100000 are one group, not two rules for the same thing."""
    assert config_hash("judge-default", 0.1) == config_hash("judge-default", 0.1000000)


def test_evaluator_object_name_carries_a_non_default_judge():
    assert evaluator_name("safety", "judge-default", "judge-default") == "safety"
    assert (
        evaluator_name("safety", "judge-strong", "judge-default")
        == "safety--judge-strong"
    )
