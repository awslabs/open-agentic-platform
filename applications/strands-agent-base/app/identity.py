"""Caller credential propagation.

    caller --[Authorization: Bearer <jwt>]--> agent --[same or own token]--> MCP

Rule, and the whole contract to reimplement in other runtimes: forward the
caller's bearer token if one arrived, otherwise use the agent's own projected
ServiceAccount token.

Identity-provider agnostic on purpose. Nothing here inspects claims or knows
about OAuth token exchange. If a gateway policy exchanges the caller's token
before it reaches us, we propagate the result unchanged.
"""

import hashlib
import logging
import os
import uuid
from contextvars import ContextVar
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Inbound Authorization header for the request in flight, scoped to the request
# by the middleware in main.py.
inbound_auth: ContextVar[Optional[str]] = ContextVar("inbound_auth", default=None)

WORKLOAD_KEY = "workload"

HeadersProvider = Callable[[], dict]


def workload_headers() -> dict:
    """Authorization from the projected ServiceAccount token.

    Read at call time, not cached, because the kubelet rewrites this file before
    the token expires (the gateway-identity trait projects it with
    expirationSeconds: 3600). Callers invoke this only when opening or recycling a
    connection, so it is not on the per-request path.
    """
    path = os.getenv("WORKLOAD_TOKEN_PATH")
    if not path:
        return {}
    try:
        with open(path) as handle:
            token = handle.read().strip()
    except OSError:
        logger.warning("WORKLOAD_TOKEN_PATH set but token unreadable at %s", path)
        return {}
    return {"Authorization": f"Bearer {token}"} if token else {}


def outbound(propagate: bool = True) -> Tuple[HeadersProvider, str]:
    """Return (headers provider, pool key) for outbound MCP calls.

    The provider is invoked at connect time rather than returning headers now, so
    the ServiceAccount token is read once per connection instead of once per
    request, and a recycled connection picks up the rotated value.

    The pool key exists because an MCP connection binds its credential when it
    opens, so connections must never be shared between callers. Hashing the
    credential gives that isolation without parsing it.

    Non-Bearer schemes are not forwarded: passing a caller's Basic credentials to
    a tool backend would widen their exposure for nothing.
    """
    header = inbound_auth.get() if propagate else None
    if header and header[:7].lower() == "bearer " and header[7:].strip():
        headers = {"Authorization": header}
        return (lambda: headers), hashlib.sha256(header.encode()).hexdigest()[:16]
    return workload_headers, WORKLOAD_KEY


async def capture_caller_auth(request, call_next):
    """Middleware recording the caller's Authorization header for this request.

    Register on every route, including framework-owned endpoints such as the
    Strands A2A JSON-RPC handler, so agent construction can forward the caller's
    credential rather than the agent's own ServiceAccount token.

    Also records the caller identity header (see caller_subject) and a per-request id.
    """
    inbound_auth.set(request.headers.get("authorization"))
    inbound_actor.set(request.headers.get(CALLER_IDENTITY_HEADER))
    request_id.set(uuid.uuid4().hex)
    return await call_next(request)


# ── caller identity ──────────────────────────────────────────────────────
#
# Platform contract: the gateway validates the caller's JWT and sets this header to
# its `sub` on every request it forwards, overwriting any value the client sent
# (gitops/addons/charts/agent-gateway/templates/caller-identity-policy.yaml).
#
#   Keycloak user        -> the user's subject id
#   agent via gateway    -> system:serviceaccount:<namespace>:<name>
#
# The header is trusted as-is and no token is parsed: pods are reachable only
# through the gateway. The name is fixed, not configurable, so the gateway and the
# image cannot drift apart. An image that needs no per-caller key can ignore it.
#
# Without the header (a request that bypassed the gateway) there is no caller to
# key on, so this module fails closed: nothing is shared between such requests, and
# the agent attaches no memory (agent._build_session_manager).

CALLER_IDENTITY_HEADER = "x-forwarded-user"

inbound_actor: ContextVar[Optional[str]] = ContextVar("inbound_actor", default=None)

# Unique per request, set by the middleware. Keys a request that carries no identity
# at all, so it is consistent within the request and never matches another one.
request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def caller_subject() -> Optional[str]:
    """The gateway-validated caller (`sub`) for this request, or None."""
    return (inbound_actor.get() or "").strip() or None


def _token_hash(header: Optional[str]) -> Optional[str]:
    if header and header[:7].lower() == "bearer " and header[7:].strip():
        return hashlib.sha256(header.encode()).hexdigest()[:16]
    return None


def _no_identity_key() -> str:
    return f"none:{request_id.get() or uuid.uuid4().hex}"


def caller_key(propagate: bool) -> str:
    """Key for caches that hold per-caller agents (/chat and A2A contexts).

    The gateway subject when present, so a token refresh keeps the same agent. When
    the agent forwards the caller's token (*propagate*), the token hash is added,
    because the cached agent's MCP connections are bound to that token. Without a
    subject: the token hash, and with no credential at all a per-request key, so a
    client-supplied context id can never select another caller's agent.
    """
    pool = outbound(propagate)[1]
    sub = caller_subject()
    if sub:
        return f"sub:{sub}" if pool == WORKLOAD_KEY else f"sub:{sub}|{pool}"
    tok = _token_hash(inbound_auth.get())
    return f"tok:{tok}" if tok else _no_identity_key()


def task_owner(context=None) -> str:
    """Owner key for A2A tasks: the gateway subject, stable across token refresh.

    Resolved from the request's a2a ServerCallContext when one is passed: a2a-sdk
    builds it per request with the request headers (DefaultCallContextBuilder,
    state["headers"]), and hands it to every task_store call, including saves made
    after the HTTP response for non-blocking and streamed tasks. Falls back to this
    request's context variables. Without a subject: the token hash, else a key no
    later request can match.
    """
    headers = (getattr(context, "state", None) or {}).get("headers") if context is not None else None
    if headers is not None:
        sub = (headers.get(CALLER_IDENTITY_HEADER) or "").strip()
        tok = _token_hash(headers.get("authorization"))
    else:
        sub = caller_subject() or ""
        tok = _token_hash(inbound_auth.get())
    if sub:
        return f"sub:{sub}"
    return f"tok:{tok}" if tok else _no_identity_key()
