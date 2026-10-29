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
from .config import DEFAULT_MAPPING, VALID_MAPPING_SOURCES, Catalog, Settings
from .declarations import AgentDeclaration, DeclarationReader, DeclarationError
from .langfuse_client import LangfuseClient, LangfuseError
from .metrics import InfoRow, ThresholdRow
from .naming import (
    JUDGE_PROVIDER,
    evaluator_name,
    is_platform_rule,
    owns,
    rule_name,
)

logger = logging.getLogger(__name__)

# Langfuse's observation-target filter that selects a trace's root observation:
# the span with no parent. This is why rules do not depend on the Strands span
# name (D4).
# Select the agent's root span, so one trace is judged once rather than once per
# child observation.
#
# `isRootObservation`, NOT `parentObservationId is null`. The two are not
# equivalent, and the difference is the whole behaviour: Langfuse documents that
# "observations marked as app roots by the SDK may retain a non-null
# parentObservationId", which is exactly what the Strands/a2a root span does. A
# `parentObservationId is null` filter therefore matches NOTHING for these
# agents — measured on the hub, the rule was active and enabled with sampling 1
# and a correct tag filter, and produced no evaluation job at all, while the same
# rule with no filter scored every observation.
ROOT_OBSERVATION_FILTER = {
    "column": "isRootObservation",
    "operator": "=",
    "type": "boolean",
    "value": True,
}


@dataclass(frozen=True)
class RuleGroup:
    """One Langfuse evaluation rule: an evaluator, a judge and a sampling rate."""

    evaluator: str
    judge: str
    sampling: float
    # Name of the evaluator object in Langfuse, for logging and diagnostics.
    evaluator_object: str
    # Stable Langfuse id of that evaluator. This, not the name, is what the rule
    # is built from: v4 names are not identifiers.
    evaluator_id: str
    tags: tuple[str, ...]

    def name(self, cluster: str) -> str:
        return rule_name(cluster, self.evaluator, self.judge, self.sampling)

    def payload(self, cluster: str) -> dict:
        return {
            "name": self.name(cluster),
            "enabled": True,
            # Evaluators are attached by STABLE ID in v4, and a rule always uses
            # the assigned evaluator's latest version — which is what lets a hub
            # prompt change reach this rule with no action here.
            #
            # variableMapping is null on purpose: it inherits the evaluator's
            # own default mapping, so how a prompt variable is populated is
            # defined once on the evaluator rather than copied into every rule
            # where it could drift.
            "evaluatorAssignments": [
                {"evaluatorId": self.evaluator_id, "variableMapping": None}
            ],
            # Omitted when 1, because the API rejects a null sampling and
            # documents "omit to evaluate every match".
            **({"sampling": self.sampling} if self.sampling < 1 else {}),
            "filter": [
                ROOT_OBSERVATION_FILTER,
                {
                    "column": "tags",
                    "operator": "any of",
                    "type": "arrayOptions",
                    "value": list(self.tags),
                },
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
        self,
        declarations: list[AgentDeclaration],
        published_evaluators: dict[str, str],
    ) -> ClusterPlan:
        """Build this cluster's desired rules.

        `published_evaluators` maps an evaluator object NAME to its stable
        Langfuse id. A name the hub has not published yet is simply absent,
        which is how a cluster running ahead of the hub is detected.
        """
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
                evaluator_id = published_evaluators.get(object_name)
                if evaluator_id is None:
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

                invalid = [
                    source
                    for source in (entry.mapping or DEFAULT_MAPPING).values()
                    if source not in VALID_MAPPING_SOURCES
                ]
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
                        evaluator_id=evaluator_id,
                        tags=(),
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
                evaluator_id=group.evaluator_id,
                tags=tuple(sorted(set(members[key]))),
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
            published = self._published_evaluator_ids()
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

    def _published_evaluator_ids(self) -> dict[str, str]:
        """Evaluator object name -> stable id, for the evaluators the hub owns.

        Only evaluators bound to the platform's judge provider are considered.
        v4 names are not identifiers, so a human could create their own
        `safety` in the UI; attaching a rule to that one would silently score
        agents with somebody's draft prompt.
        """
        ids: dict[str, str] = {}
        for item in self._client.list_evaluators():
            name, ident = item.get("name"), item.get("id")
            if not name or not ident:
                continue
            if (item.get("modelConfig") or {}).get("provider") != JUDGE_PROVIDER:
                continue
            # Oldest wins, so repeated passes keep choosing the same object.
            if name in ids:
                continue
            ids[name] = ident
        return ids

    @staticmethod
    def _rule_matches(current: dict, desired: dict) -> bool:
        """Compare only the fields this loop sets, so a pass is idempotent."""
        # Compare the assigned evaluator IDS. Langfuse echoes assignments back
        # enriched with the resolved version and the evaluator's definition, so
        # comparing whole objects would make every pass look like a change.
        if _assigned_ids(current) != _assigned_ids(desired):
            return False
        if bool(current.get("enabled")) != desired["enabled"]:
            return False
        # Sampling is omitted from the payload when it is 1, because the API
        # rejects null and treats absent as "every match" — so an absent value
        # on either side means 1, not 0.
        if abs(_sampling_of(current) - _sampling_of(desired)) > 1e-9:
            return False
        # The WHOLE filter, not just the agent tags. Comparing only the tags
        # left the reconciler unable to converge on a filter change: when the
        # root-span clause was corrected, every rule already in Langfuse kept
        # the old, non-matching clause forever, because the tags were identical
        # so the rule looked up to date. A rule whose filter cannot be corrected
        # silently scores nothing.
        return _filter_of(current) == _filter_of(desired)


def _sampling_of(rule: dict) -> float:
    value = rule.get("sampling")
    return 1.0 if value is None else float(value)


def _filter_of(rule: dict) -> list[tuple]:
    """A rule's filter, normalised so two equivalent filters compare equal.

    Sorted because clause order is not meaningful, and values are sorted within
    a clause for the same reason. Only the fields this loop sets are included,
    so a field Langfuse adds on read cannot make an unchanged rule look changed.
    """
    out = []
    for clause in rule.get("filter") or []:
        value = clause.get("value")
        if isinstance(value, list):
            value = tuple(sorted(str(v) for v in value))
        out.append(
            (
                str(clause.get("column") or ""),
                str(clause.get("operator") or ""),
                str(clause.get("type") or ""),
                value,
            )
        )
    return sorted(out, key=repr)


def _tags_of(rule: dict) -> list[str]:
    for clause in rule.get("filter") or []:
        if clause.get("column") == "tags":
            return sorted(clause.get("value") or [])
    return []


def _assigned_ids(rule: dict) -> list[str]:
    """The stable ids of the evaluators attached to a rule, sorted."""
    return sorted(
        a.get("evaluatorId") or ((a.get("evaluator") or {}).get("id") or "")
        for a in (rule.get("evaluatorAssignments") or [])
    )


def _mapping_for(evaluator) -> tuple[tuple[str, str], ...]:
    """variable -> observation field for one catalog entry.

    Defaults to the two variables every catalog prompt uses. An entry whose
    prompt also scores tool calls declares the mapping explicitly, because
    `tool_calls` is only present on observations that made any.
    """
    items = evaluator.mapping or DEFAULT_MAPPING
    return tuple(sorted(items.items()))
