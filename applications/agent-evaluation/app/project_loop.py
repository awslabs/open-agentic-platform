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

from .config import Catalog, ConfigError, Settings
from .langfuse_client import LangfuseClient, LangfuseError
from .config import DEFAULT_MAPPING
from .naming import JUDGE_PROVIDER, evaluator_name

logger = logging.getLogger(__name__)


# Catalog scoreType -> the dataType Langfuse stores the score as. The catalog
# speaks in lowercase because that is how a chart value reads; the API takes an
# uppercase enum.
_DATA_TYPES = {"numeric": "NUMERIC", "boolean": "BOOLEAN", "categorical": "CATEGORICAL"}


def evaluator_payload(
    evaluator, judge_alias: str, default_judge: str, judge_model: str
) -> dict:
    """The request body for one (catalog entry, judge alias) pair.

    Shape verified against the live v4 contract
    (`CreateLlmAsJudgeEvaluatorRequest`): name, type, prompt and
    outputDefinition are required; modelConfig and variableMapping are optional.

    Used for both the create and the new-version calls, which take the same
    definition body.

    `variableMapping` lives HERE in v4, as the evaluator's default, and a rule's
    `evaluatorAssignments` inherit it by passing `null`. That is the opposite of
    v3, where the mapping was only valid on the rule. Keeping it on the
    evaluator means one definition of how a prompt variable is populated, rather
    than a copy per rule that could drift.

    `version` is deliberately absent: the contract calls it an internal
    persistence detail and rejects it.
    """
    data_type = _DATA_TYPES.get(evaluator.score_type.lower())
    if data_type is None:
        raise ConfigError(
            f"evaluator {evaluator.name!r} has scoreType "
            f"{evaluator.score_type!r}; expected one of {sorted(_DATA_TYPES)}"
        )
    return {
        "name": evaluator_name(evaluator.name, judge_alias, default_judge),
        # Omitting this defaults to llm_as_judge for backwards compatibility, but
        # the contract asks new clients to send it explicitly.
        "type": "llm_as_judge",
        "prompt": evaluator.prompt,
        "outputDefinition": {
            "dataType": data_type,
            "reasoning": {
                "description": evaluator.reasoning_prompt or "Explain the score."
            },
            "score": {
                "description": evaluator.output_prompt
                or "Return a numeric value between 0 and 1."
            },
        },
        "variableMapping": [
            {"variable": variable, "source": source}
            for variable, source in sorted(
                (evaluator.mapping or DEFAULT_MAPPING).items()
            )
        ],
        # Pins the judge for this evaluator. Without it Langfuse falls back to the
        # project's default evaluation model, which the platform does not set — so
        # omitting it would make the judge choice implicit and unreviewable.
        #
        # `model` is the gateway's own model id, UNQUALIFIED. The earlier
        # `<provider>/<model>` form came from misreading an error that Bifrost
        # had raised and Langfuse merely passed through; v4 documents this field
        # as "Model identifier exposed by the provider", and the provider is the
        # separate field beside it. The connection's `customModels` must list the
        # same string.
        "modelConfig": {"provider": JUDGE_PROVIDER, "model": judge_model},
    }


def _mapping_of(definition: dict) -> list[tuple[str, str]]:
    return sorted(
        (m.get("variable") or "", m.get("source") or "")
        for m in (definition.get("variableMapping") or [])
    )


def _prompt_text(value) -> str:
    """Normalise a prompt to plain text for comparison.

    A prompt is SENT as a string but comes BACK as a list of chat messages, so
    comparing the two shapes directly reports a difference on every pass — which
    here means minting a new evaluator version a minute.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            str(m.get("content") or "") for m in value if isinstance(m, dict)
        ).strip()
    return ""


def _differs(existing: dict, desired: dict) -> bool:
    """True when the published evaluator no longer matches the chart.

    Compares only the fields this loop owns AND that Langfuse echoes back.
    Langfuse adds its own (id, version, variables, createdAt, versionId...), and
    comparing those would make every pass look like a change — which matters
    more than usual here, because a "change" mints a new version.

    KNOWN GAP: the read model returns `outputDefinition` as just
    `{"dataType": ...}`; the reasoning and score descriptions are accepted on
    write but never returned. They therefore cannot be diffed, so editing only a
    description in the catalog does not republish on its own. Change the prompt
    alongside it, or bump the evaluator name, if it has to take effect. Guessing
    "changed" instead would mint a version every pass forever.
    """
    if _prompt_text(existing.get("prompt")) != _prompt_text(desired["prompt"]):
        return True

    existing_model = existing.get("modelConfig") or {}
    desired_model = desired["modelConfig"]
    if existing_model.get("model") != desired_model["model"]:
        return True
    if existing_model.get("provider") != desired_model["provider"]:
        return True

    existing_output = existing.get("outputDefinition") or {}
    desired_output = desired["outputDefinition"]
    if (existing_output.get("dataType") or "") != desired_output["dataType"]:
        return True
    # The reasoning/score descriptions are deliberately NOT compared: see the
    # docstring — they are write-only, so diffing them never converges.

    if _mapping_of(existing) != _mapping_of(desired):
        return True

    # A paused evaluator scores nothing, and replacing its definition is what
    # returns it to active. So treat "paused for a bad model config" as a
    # difference even when the definition matches: that is the one pause this
    # loop can clear by itself, and it is exactly what happens when a judge
    # model is fixed in Bifrost after the evaluator was first published.
    if (existing.get("status") or "").lower() == "paused" and (
        existing.get("pausedReason") in _CLEARABLE_PAUSE_REASONS
    ):
        return True
    return False


# Pauses that republishing the definition resolves. Others (provider auth,
# billing, connectivity, account state) need an explicit reactivation check and
# are left alone, because re-posting on every pass would mint a version a minute
# without fixing anything.
_CLEARABLE_PAUSE_REASONS = frozenset(
    {"EVAL_MODEL_CONFIG_INVALID", "DEFAULT_EVAL_MODEL_MISSING"}
)


def _ours_by_name(published: list[dict], wanted: set[str]) -> dict[str, dict]:
    """Index the evaluators this platform owns by name.

    v4 lists one entry per evaluator with its latest definition already
    flattened in, so there is no version collapsing to do. What there IS is a
    duplicate-name risk: names are not identifiers in v4, so the project can
    legitimately hold several evaluators called `safety`. Only ours are
    considered — matched on name AND on our judge provider — and if two of ours
    somehow share a name, the oldest is kept so the choice is stable across
    passes rather than flipping with list order.
    """
    ours: dict[str, dict] = {}
    for item in published:
        name = item.get("name")
        if not name or name not in wanted:
            continue
        if (item.get("modelConfig") or {}).get("provider") != JUDGE_PROVIDER:
            continue
        current = ours.get(name)
        if current is None or str(item.get("createdAt") or "") < str(
            current.get("createdAt") or ""
        ):
            ours[name] = item
    return ours


def connection_payload(settings: Settings, judge_models: tuple[str, ...]) -> dict:
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
        # Every gateway model id an evaluator's modelConfig may name. Langfuse
        # validates an evaluator's model against this list, so a model missing
        # here fails the evaluator write rather than the judge call. These are
        # the resolved model ids, not the platform's alias labels.
        "customModels": list(dict.fromkeys(judge_models)),
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
                    evaluator,
                    alias,
                    self._catalog.judge_default,
                    self._catalog.judge_model(alias),
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

        desired = self.desired_evaluators()
        try:
            all_published = self._client.list_evaluators()
        except LangfuseError as exc:
            self._error("listing evaluators", exc)
            return False
        published = _ours_by_name(all_published, set(desired))

        for name, payload in sorted(desired.items()):
            existing = published.get(name)
            try:
                if existing is None:
                    # No evaluator of ours carries this name yet. In v4 a POST
                    # to /evaluators always creates a NEW object rather than
                    # upserting, so this must only run when we genuinely have
                    # none — otherwise the project fills with duplicates that
                    # all score the same traces.
                    created = self._client.create_evaluator(payload)
                    logger.info(
                        "created evaluator %s id=%s judge=%s status=%s",
                        name,
                        created.get("id"),
                        payload["modelConfig"]["model"],
                        created.get("status"),
                    )
                    self._warn_if_paused(name, created)
                elif _differs(existing, payload):
                    # Keyed by id, not name: replacing the definition publishes
                    # a new VERSION of the same evaluator, which every assigned
                    # rule picks up automatically. This is also what clears a
                    # model-config pause.
                    updated = self._client.update_evaluator(existing["id"], payload)
                    logger.info(
                        "published version %s of evaluator %s id=%s status=%s",
                        updated.get("version"),
                        name,
                        existing["id"],
                        updated.get("status"),
                    )
                    self._warn_if_paused(name, updated)
            except LangfuseError as exc:
                ok = False
                self._error(f"evaluator {name}", exc)

        # Deleting a dropped catalog entry is deliberately NOT done here. An
        # evaluator still referenced by another cluster's rule would take that
        # cluster's evaluation down, and this loop cannot see other clusters'
        # rules. Report it instead; removing it is a deliberate platform action.
        orphans = sorted(
            f"{record.get('name')} ({record.get('id')})"
            for record in all_published
            if record.get("name") not in desired and self._looks_like_ours(record)
        )
        if orphans:
            logger.warning(
                "evaluators published by a previous catalog are no longer in it "
                "and were NOT deleted (another cluster's rules may still "
                "reference them): %s",
                ", ".join(orphans),
            )
        return ok

    def _warn_if_paused(self, name: str, record: dict) -> None:
        """Say so loudly when Langfuse publishes an evaluator but pauses it.

        A paused evaluator scores nothing, so this is the difference between
        "evaluation is configured" and "evaluation is running". It is not an
        error from the API's point of view — the write returned 201 — so without
        this the hub would look entirely healthy while no trace was ever judged.
        """
        if (record.get("status") or "").lower() != "paused":
            return
        reason = record.get("pausedReason") or "unknown"
        self._metrics.errors.labels(self._settings.cluster, "project").inc()
        logger.error(
            "evaluator %s was published but Langfuse PAUSED it (%s): %s. It will "
            "score nothing until this is resolved. For "
            "EVAL_MODEL_CONFIG_INVALID the usual cause is the judge model "
            "refusing the structured-output call Langfuse validates with — on "
            "Bedrock through Bifrost that is a forced tool call, which some "
            "models reject.",
            name,
            reason,
            record.get("pausedMessage") or "no message",
        )

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
        payload = connection_payload(
            self._settings, tuple(self._catalog.judge_models.values())
        )
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
