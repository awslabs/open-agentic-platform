"""Built-in tools the agent can carry next to its MCP tools.

Off by default. A deployment turns one on by naming it in the BUILTIN_TOOLS env var
(comma-separated). Today there is one:

  argocd_applications  ->  list_argocd_applications

Why list_argocd_applications exists: a fleet question such as "is any Argo CD
Application unhealthy?" needs the sync and health status of EVERY Application on the
hub. A generic Kubernetes list tool returns Application metadata only, so the model
had to read each Application in full (10-40 KB each) to see its status. On a hub with
180 Applications that is ~45 tool calls; the agent stopped after a dozen, and missed
the one app that was not healthy. This tool lists all Applications server-side
(paginated with the Kubernetes limit/continue protocol) and returns one compact row
per Application, plus the full list of the ones that are not Synced+Healthy, in a
single tool call.

Access: the tool calls the cluster's Kubernetes API with an EKS token derived from the
pod's AWS credentials (Pod Identity), exactly like `aws eks get-token`. Reads are
therefore gated by the cluster's EKS access entry and RBAC for that IAM role: the role
needs get/list on applications.argoproj.io and eks:DescribeCluster. The tool never
writes.
"""

from __future__ import annotations

import base64
import logging
import os
import ssl
from collections.abc import Callable
from typing import Any

import httpx
from strands import tool

logger = logging.getLogger(__name__)

# Kubernetes list page size. 100 Application objects is a few MB at most.
_PAGE_SIZE = 100
# Upper bound on pages per call (5000 Applications), so a misbehaving API server
# returning an endless `continue` token cannot loop forever.
_MAX_PAGES = 50
# Rows returned per call. Each row is ~60 bytes, so 500 rows stays well under the
# size of ONE full Application read.
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000
_MESSAGE_CAP = 200
_HTTP_TIMEOUT = 30.0

_APPLICATIONS_PATH = "/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications"


def _region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"


def _eks_token(cluster_name: str, region: str, session=None) -> str:
    """Bearer token for an EKS cluster, same algorithm as `aws eks get-token`."""
    import boto3
    from botocore.signers import RequestSigner

    session = session or boto3.session.Session()
    sts = session.client("sts", region_name=region)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        region_name=region,
        expires_in=60,
        operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _cluster_connection(
    cluster_name: str, region: str, session=None
) -> tuple[str, str]:
    """(API endpoint, CA bundle PEM) of an EKS cluster."""
    import boto3

    session = session or boto3.session.Session()
    cluster = session.client("eks", region_name=region).describe_cluster(
        name=cluster_name
    )["cluster"]
    ca_pem = base64.b64decode(cluster["certificateAuthority"]["data"]).decode()
    return cluster["endpoint"], ca_pem


def _http_client(endpoint: str, token: str, ca_pem: str) -> httpx.Client:
    return httpx.Client(
        base_url=endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        verify=ssl.create_default_context(cadata=ca_pem),
        timeout=_HTTP_TIMEOUT,
    )


def _summarize(app: dict) -> dict:
    """The few fields that answer "is this Application OK?"."""
    status = app.get("status") or {}
    row = {
        "name": (app.get("metadata") or {}).get("name", ""),
        "sync": (status.get("sync") or {}).get("status") or "Unknown",
        "health": (status.get("health") or {}).get("status") or "Unknown",
    }
    conditions = status.get("conditions") or []
    if conditions:
        first = conditions[0]
        row["condition"] = (
            f"{first.get('type', '')}: {str(first.get('message', ''))[:_MESSAGE_CAP]}"
        )
    op = (status.get("operationState") or {}).get("phase")
    if op and op != "Succeeded":
        row["operation"] = op
    return row


def _is_problem(row: dict) -> bool:
    return not (row["sync"] == "Synced" and row["health"] == "Healthy")


def list_applications(
    client: httpx.Client,
    namespace: str,
    page_size: int = _PAGE_SIZE,
    max_pages: int = _MAX_PAGES,
) -> tuple[list[dict], bool]:
    """Every Application in *namespace*, summarized. Returns (rows, truncated)."""
    rows: list[dict] = []
    token: str | None = None
    path = _APPLICATIONS_PATH.format(namespace=namespace)
    for _ in range(max_pages):
        params = {"limit": str(page_size)}
        if token:
            params["continue"] = token
        resp = client.get(path, params=params)
        resp.raise_for_status()
        body = resp.json()
        rows.extend(_summarize(item) for item in body.get("items") or [])
        token = (body.get("metadata") or {}).get("continue") or None
        if not token:
            return rows, False
    return rows, True


def build_result(
    rows: list[dict],
    cluster_name: str,
    namespace: str,
    only_problems: bool,
    offset: int,
    limit: int,
    truncated: bool,
) -> dict:
    rows = sorted(rows, key=lambda r: r["name"])
    problems = [r for r in rows if _is_problem(r)]
    limit = max(1, min(int(limit), _MAX_LIMIT))
    offset = max(0, int(offset))
    listed = problems if only_problems else rows
    page = listed[offset : offset + limit]
    result: dict[str, Any] = {
        "cluster": cluster_name,
        "namespace": namespace,
        "total": len(rows),
        "synced_and_healthy": len(rows) - len(problems),
        "not_synced_or_not_healthy": len(problems),
        # Always complete: these are the answer to a fleet-health question.
        "problems": problems,
        # One "name sync health" row per Application, paged by offset/limit.
        "rows": [f"{r['name']} {r['sync']} {r['health']}" for r in page],
        "offset": offset,
        "returned": len(page),
    }
    if offset + len(page) < len(listed):
        result["next_offset"] = offset + len(page)
    if truncated:
        result["warning"] = (
            f"stopped after {_MAX_PAGES * _PAGE_SIZE} Applications; counts cover those only"
        )
    return result


@tool(name="list_argocd_applications")
def list_argocd_applications(
    cluster_name: str = "",
    namespace: str = "argocd",
    only_problems: bool = False,
    offset: int = 0,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    """List EVERY Argo CD Application on a cluster with its sync and health status, in ONE call.

    Call this FIRST for any Argo CD fleet-health question ("is anything unhealthy?",
    "which apps are OutOfSync?"), instead of reading Applications one by one. It reads
    all Applications server-side and returns:
      - total, synced_and_healthy, not_synced_or_not_healthy counts;
      - problems: every Application that is NOT both Synced and Healthy, with its
        first status condition (e.g. ComparisonError) and a non-Succeeded operation
        phase. This list is always complete, so it is safe to say "none" when it is
        empty and `warning` is absent;
      - rows: "name sync health" for each Application, paged with offset/limit
        (follow next_offset when present).
    Read a single Application in full only to investigate one that is listed in problems.

    Args:
        cluster_name: EKS cluster that runs Argo CD (the hub). Defaults to the
            ARGOCD_CLUSTER_NAME env var when empty.
        namespace: Namespace holding the Application objects. Default "argocd".
        only_problems: When true, rows lists only the problem Applications.
        offset: First row to return (for paging rows).
        limit: Max rows to return (1-2000, default 500).
    """
    cluster_name = cluster_name or os.getenv("ARGOCD_CLUSTER_NAME", "")
    if not cluster_name:
        return {
            "error": "cluster_name is required (no ARGOCD_CLUSTER_NAME default is set)"
        }
    region = _region()
    try:
        endpoint, ca_pem = _cluster_connection(cluster_name, region)
        token = _eks_token(cluster_name, region)
        with _http_client(endpoint, token, ca_pem) as client:
            rows, truncated = list_applications(client, namespace)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = (
            " (the agent's IAM role needs an EKS access entry and RBAC get/list on applications.argoproj.io)"
            if code in (401, 403)
            else ""
        )
        return {
            "error": f"Kubernetes API returned HTTP {code} listing Applications in {namespace}{hint}"
        }
    except Exception as exc:  # noqa: BLE001 - surface any failure to the model, never crash the turn
        logger.warning("list_argocd_applications failed for %s: %s", cluster_name, exc)
        return {"error": f"{type(exc).__name__}: {str(exc)[:_MESSAGE_CAP]}"}
    return build_result(
        rows, cluster_name, namespace, only_problems, offset, limit, truncated
    )


# name in BUILTIN_TOOLS -> (tool, system-prompt hint)
_REGISTRY: dict[str, tuple[Callable, str]] = {
    "argocd_applications": (
        list_argocd_applications,
        (
            "To assess Argo CD Applications, call `list_argocd_applications` FIRST: one call "
            "returns the sync and health status of every Application and the complete list of "
            "those that are not Synced+Healthy. Do not read Applications one by one to collect "
            "their status; read one in full only to investigate an Application that tool "
            "reports as a problem."
        ),
    ),
}


def enabled_names(raw: str | None) -> list[str]:
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        logger.warning(
            "BUILTIN_TOOLS: ignoring unknown tool(s) %s (known: %s)",
            unknown,
            sorted(_REGISTRY),
        )
    return [n for n in names if n in _REGISTRY]


def builtin_tools(raw: str | None) -> list:
    return [_REGISTRY[n][0] for n in enabled_names(raw)]


def system_prompt_hint(raw: str | None) -> str:
    hints = [_REGISTRY[n][1] for n in enabled_names(raw)]
    return ("\n\n" + "\n".join(hints)) if hints else ""
