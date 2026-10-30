"""The metrics loop: read score aggregates back out of Langfuse (D5).

Scores are written by the Langfuse worker, not by this reconciler, so they cannot
be counted as they happen. This loop instead reads rolling-window aggregates from
`GET /api/public/metrics` over the `scores-numeric` view and republishes them as
gauges for the AMP scraper.

Two queries per agent per window: one grouped by evaluator for the scored count
and the mean, and one per evaluator with `value >= threshold` for the passed
count. The pass ratio is computed here rather than in Langfuse because the
threshold comes from the agent's strictness, which Langfuse knows nothing about —
that is what lets two agents in one shared rule be held to different bars.

ALL OR NOTHING. A pass that is throttled part-way through publishes nothing and
leaves the previous values in place, because a half-written gauge set reads as a
quality collapse on the dashboard. The error counter rises and the last-success
timestamp stops moving, so the health row shows "rate-limited, data ageing"
instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Catalog, Settings
from .cluster_loop import ClusterPlan
from .langfuse_client import LangfuseClient, LangfuseError
from .metrics import ScoreRow

logger = logging.getLogger(__name__)

VIEW = "scores-numeric"


class WindowError(Exception):
    pass


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


def _tag_filter(tag: str) -> dict:
    return {
        "column": "tags",
        "operator": "any of",
        "type": "arrayOptions",
        "value": [tag],
    }


def _evaluator_filter(names: list[str]) -> dict:
    return {
        "column": "name",
        "operator": "any of",
        "type": "stringOptions",
        "value": names,
    }


def scored_query(tag: str, evaluators: list[str], start: datetime, end: datetime) -> dict:
    """Counts and means for every evaluator of one agent in one window."""
    return {
        "view": VIEW,
        "dimensions": [{"field": "name"}],
        "metrics": [
            {"measure": "count", "aggregation": "count"},
            {"measure": "value", "aggregation": "avg"},
        ],
        "filters": [_tag_filter(tag), _evaluator_filter(evaluators)],
        "timeDimension": None,
        "fromTimestamp": _iso(start),
        "toTimestamp": _iso(end),
        "orderBy": None,
    }


def passed_query(
    tag: str, evaluator: str, threshold: float, start: datetime, end: datetime
) -> dict:
    """Count of one evaluator's scores at or above that agent's threshold."""
    return {
        "view": VIEW,
        "dimensions": [],
        "metrics": [{"measure": "count", "aggregation": "count"}],
        "filters": [
            _tag_filter(tag),
            _evaluator_filter([evaluator]),
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


def _count_of(rows: list[dict]) -> int:
    """Pull a count out of a metrics response row, whatever Langfuse named it."""
    if not rows:
        return 0
    row = rows[0]
    for key in ("count_count", "count", "countCount"):
        if key in row and row[key] is not None:
            return int(row[key])
    return 0


def _row_count_and_avg(row: dict) -> tuple[int, float | None]:
    count = 0
    for key in ("count_count", "count", "countCount"):
        if row.get(key) is not None:
            count = int(row[key])
            break
    average = None
    for key in ("avg_value", "value_avg", "avgValue"):
        if row.get(key) is not None:
            average = float(row[key])
            break
    return count, average


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
        # (namespace, agent) -> {evaluator: threshold}
        by_agent: dict[tuple[str, str], dict[str, float]] = {}
        for row in plan.thresholds:
            by_agent.setdefault((row.namespace, row.agent), {})[
                row.evaluator
            ] = row.threshold

        if not by_agent:
            self._metrics.publish_scores([])
            self._metrics.last_success.labels(cluster, "metrics").set(_now())
            return True

        now = datetime.now(timezone.utc)
        rows: list[ScoreRow] = []

        for window in self._settings.metrics_windows:
            try:
                start = now - window_delta(window)
            except WindowError as exc:
                logger.error("%s", exc)
                self._metrics.errors.labels(cluster, "metrics").inc()
                return False

            for (namespace, agent), thresholds in sorted(by_agent.items()):
                tag = f"agent:{cluster}/{namespace}/{agent}"
                evaluators = sorted(thresholds)
                try:
                    scored = self._client.query_metrics(
                        scored_query(tag, evaluators, start, now)
                    )
                except LangfuseError as exc:
                    self._fail(exc, f"scores for {namespace}/{agent} over {window}")
                    return False

                counts: dict[str, tuple[int, float | None]] = {}
                for row in scored:
                    name = row.get("name")
                    if name:
                        counts[name] = _row_count_and_avg(row)

                for evaluator in evaluators:
                    count, average = counts.get(evaluator, (0, None))
                    passed = 0
                    if count:
                        try:
                            passed = _count_of(
                                self._client.query_metrics(
                                    passed_query(
                                        tag,
                                        evaluator,
                                        thresholds[evaluator],
                                        start,
                                        now,
                                    )
                                )
                            )
                        except LangfuseError as exc:
                            self._fail(
                                exc,
                                f"passed count for {namespace}/{agent} "
                                f"{evaluator} over {window}",
                            )
                            return False
                    rows.append(
                        ScoreRow(
                            cluster=cluster,
                            namespace=namespace,
                            agent=agent,
                            evaluator=evaluator,
                            window=window,
                            scored=count,
                            passed=min(passed, count),
                            average=average,
                        )
                    )

        self._metrics.publish_scores(rows)
        self._metrics.last_success.labels(cluster, "metrics").set(_now())
        return True

    def _fail(self, exc: LangfuseError, what: str) -> None:
        self._metrics.errors.labels(self._settings.cluster, "metrics").inc()
        if exc.is_rate_limited:
            logger.warning(
                "Langfuse rate-limited the metrics loop while reading %s; keeping "
                "the previous values and retrying next pass. Raise "
                "metrics.intervalSeconds if this persists.",
                what,
            )
        else:
            logger.error("metrics loop could not read %s: %s", what, exc)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()
