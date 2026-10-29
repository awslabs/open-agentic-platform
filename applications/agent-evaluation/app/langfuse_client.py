"""Langfuse public API client, scoped to the four surfaces this addon uses.

Auth is HTTP Basic with the seeded project's public/secret key pair — the same
pair the OTel Collector uses for ingestion, so no admin key is minted (D4).

The evaluator and evaluation-rule endpoints are under `/api/public/unstable/` at
the pinned Langfuse v3.225.11 and are expected to move to a stable path on a v4
upgrade (Q2), so the prefix is one constant.

Role separation is enforced here, in the code path, not by a second credential:
the shared key pair would allow a spoke to write an evaluator, so
`allow_project_writes` gates those methods and raises if a spoke ever reaches
them. That makes "a spoke never writes an evaluator" a property of the program
rather than a convention.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UNSTABLE = "/api/public/unstable"
PUBLIC = "/api/public"


class LangfuseError(Exception):
    """A Langfuse call failed. Carries the status code when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429

    @property
    def is_rule_limit(self) -> bool:
        """Langfuse returns 409 past the active-rule cap for a project."""
        return self.status == 409


class ProjectWriteForbidden(LangfuseError):
    """A spoke instance attempted to write a project-wide object."""


class LangfuseClient:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        *,
        allow_project_writes: bool,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.allow_project_writes = allow_project_writes
        self._client = client or httpx.Client(
            base_url=self.base_url,
            auth=(public_key, secret_key),
            timeout=timeout,
            # Langfuse sits behind CloudFront for a spoke; follow the redirect
            # it issues for a path without a trailing slash rather than
            # treating a 3xx as a failure.
            follow_redirects=True,
            headers={"User-Agent": "oap-agent-evaluation/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    # ── transport ─────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LangfuseError(f"{method} {path} failed: {exc}") from exc

        if response.status_code >= 400:
            # Never include the request body in the message: an evaluator POST
            # carries a judge prompt, and a 4xx is logged.
            raise LangfuseError(
                f"{method} {path} returned {response.status_code}: "
                f"{response.text[:300]}",
                status=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise LangfuseError(f"{method} {path} returned non-JSON body") from exc

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Collect every page of a list endpoint.

        Langfuse list endpoints are 1-indexed on `page` and report totals in
        `meta`. Without this, a project with more than one page of evaluators
        would look as though the later ones did not exist, and the hub would
        re-POST them on every pass.
        """
        items: list[dict] = []
        page = 1
        while True:
            body = self._call(
                "GET", path, params={**(params or {}), "page": page, "limit": 100}
            )
            batch = (body or {}).get("data") or []
            items.extend(batch)
            meta = (body or {}).get("meta") or {}
            total_pages = meta.get("totalPages")
            if total_pages is None:
                if len(batch) < 100:
                    break
                page += 1
                continue
            if page >= int(total_pages):
                break
            page += 1
        return items

    def _require_project_write(self, what: str) -> None:
        if not self.allow_project_writes:
            raise ProjectWriteForbidden(
                f"refusing to write {what}: this instance runs with role=spoke, and "
                "the hub owns the project-wide Langfuse objects (D4). A spoke that "
                "needs a new evaluator waits for the hub to publish it."
            )

    # ── evaluators (hub writes, every role reads) ─────────────────────────

    def list_evaluators(self) -> list[dict]:
        return self._paged(f"{UNSTABLE}/evaluators")

    def create_evaluator_version(self, payload: dict) -> dict:
        """POST an evaluator. An existing name creates the next version.

        At the pin there is no PATCH: posting a name that already exists mints a
        new version and every rule referencing that name moves to it. That is
        what makes a hub prompt change reach every cluster with no spoke action
        — and also why the project loop must compare before posting.
        """
        self._require_project_write(f"evaluator {payload.get('name')!r}")
        return self._call("POST", f"{UNSTABLE}/evaluators", json=payload)

    def delete_evaluator(self, name: str) -> None:
        self._require_project_write(f"deletion of evaluator {name!r}")
        self._call("DELETE", f"{UNSTABLE}/evaluators/{name}")

    # ── LLM connections (hub only) ────────────────────────────────────────

    def list_llm_connections(self) -> list[dict]:
        return self._paged(f"{PUBLIC}/llm-connections")

    def upsert_llm_connection(self, payload: dict) -> dict:
        """PUT is an upsert on the connection's provider name at the pin."""
        self._require_project_write("the judge LLM connection")
        return self._call("PUT", f"{PUBLIC}/llm-connections", json=payload)

    # ── evaluation rules (every role, own cluster only) ───────────────────

    def list_evaluation_rules(self) -> list[dict]:
        return self._paged(f"{UNSTABLE}/evaluation-rules")

    def create_evaluation_rule(self, payload: dict) -> dict:
        return self._call("POST", f"{UNSTABLE}/evaluation-rules", json=payload)

    def update_evaluation_rule(self, rule_id: str, payload: dict) -> dict:
        return self._call(
            "PATCH", f"{UNSTABLE}/evaluation-rules/{rule_id}", json=payload
        )

    def delete_evaluation_rule(self, rule_id: str) -> None:
        self._call("DELETE", f"{UNSTABLE}/evaluation-rules/{rule_id}")

    # ── metrics (every role, own cluster's agents only) ───────────────────

    def query_metrics(self, query: dict) -> list[dict]:
        """Run one query against the metrics endpoint.

        The v1 endpoint refuses requests when Langfuse runs in events_only mode;
        a v4 upgrade (Q2) moves this to /api/public/v2/metrics.
        """
        import json as _json

        body = self._call(
            "GET", f"{PUBLIC}/metrics", params={"query": _json.dumps(query)}
        )
        return (body or {}).get("data") or []
