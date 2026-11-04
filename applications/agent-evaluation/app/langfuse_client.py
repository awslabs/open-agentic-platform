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

# Langfuse v4. The evaluator and evaluation-rule surfaces are STABLE here; on v3
# they lived under /api/public/unstable/ and could not be used at all, because
# their preflight could not resolve a provider config (see the design doc, Q2).
V2 = "/api/public/v2"
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
        """Collect every page of a page-numbered list endpoint (`page`/`meta`).

        Used by the endpoints that still paginate that way, such as
        /api/public/llm-connections.
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

    def _cursor_paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Collect every page of a CURSOR-paginated v2 list endpoint.

        The v2 evaluator and evaluation-rule endpoints page by an opaque cursor
        in `meta.cursor`, which is omitted on the last page — unlike the
        page-numbered v1 endpoints. Getting this wrong is quiet and expensive:
        the hub would stop seeing evaluators past the first 100, decide they do
        not exist, and publish a duplicate of each on every pass (v4 happily
        creates same-named evaluators, since names are not identifiers).

        `limit` is capped at 100 by the API.
        """
        items: list[dict] = []
        cursor: str | None = None
        # Bounded rather than `while True`: a server that kept returning the
        # same cursor would otherwise spin forever holding the reconcile loop.
        for _ in range(1000):
            query = {**(params or {}), "limit": 100}
            if cursor:
                query["cursor"] = cursor
            body = self._call("GET", path, params=query)
            items.extend((body or {}).get("data") or [])
            cursor = ((body or {}).get("meta") or {}).get("cursor")
            if not cursor:
                return items
        raise LangfuseError(
            f"GET {path} did not stop paginating after 1000 pages; refusing to "
            "loop further"
        )

    def _require_project_write(self, what: str) -> None:
        if not self.allow_project_writes:
            raise ProjectWriteForbidden(
                f"refusing to write {what}: this instance runs with role=spoke, and "
                "the hub owns the project-wide Langfuse objects (D4). A spoke that "
                "needs a new evaluator waits for the hub to publish it."
            )

    # ── evaluators (hub writes, every role reads) ─────────────────────────

    def list_evaluators(self) -> list[dict]:
        """Every evaluator, each with its latest definition flattened in."""
        return self._cursor_paged(f"{V2}/evaluators")

    def create_evaluator(self, payload: dict) -> dict:
        """Create a NEW evaluator at version 1.

        In v4 a name is not an identifier and does not have to be unique, so
        this always creates another object — it is not an upsert. Call it only
        when no evaluator of ours carries that name yet, or the project
        accumulates duplicates that all match the same traces.
        """
        self._require_project_write(f"evaluator {payload.get('name')!r}")
        return self._call("POST", f"{V2}/evaluators", json=payload)

    def update_evaluator(self, evaluator_id: str, payload: dict) -> dict:
        """Replace an evaluator's definition, which publishes a new version.

        PATCH, not `POST /evaluators/{id}/versions`: that path exists but is
        read-only (listing version history) and answers 405 to a POST. A
        definition change through PATCH is what mints the next version — the
        contract notes that metadata-only changes do not.

        Every rule assigned to the evaluator automatically uses its latest
        version, so this is what makes a hub prompt change reach every cluster
        with no action on the spokes. It also clears a pause caused by an
        invalid or missing model config, keeping the id and rule assignments.

        The whole definition must be sent: definition fields are replaced as a
        unit rather than merged.
        """
        self._require_project_write(f"a new version of evaluator {evaluator_id}")
        return self._call("PATCH", f"{V2}/evaluators/{evaluator_id}", json=payload)

    def list_evaluator_versions(self, evaluator_id: str) -> list[dict]:
        """Version history, newest first. Diagnostics only."""
        return self._cursor_paged(f"{V2}/evaluators/{evaluator_id}/versions")

    def delete_evaluator(self, evaluator_id: str) -> None:
        self._require_project_write(f"deletion of evaluator {evaluator_id}")
        self._call("DELETE", f"{V2}/evaluators/{evaluator_id}")

    # ── LLM connections (hub only) ────────────────────────────────────────

    def list_llm_connections(self) -> list[dict]:
        return self._paged(f"{PUBLIC}/llm-connections")

    def upsert_llm_connection(self, payload: dict) -> dict:
        """PUT is an upsert on the connection's provider name at the pin."""
        self._require_project_write("the judge LLM connection")
        return self._call("PUT", f"{PUBLIC}/llm-connections", json=payload)

    # ── evaluation rules (every role, own cluster only) ───────────────────

    def list_evaluation_rules(self) -> list[dict]:
        return self._cursor_paged(f"{V2}/evaluation-rules")

    def create_evaluation_rule(self, payload: dict) -> dict:
        return self._call("POST", f"{V2}/evaluation-rules", json=payload)

    def update_evaluation_rule(self, rule_id: str, payload: dict) -> dict:
        return self._call("PATCH", f"{V2}/evaluation-rules/{rule_id}", json=payload)

    def delete_evaluation_rule(self, rule_id: str) -> None:
        self._call("DELETE", f"{V2}/evaluation-rules/{rule_id}")

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
