"""The per-cluster loop: this cluster's evaluation rules, and nothing else (D4).

Run by every instance, hub included, because agents run on every cluster that
enables the agent platform. Each pass is a full diff:

1. list this cluster's declarations
2. resolve each (evaluator, judge) to a published evaluator name — read-only,
   because the hub owns evaluators
3. resolve coverage and strictness to a sampling rate and a threshold
4. group agents by (evaluator, judge, sampling), one rule per group
5. delete rules whose group is now empty, only among rules this cluster owns

Two properties are load-bearing.

**Grouping, not one rule per agent.** Langfuse caps active rules per project and
the whole fleet shares one project, so the rule count has to scale with the
number of distinct (evaluator, judge, sampling) combinations rather than with the
number of agents. `strictness` is deliberately not part of the group: the
threshold is applied when the metrics loop computes pass ratios, so two agents in
one rule can still be held to different bars.

**Deletion is scoped by an exact owner token**, not by a cluster-name prefix. See
`naming` for why the prefix in the design is unsafe for cluster names that are
prefixes of each other.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from . import events
from .config import Catalog, Settings
from .declarations import AgentDeclaration, DeclarationReader, DeclarationError
from .langfuse_client import LangfuseClient, LangfuseError
from .metrics import InfoRow, ThresholdRow
from .naming import evaluator_name, is_platform_rule, owns, rule_name

logger = logging.getLogger(__name__)

# Langfuse's observation-target filter that selects a trace's root observation:
# the span with no parent. This is why rules do not depend on the Strands span
# name (D4).
ROOT_OBSERVATION_FILTER = {
    "column": "parentObservationId",
    "operator": "is null",
    "type": "null",
    "value": "",
}

# Observation-target rules may map a variable from one of these only. A catalog
# entry asking for anything else is rejected before a rule is built, because
# Langfuse would answer 400 invalid_variable_mapping mid-pass.
VALID_MAPPING_SOURCES = ("input", "output", "metadata", "tool_calls")

# Variables every catalog prompt uses, when an entry declares no mapping.
DEFAULT_MAPPING = {"input": "input", "output": "output"}


@dataclass(frozen=True)
class RuleGroup:
    """One Langfuse evaluation rule: an evaluator, a judge and a sampling rate."""

    evaluator: str
    judge: str
    sampling: float
    evaluator_object: str
    tags: tuple[str, ...]
    # variable -> observation field. Carried on the RULE, not the evaluator:
    # Langfuse derives an evaluator's variables from its prompt, and every one of
    # them must be mapped here exactly once or the rule is refused.
    mapping: tuple[tuple[str, str], ...] = ()

    def name(self, cluster: str) -> str:
        return rule_name(cluster, self.evaluator, self.judge, self.sampling)

    def payload(self, cluster: str) -> dict:
        return {
            "name": self.name(cluster),
            # An evaluator FAMILY reference, not a bare name: Langfuse resolves
            # it to the latest version when saving, which is what lets a hub
            # prompt change reach this rule with no action here. scope=project
            # distinguishes ours from Langfuse's managed library.
            "evaluator": {
                "name": self.evaluator_object,
                "scope": "project",
                "type": "llm_as_judge",
            },
            "target": "observation",
            "enabled": True,
            "sampling": self.sampling,
            "filter": [
                ROOT_OBSERVATION_FILTER,
                {
                    "column": "tags",
                    "operator": "any of",
                    "type": "arrayOptions",
                    "value": list(self.tags),
                },
            ],
            "mapping": [
                {"variable": variable, "source": source}
                for variable, source in self.mapping
            ],
        }


@dataclass
class ClusterPlan:
    groups: dict[str, RuleGroup]
    thresholds: list[ThresholdRow]
    info: list[InfoRow]
    declared: int
    skipped: int


class ClusterLoop:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        client: LangfuseClient,
        metrics,
        reader: DeclarationReader,
        recorder: events.EventRecorder,
    ):
        self._settings = settings
        self._catalog = catalog
        self._client = client
        self._metrics = metrics
        self._reader = reader
        self._events = recorder
        # Published by the last pass, so the metrics loop uses the same resolved
        # thresholds the rules were built from rather than resolving twice.
        self.last_plan: ClusterPlan | None = None

    # ── planning (pure, so it is unit-testable without a cluster) ─────────

    def plan(
        self, declarations: list[AgentDeclaration], published_evaluators: set[str]
    ) -> ClusterPlan:
        cluster = self._settings.cluster
        groups: dict[str, RuleGroup] = {}
        members: dict[str, list[str]] = defaultdict(list)
        thresholds: list[ThresholdRow] = []
        info: list[InfoRow] = []
        skipped: set[str] = set()

        for decl in declarations:
            judge = decl.judge or self._catalog.judge_default
            if judge not in self._catalog.judge_aliases:
                self._events.warn(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_UNKNOWN_JUDGE,
                    f"judge {judge!r} is not in the platform's judge list "
                    f"{list(self._catalog.judge_aliases)}; no evaluation rule was "
                    "created for this agent. Ask a platform engineer to add the "
                    "alias, or remove evaluation.judge to use the default.",
                )
                skipped.add(decl.key)
                continue

            try:
                sampling = (
                    decl.sampling
                    if decl.sampling is not None
                    else self._catalog.sampling_for(decl.coverage)
                )
            except Exception as exc:  # ConfigError
                self._events.warn(
                    decl.namespace,
                    decl.configmap,
                    events.REASON_INVALID_DECLARATION,
                    str(exc),
                )
                skipped.add(decl.key)
                continue

            info.append(
                InfoRow(
                    cluster=cluster,
                    namespace=decl.namespace,
                    agent=decl.agent,
                    agent_model=decl.model,
                    judge_model=judge,
                    strictness=decl.strictness,
                    coverage=decl.coverage if decl.sampling is None else "explicit",
                )
            )

            for requested in decl.evaluators:
                entry = self._catalog.evaluators.get(requested.name)
                if entry is None:
                    self._events.warn(
                        decl.namespace,
                        decl.configmap,
                        events.REASON_UNKNOWN_EVALUATOR,
                        f"evaluator {requested.name!r} is not in the platform "
                        f"catalog {sorted(self._catalog.evaluators)}; it was skipped. "
                        "Check the name, or ask a platform engineer to add it.",
                    )
                    skipped.add(decl.key)
                    continue

                object_name = evaluator_name(
                    requested.name, judge, self._catalog.judge_default
                )
                if object_name not in published_evaluators:
                    # The hub has not published this evaluator yet. This is what a
                    # cluster upgraded ahead of the hub looks like. Skip just this
                    # evaluator and say so: the rule appears on the pass after the
                    # hub lands it, with no action here.
                    self._events.warn(
                        decl.namespace,
                        decl.configmap,
                        events.REASON_EVALUATOR_NOT_PUBLISHED,
                        f"evaluator {object_name!r} is in this cluster's catalog but "
                        "not yet published to the shared Langfuse project, which the "
                        "hub owns. Waiting for the hub to publish it; no action is "
                        "needed here and the rule appears automatically.",
                    )
                    skipped.add(decl.key)
                    continue

                strictness = requested.strictness or decl.strictness
                try:
                    threshold = entry.threshold(strictness)
                except Exception as exc:  # ConfigError
                    self._events.warn(
                        decl.namespace,
                        decl.configmap,
                        events.REASON_INVALID_DECLARATION,
                        str(exc),
                    )
                    skipped.add(decl.key)
                    continue

                thresholds.append(
                    ThresholdRow(
                        cluster=cluster,
                        namespace=decl.namespace,
                        agent=decl.agent,
                        evaluator=requested.name,
                        threshold=threshold,
                    )
                )

                mapping = _mapping_for(entry)
                invalid = [s for _, s in mapping if s not in VALID_MAPPING_SOURCES]
                if invalid:
                    self._events.warn(
                        decl.namespace,
                        decl.configmap,
                        events.REASON_INVALID_DECLARATION,
                        f"evaluator {requested.name!r} maps a variable to "
                        f"{invalid!r}, which an observation rule cannot supply; "
                        f"valid sources are {list(VALID_MAPPING_SOURCES)}. This is "
                        "a platform catalog error, not something this Application "
                        "can fix.",
                    )
                    skipped.add(decl.key)
                    continue

                key = f"{requested.name}\x00{judge}\x00{sampling:.6f}"
                if key not in members:
                    groups[key] = RuleGroup(
                        evaluator=requested.name,
                        judge=judge,
                        sampling=sampling,
                        evaluator_object=object_name,
                        tags=(),
                        mapping=mapping,
                    )
                members[key].append(decl.trace_tag(cluster))

        # Tags are sorted so an unchanged set of agents produces an identical
        # payload, which is what keeps the PATCH in step 4 a no-op.
        for key, group in list(groups.items()):
            groups[key] = RuleGroup(
                evaluator=group.evaluator,
                judge=group.judge,
                sampling=group.sampling,
                evaluator_object=group.evaluator_object,
                tags=tuple(sorted(set(members[key]))),
                mapping=group.mapping,
            )

        return ClusterPlan(
            groups=groups,
            thresholds=thresholds,
            info=info,
            declared=len(declarations),
            skipped=len(skipped),
        )

    # ── applying ──────────────────────────────────────────────────────────

    def run_once(self) -> bool:
        cluster = self._settings.cluster
        ok = True

        try:
            declarations, malformed = self._reader.list()
        except DeclarationError as exc:
            self._metrics.errors.labels(cluster, "rules").inc()
            logger.error("%s", exc)
            return False

        for cm, message in malformed:
            self._events.warn(
                cm.metadata.namespace,
                cm.metadata.name,
                events.REASON_INVALID_DECLARATION,
                message,
            )

        try:
            published = {
                e["name"] for e in self._client.list_evaluators() if e.get("name")
            }
        except LangfuseError as exc:
            self._metrics.errors.labels(cluster, "rules").inc()
            logger.error("could not list evaluators: %s", exc)
            return False

        plan = self.plan(declarations, published)
        plan.skipped += len(malformed)
        self.last_plan = plan

        try:
            existing_rules = self._client.list_evaluation_rules()
        except LangfuseError as exc:
            self._metrics.errors.labels(cluster, "rules").inc()
            logger.error("could not list evaluation rules: %s", exc)
            return False

        mine = {
            r["name"]: r
            for r in existing_rules
            if r.get("name") and owns(r["name"], cluster)
        }
        others = sum(
            1
            for r in existing_rules
            if r.get("name")
            and is_platform_rule(r["name"])
            and not owns(r["name"], cluster)
        )

        desired = {g.name(cluster): g for g in plan.groups.values()}

        for name, group in sorted(desired.items()):
            payload = group.payload(cluster)
            current = mine.get(name)
            if current is None:
                if len(mine) + 1 > self._settings.max_active_rules - others:
                    # Reporting the cap is better than letting Langfuse 409
                    # part-way through a pass, which would leave some agents
                    # evaluated and some not with nothing saying why.
                    ok = False
                    logger.error(
                        "not creating rule %s: the project is at its active-rule "
                        "cap (%d). This cluster owns %d and other clusters own %d.",
                        name,
                        self._settings.max_active_rules,
                        len(mine),
                        others,
                    )
                    self._metrics.errors.labels(cluster, "rules").inc()
                    continue
                try:
                    self._client.create_evaluation_rule(payload)
                    mine[name] = payload
                    logger.info(
                        "created rule %s (evaluator=%s judge=%s sampling=%s agents=%d)",
                        name,
                        group.evaluator_object,
                        group.judge,
                        group.sampling,
                        len(group.tags),
                    )
                except LangfuseError as exc:
                    ok = False
                    self._metrics.errors.labels(cluster, "rules").inc()
                    logger.error("could not create rule %s: %s", name, exc)
                continue

            if self._rule_matches(current, payload):
                continue
            rule_id = current.get("id")
            if not rule_id:
                continue
            try:
                self._client.update_evaluation_rule(rule_id, payload)
                logger.info("updated rule %s (agents=%d)", name, len(group.tags))
            except LangfuseError as exc:
                ok = False
                self._metrics.errors.labels(cluster, "rules").inc()
                logger.error("could not update rule %s: %s", name, exc)

        for name, rule in sorted(mine.items()):
            if name in desired or not rule.get("id"):
                continue
            try:
                self._client.delete_evaluation_rule(rule["id"])
                logger.info("deleted rule %s: no agent needs it any more", name)
            except LangfuseError as exc:
                ok = False
                self._metrics.errors.labels(cluster, "rules").inc()
                logger.error("could not delete rule %s: %s", name, exc)

        self._metrics.rules_active.labels(cluster).set(len(desired))
        self._metrics.agents_declared.labels(cluster).set(plan.declared)
        self._metrics.agents_skipped.labels(cluster).set(plan.skipped)
        self._metrics.publish_thresholds(plan.thresholds)
        self._metrics.publish_info(plan.info)
        return ok

    @staticmethod
    def _rule_matches(current: dict, desired: dict) -> bool:
        """Compare only the fields this loop sets, so a pass is idempotent."""
        # `evaluator` is a family reference object, and Langfuse echoes it back
        # with the resolved version and id attached, so only the name is
        # comparable. Comparing the whole object would make every pass a change.
        if _evaluator_name_of(current) != _evaluator_name_of(desired):
            return False
        if _mapping_of(current) != _mapping_of(desired):
            return False
        if bool(current.get("enabled")) != desired["enabled"]:
            return False
        if abs(float(current.get("sampling") or 0) - desired["sampling"]) > 1e-9:
            return False
        if (current.get("target") or "") != desired["target"]:
            return False
        return _tags_of(current) == _tags_of(desired)


def _tags_of(rule: dict) -> list[str]:
    for clause in rule.get("filter") or []:
        if clause.get("column") == "tags":
            return sorted(clause.get("value") or [])
    return []


def _evaluator_name_of(rule: dict) -> str:
    """The evaluator family name, however the reference is shaped."""
    evaluator = rule.get("evaluator")
    if isinstance(evaluator, dict):
        return evaluator.get("name") or ""
    return evaluator or ""


def _mapping_of(rule: dict) -> list[tuple[str, str]]:
    return sorted(
        (m.get("variable") or "", m.get("source") or "")
        for m in (rule.get("mapping") or [])
    )


def _mapping_for(evaluator) -> tuple[tuple[str, str], ...]:
    """variable -> observation field for one catalog entry.

    Defaults to the two variables every catalog prompt uses. An entry whose
    prompt also scores tool calls declares the mapping explicitly, because
    `tool_calls` is only present on observations that made any.
    """
    items = evaluator.mapping or DEFAULT_MAPPING
    return tuple(sorted(items.items()))
