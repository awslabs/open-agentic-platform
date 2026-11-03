"""The metrics loop: read score aggregates back out of the backend (D5).

Scores are written by the Langfuse worker, not by this reconciler, so they cannot
be counted as they happen. This loop instead reads rolling-window aggregates and
republishes them as gauges for the AMP scraper.

THREE PROPERTIES SHAPE THIS MODULE.

**Batched, not per agent.** A pass sends `windows x (1 + distinct
(evaluator, threshold) pairs)` queries, not `windows x agents x
(1 + evaluators)`. The scores view can be grouped by the `tags` dimension, so one
query covers every agent in a window, and the per-threshold queries group the
agents that share a threshold. At 100 agents and 3 evaluators that is ~20 queries
a pass instead of ~800, and the count stops growing with the fleet: distinct
(evaluator, threshold) pairs are bounded by the catalog times the three
strictness levels, whatever the agent count.

**Strict about the response shape.** Aggregate keys are read by their documented
names and a row missing one is an error, never a zero. Guessing across candidate
key names turns a backend rename into "no traffic" or a 0% pass rate on the
dashboard, with nothing logged -- the worst failure this component can have,
because it is indistinguishable from a real quality collapse.

**All or nothing.** A pass that fails part-way publishes nothing and leaves the
previous values in place, because a half-written gauge set reads as a quality
collapse. The error counter rises and the last-success timestamp stops moving, so
the health row shows "rate-limited, data ageing" instead.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Catalog, Settings
from .cluster_loop import ClusterPlan
from .langfuse_client import LangfuseClient, LangfuseError
from .metrics import ScoreRow

logger = logging.getLogger(__name__)

VIEW = "scores-numeric"

# The aggregate keys this code reads, and the ONLY ones it accepts.
#
# Langfuse names a metrics aggregate `<aggregation>_<measure>`
# (web/src/features/query/server/queryMetrics.ts), so `count` of `count` is
# `count_count` and `avg` of `value` is `avg_value`. Verified against a real
# response from Langfuse v4.72.0:
#
#   {"avg_value": 0.7272727272727273, "count_count": 33, "name": "helpfulness"}
#
# These are deliberately not a list of candidates to try. The previous version
# tried three spellings each and fell back to 0, which means a rename ships as a
# silent zero rather than an error. A dimension rename is already loud -- the API
# answers 400 and lists the valid dimensions -- and this makes an aggregate
# rename loud too.
COUNT_KEY = "count_count"
AVG_KEY = "avg_value"

# The dimension that carries an agent's identity. Grouping by it is what lets one
# query cover the whole cluster; it returns the observation's WHOLE tag array, so
# the agent has to be recovered from it (see `agent_from_tags`).
TAGS_DIMENSION = "tags"


class WindowError(Exception):
    pass


class MetricsSchemaError(Exception):
    """The backend's response did not have the shape this code requires.

    Raised rather than defaulted, so a schema change on the backend shows up as a
    failed pass with a named cause instead of zeroes on the dashboard.
    """


def window_delta(window: str) -> timedelta:
    """Parse a window like `24h` or `7d` into a timedelta."""
    unit = window[-1:]
    try:
        amount = int(window[:-1])
    except ValueError as exc:
        raise WindowError(f"window {window!r} is not <number><unit>") from exc
    if amount <= 0:
        raise WindowError(f"window {window!r} must be positive")
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    raise WindowError(f"window {window!r} has unit {unit!r}; expected h, d or m")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tag_filter(tags: list[str]) -> dict:
    return {
        "column": "tags",
        "operator": "any of",
        "type": "arrayOptions",
        "value": list(tags),
    }


def _evaluator_filter(names: list[str]) -> dict:
    return {
        "column": "name",
        "operator": "any of",
        "type": "stringOptions",
        "value": list(names),
    }


def scored_query(
    tags: list[str], evaluator_objects: list[str], start: datetime, end: datetime
) -> dict:
    """Counts and means for every (agent, evaluator) in one window, in ONE query.

    `evaluator_objects` are evaluator OBJECT names, which is how Langfuse names
    the score -- `helpfulness--judge-haiku` for a non-default judge, not the
    catalog name.
    """
    return {
        "view": VIEW,
        "dimensions": [{"field": TAGS_DIMENSION}, {"field": "name"}],
        "metrics": [
            {"measure": "count", "aggregation": "count"},
            {"measure": "value", "aggregation": "avg"},
        ],
        "filters": [_tag_filter(tags), _evaluator_filter(evaluator_objects)],
        "timeDimension": None,
        "fromTimestamp": _iso(start),
        "toTimestamp": _iso(end),
        "orderBy": None,
    }


def passed_query(
    tags: list[str],
    evaluator_object: str,
    threshold: float,
    start: datetime,
    end: datetime,
) -> dict:
    """Passed counts for every agent sharing one (evaluator, threshold), in ONE query.

    The threshold comes from each agent's strictness, which the backend knows
    nothing about, so it cannot be pushed into a single grouped query for all
    thresholds at once. Grouping by the threshold instead keeps the query count
    bounded by the catalog rather than by the fleet.
    """
    return {
        "view": VIEW,
        "dimensions": [{"field": TAGS_DIMENSION}],
        "metrics": [{"measure": "count", "aggregation": "count"}],
        "filters": [
            _tag_filter(tags),
            _evaluator_filter([evaluator_object]),
            {
                "column": "value",
                "operator": ">=",
                "type": "number",
                "value": threshold,
            },
        ],
        "timeDimension": None,
        "fromTimestamp": _iso(start),
        "toTimestamp": _iso(end),
        "orderBy": None,
    }


def _require(row: dict, key: str, what: str):
    """Read a documented aggregate key, or fail loudly."""
    if key not in row:
        raise MetricsSchemaError(
            f"{what}: response row {sorted(row)} has no {key!r}. The backend "
            f"renamed its aggregate keys, or the query changed shape. Refusing to "
            f"report 0, which would read as 'no traffic' on the dashboard."
        )
    return row[key]


def row_count(row: dict, what: str) -> int:
    return int(_require(row, COUNT_KEY, what))


def row_count_and_avg(row: dict, what: str) -> tuple[int, float | None]:
    count = int(_require(row, COUNT_KEY, what))
    raw = _require(row, AVG_KEY, what)
    # An average is legitimately null when nothing was aggregated; the count
    # carries that fact, so this is not a schema problem.
    return count, None if raw is None else float(raw)


def agent_from_tags(value, cluster: str) -> str | None:
    """Recover `<namespace>/<agent>` from an observation's tag array.

    The `tags` dimension returns every tag on the observation, not the one that
    matched the filter -- measured:

        ["agent:hub/agents/eval-smoketest", "eval-smoketest", "source:chat"]

    so the scoped tag has to be picked out. Returns None for a row carrying no
    scoped tag for THIS cluster, which is how another cluster's scores sharing
    the project are ignored.
    """
    if not isinstance(value, list):
        raise MetricsSchemaError(
            f"the {TAGS_DIMENSION!r} dimension returned {type(value).__name__}, "
            "expected a list of tags"
        )
    prefix = f"agent:{cluster}/"
    for tag in value:
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


@dataclass(frozen=True)
class _PassedGroup:
    """One passed-count query: an evaluator object, a threshold, and its agents."""

    evaluator_object: str
    threshold: float
    tags: tuple[str, ...]


class MetricsLoop:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        client: LangfuseClient,
        metrics,
        plan_source,
    ):
        self._settings = settings
        self._catalog = catalog
        self._client = client
        self._metrics = metrics
        # A callable returning the last ClusterPlan, so this loop reuses the
        # thresholds the rules were built from instead of resolving them again.
        self._plan_source = plan_source

    def run_once(self) -> bool:
        plan: ClusterPlan | None = self._plan_source()
        if plan is None:
            logger.info("no reconcile pass has completed yet; skipping metrics pass")
            return False

        cluster = self._settings.cluster

        # agent key -> (namespace, agent), and the per-(agent, evaluator) facts.
        agents: dict[str, tuple[str, str]] = {}
        # agent key -> {evaluator_object: (catalog_name, threshold)}
        wanted: dict[str, dict[str, tuple[str, float]]] = {}
        for row in plan.thresholds:
            key = f"{row.namespace}/{row.agent}"
            agents[key] = (row.namespace, row.agent)
            wanted.setdefault(key, {})[row.evaluator_object] = (
                row.evaluator,
                row.threshold,
            )

        if not agents:
            self._metrics.publish_scores([])
            self._metrics.last_success.labels(cluster, "metrics").set(_now())
            return True

        tag_of = {key: f"agent:{cluster}/{key}" for key in agents}
        all_tags = sorted(tag_of.values())
        all_objects = sorted({obj for per in wanted.values() for obj in per})

        now = datetime.now(timezone.utc)
        rows: list[ScoreRow] = []

        logger.debug(
            "metrics pass: %d agent(s), %d window(s), 1 scored query per window "
            "plus at most %d grouped passed queries",
            len(agents),
            len(self._settings.metrics_windows),
            len({(obj, t) for per in wanted.values() for obj, (_c, t) in per.items()}),
        )

        for window in self._settings.metrics_windows:
            try:
                start = now - window_delta(window)
            except WindowError as exc:
                logger.error("%s", exc)
                self._metrics.errors.labels(cluster, "metrics").inc()
                return False

            # ── one query for counts and means across every agent ──────────
            try:
                scored_rows = self._client.query_metrics(
                    scored_query(all_tags, all_objects, start, now)
                )
            except LangfuseError as exc:
                self._fail(exc, f"scores over {window}")
                return False

            # (agent key, evaluator object) -> (count, average)
            observed: dict[tuple[str, str], tuple[int, float | None]] = {}
            try:
                for row in scored_rows:
                    agent_key = agent_from_tags(row.get(TAGS_DIMENSION), cluster)
                    name = row.get("name")
                    if agent_key is None or not name:
                        continue
                    observed[(agent_key, name)] = row_count_and_avg(
                        row, f"scores over {window}"
                    )
            except MetricsSchemaError as exc:
                self._schema_fail(exc)
                return False

            # ── one query per (evaluator, threshold) group ──────────────────
            #
            # Built from what was actually SCORED, not from the plan: an agent
            # with no traffic in this window cannot have a passed count, so
            # including it would spend a query to be told 0. A group whose every
            # agent is idle disappears entirely, which is why an all-idle
            # cluster still costs exactly one query per window.
            grouped: dict[tuple[str, float], list[str]] = {}
            for key, per in wanted.items():
                for obj, (_catalog_name, threshold) in per.items():
                    count, _avg = observed.get((key, obj), (0, None))
                    if count > 0:
                        grouped.setdefault((obj, threshold), []).append(tag_of[key])
            passed_groups = [
                _PassedGroup(obj, threshold, tuple(sorted(group_tags)))
                for (obj, threshold), group_tags in sorted(grouped.items())
            ]

            try:
                passed_by_group = self._run_passed_queries(
                    passed_groups, start, now, window, cluster
                )
            except LangfuseError as exc:
                self._fail(exc, f"passed counts over {window}")
                return False
            except MetricsSchemaError as exc:
                self._schema_fail(exc)
                return False

            for key, (namespace, agent) in sorted(agents.items()):
                for obj, (catalog_name, threshold) in sorted(wanted[key].items()):
                    count, average = observed.get((key, obj), (0, None))
                    passed = passed_by_group.get((obj, threshold), {}).get(key, 0)
                    rows.append(
                        ScoreRow(
                            cluster=cluster,
                            namespace=namespace,
                            agent=agent,
                            # Catalog name: the label stays judge-independent.
                            evaluator=catalog_name,
                            window=window,
                            scored=count,
                            passed=min(passed, count),
                            average=average,
                        )
                    )

        self._metrics.publish_scores(rows)
        self._metrics.last_success.labels(cluster, "metrics").set(_now())
        return True

    def _run_passed_queries(
        self,
        groups: list[_PassedGroup],
        start: datetime,
        end: datetime,
        window: str,
        cluster: str,
    ) -> dict[tuple[str, float], dict[str, int]]:
        """Run the grouped passed-count queries, at BOUNDED concurrency.

        Bounded on purpose, and modestly: these all hit one rate-limited backend
        shared by the whole fleet, so unbounded fan-out converts a large cluster
        into a self-inflicted 429 -- which, under this module's all-or-nothing
        rule, throws away the whole pass. Batching is what made the pass cheap;
        concurrency only shortens the tail. `metrics.concurrency: 1` restores
        fully sequential behaviour.
        """
        results: dict[tuple[str, float], dict[str, int]] = {}
        if not groups:
            return results

        def one(group: _PassedGroup) -> tuple[_PassedGroup, list[dict]]:
            return group, self._client.query_metrics(
                passed_query(
                    list(group.tags),
                    group.evaluator_object,
                    group.threshold,
                    start,
                    end,
                )
            )

        workers = max(1, min(self._settings.metrics_concurrency, len(groups)))
        if workers == 1:
            produced = [one(group) for group in groups]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # list() re-raises the first worker exception here, so a failure
                # still aborts the pass rather than publishing a partial set.
                produced = list(pool.map(one, groups))

        for group, raw in produced:
            per_agent: dict[str, int] = {}
            for row in raw:
                agent_key = agent_from_tags(row.get(TAGS_DIMENSION), cluster)
                if agent_key is None:
                    continue
                per_agent[agent_key] = row_count(
                    row, f"passed count for {group.evaluator_object} over {window}"
                )
            results[(group.evaluator_object, group.threshold)] = per_agent
        return results

    def _fail(self, exc: LangfuseError, what: str) -> None:
        self._metrics.errors.labels(self._settings.cluster, "metrics").inc()
        if exc.is_rate_limited:
            logger.warning(
                "Langfuse rate-limited the metrics loop while reading %s; keeping "
                "the previous values and retrying next pass. Raise "
                "metrics.intervalSeconds, or lower metrics.concurrency, if this "
                "persists.",
                what,
            )
        else:
            logger.error("metrics loop could not read %s: %s", what, exc)

    def _schema_fail(self, exc: MetricsSchemaError) -> None:
        self._metrics.errors.labels(self._settings.cluster, "metrics").inc()
        logger.error(
            "metrics loop refused to publish this pass: %s. Previous values are "
            "left in place; this is a backend contract change, not a quality "
            "change.",
            exc,
        )


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()
