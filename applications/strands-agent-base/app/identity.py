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
    """
    inbound_auth.set(request.headers.get("authorization"))
    return await call_next(request)
