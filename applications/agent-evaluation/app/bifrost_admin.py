"""Mint and own the harness judge virtual key in this cluster's Bifrost.

A Bifrost virtual key is only valid in the governance store of the Bifrost that
minted it, and every cluster runs its own Bifrost, so a key cannot be shipped
from the hub. The reconciler mints a harness-only, budgeted key through the
management API and owns it like it owns its Langfuse rules: the name carries a
hash of the key's configuration, it is written only when that changes, and
stale keys with the prefix are deleted.

The management API is reachable in-cluster without credentials while
governance auth is off (the current Bifrost config). When auth is turned on,
set HARNESS_JUDGE_KEY_MINT=false and let a platform minter own the Secret.
"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx

from .naming import HARNESS_VK_PREFIX

logger = logging.getLogger(__name__)

KEY_VALUE_PREFIX = "sk-bf-"
_PATH = "/api/governance/virtual-keys"
# Bifrost denies every provider key to a virtual key whose provider config omits
# key_ids (allow_all_keys false, no keys), so each judge call fails with "no
# keys found for provider". The wildcard lets the key use the provider's
# configured keys; allowed_models still limits it to the judge model.
PROVIDER_KEY_IDS = ("*",)


class BifrostAdminError(Exception):
    """The management API failed or answered in a shape this code does not understand.

    `status` is the HTTP status of a non-2xx reply, None for every other failure.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def vk_name(
    models,
    budget_usd: float,
    budget_reset: str,
    rl_requests: int,
    rl_reset: str,
) -> str:
    payload = json.dumps(
        {
            "models": sorted(models),
            # Part of the hash so a key minted without provider-key access gets
            # a new name, and is replaced instead of reused.
            "provider_key_ids": list(PROVIDER_KEY_IDS),
            "budget_usd": float(budget_usd),
            "budget_reset": budget_reset,
            "rate_limit_requests": int(rl_requests),
            "rate_limit_reset": rl_reset,
        },
        sort_keys=True,
    )
    return HARNESS_VK_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


class BifrostAdmin:
    def __init__(self, client: httpx.Client, base_url: str):
        self._client = client
        self._base = base_url.rstrip("/")
        # Values this process minted, by key name. Used only when the list call
        # does not return a key's value (unverified on a live Bifrost), so the
        # key is not re-minted on every pass.
        self._minted: dict[str, str] = {}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, self._base + path, **kwargs)
        except httpx.HTTPError as exc:
            raise BifrostAdminError(
                f"{method} {path} failed: {type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise BifrostAdminError(
                f"{method} {path} returned HTTP {response.status_code}",
                status=response.status_code,
            )
        return response

    def _json(self, response: httpx.Response, what: str) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BifrostAdminError(f"{what}: reply is not JSON") from exc
        if not isinstance(payload, dict):
            raise BifrostAdminError(f"{what}: reply is not a JSON object")
        return payload

    def list_harness_keys(self) -> list[dict]:
        payload = self._json(
            self._request("GET", _PATH, params={"search": HARNESS_VK_PREFIX}),
            "list virtual keys",
        )
        if "virtual_keys" not in payload:
            raise BifrostAdminError("list virtual keys: reply has no 'virtual_keys'")
        keys = payload["virtual_keys"] or []
        if not isinstance(keys, list):
            raise BifrostAdminError("list virtual keys: 'virtual_keys' is not a list")
        # `search` is a substring match; ownership is the prefix.
        return [
            k
            for k in keys
            if isinstance(k, dict) and str(k.get("name", "")).startswith(HARNESS_VK_PREFIX)
        ]

    def create_key(
        self,
        name: str,
        models,
        budget_usd: float,
        budget_reset: str,
        rl_requests: int,
        rl_reset: str,
    ) -> dict:
        body = {
            "name": name,
            "description": (
                "Agent harness judge key, owned by the agent-evaluation reconciler. "
                "Report mode only; do not edit by hand."
            ),
            "provider_configs": [
                {
                    "provider": "bedrock",
                    "weight": 1,
                    "allowed_models": list(models),
                    "key_ids": list(PROVIDER_KEY_IDS),
                }
            ],
            "budgets": [{"max_limit": float(budget_usd), "reset_duration": budget_reset}],
            "rate_limit": {
                "request_max_limit": int(rl_requests),
                "request_reset_duration": rl_reset,
            },
            "is_active": True,
        }
        payload = self._json(self._request("POST", _PATH, json=body), "create virtual key")
        key = payload.get("virtual_key")
        if not isinstance(key, dict):
            raise BifrostAdminError("create virtual key: reply has no 'virtual_key'")
        return key

    def delete_key(self, key_id) -> None:
        try:
            self._request("DELETE", f"{_PATH}/{key_id}")
        except BifrostAdminError as exc:
            # Already gone (deleted by hand, or by another pass): the goal is met.
            if exc.status != 404:
                raise

    def ensure_judge_key(
        self,
        models,
        budget_usd: float,
        budget_reset: str,
        rl_requests: int,
        rl_reset: str,
        known: tuple[str, str] | None = None,
        delete_stale: bool = True,
    ) -> str:
        """Return the value of the judge key for this configuration, minting it if needed.

        `known` is (name, value) as last written to the judge-key Secret. When
        the list call returns no readable value, a matching `known` is reused,
        so a restart neither resets the key's budget nor breaks running Jobs
        that mount the current value.

        Other prefixed keys (an older configuration) are deleted only when
        `delete_stale` is true. The caller passes false while a harness Job is
        active: a running Job keeps the value it mounted at start (a subPath
        mount never updates), so deleting its key would fail its judge calls.
        """
        name = vk_name(models, budget_usd, budget_reset, rl_requests, rl_reset)
        if known and known[0] == name and str(known[1]).startswith(KEY_VALUE_PREFIX):
            self._minted.setdefault(name, known[1])
        keys = self.list_harness_keys()

        value = ""
        for key in keys:
            if key.get("name") != name:
                continue
            candidate = str(key.get("value") or "")
            if candidate.startswith(KEY_VALUE_PREFIX):
                value = candidate
            elif name in self._minted:
                value = self._minted[name]
            else:
                # Ours by name but its value is not readable here or in the
                # Secret: replace it rather than run without a key.
                logger.warning(
                    "harness judge key %s has no readable value; replacing it", name
                )
                self.delete_key(key.get("id"))
                continue
            break

        if not value:
            created = self.create_key(
                name, models, budget_usd, budget_reset, rl_requests, rl_reset
            )
            value = str(created.get("value") or "")
            if not value.startswith(KEY_VALUE_PREFIX):
                raise BifrostAdminError(
                    "create virtual key: the new key has no 'sk-bf-' value"
                )
            self._minted[name] = value
            logger.info("minted harness judge key %s", name)

        for key in keys:
            if key.get("name") != name and delete_stale:
                logger.info("deleting stale harness judge key %s", key.get("name"))
                self.delete_key(key.get("id"))
        return value
