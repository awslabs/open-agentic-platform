"""The project loop: the hub's sole ownership of the project-wide objects (D4).

Runs only where `role: hub`. It keeps two kinds of object in step with the hub's
own chart version:

- one evaluator per catalog entry per judge alias, because the judge model is a
  property of the evaluator at the pin and an evaluation rule has no model field
- one judge LLM connection pointing at Bifrost, carrying the judge virtual key

Both are project-wide, so a spoke writing them would mean the last cluster to
reconcile decides how the whole fleet is scored. `LangfuseClient` refuses these
calls unless the instance was built with `allow_project_writes`, so the guarantee
is in the code path rather than in the configuration.

WRITE ONLY ON CHANGE. Evaluators have no PATCH at the pin: a POST with an
existing name mints a new version and moves every rule referencing that name
onto it. That is the mechanism that makes a hub prompt change reach every cluster
with no spoke action — and it is why this loop diffs first. Posting
unconditionally would mint a new version every 60 seconds, so the version history
would be noise and every cluster's scores would straddle versions.
"""

from __future__ import annotations

import logging

from .config import Catalog, Settings
from .langfuse_client import LangfuseClient, LangfuseError
from .naming import evaluator_name

logger = logging.getLogger(__name__)

# The provider name the judge LLM connection is registered under. Evaluators
# reference the judge by this provider plus a model, so it is part of the
# contract between this loop and the evaluator payloads below.
JUDGE_PROVIDER = "oap-bifrost-judge"


def _mapping_for(evaluator) -> list[dict]:
    """Langfuse variable mapping for an observation-target evaluator.

    Defaults cover the two variables every catalog prompt uses. An entry that
    scores tool calls adds a third, which is why the catalog can override it:
    `tool_calls` is only present on observations that made any.
    """
    if evaluator.mapping:
        items = evaluator.mapping
    else:
        items = {"input": "input", "output": "output"}
    return [
        {
            "templateVariable": variable,
            "langfuseObject": "trace" if source.startswith("trace.") else "observation",
            "objectName": None,
            "selectedColumnId": source.split(".")[-1],
        }
        for variable, source in items.items()
    ]


def evaluator_payload(evaluator, judge_alias: str, default_judge: str) -> dict:
    """The POST body for one (catalog entry, judge alias) pair."""
    return {
        "name": evaluator_name(evaluator.name, judge_alias, default_judge),
        "prompt": evaluator.prompt,
        "modelConfig": {
            "provider": JUDGE_PROVIDER,
            "model": judge_alias,
            "modelParams": {},
        },
        "outputSchema": {
            "name": "score",
            "description": evaluator.output_prompt
            or "Return a numeric value between 0 and 1.",
            "reasoning": evaluator.reasoning_prompt or "Explain the score.",
        },
        "scoreType": evaluator.score_type,
        "mapping": _mapping_for(evaluator),
    }


def _differs(existing: dict, desired: dict) -> bool:
    """True when the published evaluator no longer matches the chart.

    Compares only the fields this loop owns. Langfuse adds its own (id, version,
    createdAt, projectId), and comparing those would make every pass look like a
    change.
    """
    if (existing.get("prompt") or "") != desired["prompt"]:
        return True
    existing_model = existing.get("modelConfig") or {}
    desired_model = desired["modelConfig"]
    if existing_model.get("model") != desired_model["model"]:
        return True
    if existing_model.get("provider") != desired_model["provider"]:
        return True
    existing_schema = existing.get("outputSchema") or {}
    desired_schema = desired["outputSchema"]
    for key in ("description", "reasoning"):
        if (existing_schema.get(key) or "") != desired_schema[key]:
            return True
    if (existing.get("scoreType") or "") != desired["scoreType"]:
        return True
    return False


def _latest_by_name(published: list[dict]) -> dict[str, dict]:
    """Keep the highest version of each evaluator name.

    The list endpoint returns every version, and a rule follows the newest, so
    only the newest is worth diffing against.
    """
    latest: dict[str, dict] = {}
    for item in published:
        name = item.get("name")
        if not name:
            continue
        version = item.get("version") or 0
        current = latest.get(name)
        if current is None or (current.get("version") or 0) < version:
            latest[name] = item
    return latest


def connection_payload(settings: Settings, judge_aliases: tuple[str, ...]) -> dict:
    """The judge LLM connection: Bifrost's OpenAI-compatible endpoint.

    The virtual key travels in `extraHeaders` as `x-bf-vk`, which is how Bifrost
    reads it, and not only as the connection's secret key — Langfuse sends the
    secret key as a bearer token, which Bifrost does not use for attribution.
    """
    headers = {}
    if settings.judge_virtual_key:
        headers["x-bf-vk"] = settings.judge_virtual_key
    return {
        "provider": JUDGE_PROVIDER,
        "adapter": "openai",
        "baseURL": settings.bifrost_url,
        # Bifrost authenticates to Bedrock with its own IAM identity, so this
        # field has no credential to carry. Langfuse requires it to be present.
        "secretKey": settings.judge_virtual_key or "not-used-by-bifrost",
        "extraHeaders": headers,
        "withDefaultModels": False,
        # Every alias an evaluator's modelConfig may name. Langfuse validates an
        # evaluator's model against the connection's model list, so an alias
        # missing here would fail the evaluator write rather than the judge call.
        "customModels": list(dict.fromkeys(judge_aliases)),
    }


class ProjectLoop:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        client: LangfuseClient,
        metrics,
    ):
        self._settings = settings
        self._catalog = catalog
        self._client = client
        self._metrics = metrics

    def desired_evaluators(self) -> dict[str, dict]:
        """Every evaluator object the hub's catalog implies, by name."""
        desired: dict[str, dict] = {}
        for evaluator in self._catalog.evaluators.values():
            for alias in self._catalog.judge_aliases:
                payload = evaluator_payload(
                    evaluator, alias, self._catalog.judge_default
                )
                desired[payload["name"]] = payload
        return desired

    def run_once(self) -> bool:
        """One pass. Returns True when the whole pass succeeded."""
        ok = True
        try:
            self._reconcile_connection()
        except LangfuseError as exc:
            ok = False
            self._error("judge LLM connection", exc)

        try:
            published = _latest_by_name(self._client.list_evaluators())
        except LangfuseError as exc:
            self._error("listing evaluators", exc)
            return False

        desired = self.desired_evaluators()
        for name, payload in sorted(desired.items()):
            existing = published.get(name)
            if existing is not None and not _differs(existing, payload):
                continue
            action = "creating" if existing is None else "publishing a new version of"
            try:
                self._client.create_evaluator_version(payload)
                logger.info(
                    "%s evaluator %s (judge=%s)",
                    action,
                    name,
                    payload["modelConfig"]["model"],
                )
            except LangfuseError as exc:
                ok = False
                self._error(f"evaluator {name}", exc)

        # Deleting a dropped catalog entry is deliberately NOT done here. An
        # evaluator still referenced by another cluster's rule would take that
        # cluster's evaluation down, and this loop cannot see other clusters'
        # rules. Report it instead; removing it is a deliberate platform action.
        orphans = sorted(
            name
            for name, record in published.items()
            if name not in desired and self._looks_like_ours(record)
        )
        if orphans:
            logger.warning(
                "evaluators published by a previous catalog are no longer in it "
                "and were NOT deleted (another cluster's rules may still "
                "reference them): %s",
                ", ".join(orphans),
            )
        return ok

    @staticmethod
    def _looks_like_ours(record: dict) -> bool:
        """True when a published evaluator was written by this loop.

        Discriminated on the judge provider rather than on the name, because an
        orphan is by definition a name the catalog NO LONGER has — matching
        against the current catalog could never report one. Every evaluator this
        loop writes names the judge connection it also owns, and an evaluator
        somebody built in the Langfuse UI points at a different connection, so
        this leaves those alone and never mentions them.
        """
        return (record.get("modelConfig") or {}).get("provider") == JUDGE_PROVIDER

    def _reconcile_connection(self) -> None:
        if not self._settings.judge_virtual_key:
            logger.warning(
                "no Bifrost judge virtual key is mounted; the judge LLM connection "
                "will be created without x-bf-vk and judge spend will not be "
                "attributed to a budget. Bifrost runs with is_vk_mandatory=false so "
                "calls still succeed. Create the Secrets Manager entry the chart's "
                "ExternalSecret reads to fix this."
            )
        payload = connection_payload(self._settings, self._catalog.judge_aliases)
        existing = {
            c.get("provider"): c for c in self._client.list_llm_connections()
        }.get(JUDGE_PROVIDER)
        if existing and self._connection_matches(existing, payload):
            return
        self._client.upsert_llm_connection(payload)
        logger.info(
            "upserted judge LLM connection %s -> %s",
            JUDGE_PROVIDER,
            payload["baseURL"],
        )

    @staticmethod
    def _connection_matches(existing: dict, desired: dict) -> bool:
        """Compare what we can see.

        Langfuse redacts the secret key and may redact header values, so this
        compares the base URL, the adapter and the model list. A virtual-key
        rotation is therefore not detected here; it is picked up when the
        ExternalSecret refresh restarts the pod, which is how the chart wires it.
        """
        if (existing.get("baseURL") or "").rstrip("/") != desired["baseURL"].rstrip("/"):
            return False
        if (existing.get("adapter") or "") != desired["adapter"]:
            return False
        return sorted(existing.get("customModels") or []) == sorted(
            desired["customModels"]
        )

    def _error(self, what: str, exc: LangfuseError) -> None:
        self._metrics.errors.labels(self._settings.cluster, "project").inc()
        logger.error("project loop could not reconcile %s: %s", what, exc)
