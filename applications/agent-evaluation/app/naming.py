"""Names for the Langfuse objects this platform owns, and the ownership test.

Every object in the shared Langfuse project has exactly one writer (D4). Which
writer is readable off the object's name, so the reconciler can delete the
objects it owns without ever touching another cluster's.

WHY A CLUSTER HASH RATHER THAN THE CLUSTER NAME. The design writes rules as
`oap-<cluster>-<evaluator>-<hash>` and scopes deletion to names starting
`oap-<this cluster>-`. That prefix is ambiguous, because EKS cluster names may
contain hyphens: the prefix for cluster `dev` (`oap-dev-`) is also a prefix of
every rule belonging to a cluster named `dev-2` (`oap-dev-2-safety-abc123`). The
`dev` reconciler would then see those rules as its own, find no ConfigMap for
them, and delete another cluster's rules — the exact cross-cluster deletion the
per-cluster ownership rule exists to prevent.

The fix is an owner token that cannot be a prefix of another owner's token: the
first 8 hex characters of SHA-256 over the cluster name. Fixed length and a
fixed alphabet mean no cluster's token can extend another's, so a prefix test on
`oap-<token>-` is exact. A separator EKS names cannot contain (`.`) would also
work, but only if Langfuse accepts it in an object name, which is unverified at
the pin; hex is accepted anywhere.

The cost is that the cluster is no longer legible in the name. The reconciler
therefore logs `cluster=<name> owner_token=<token>` on every startup and
publishes the same pair as labels on `oap_agent_eval_cluster_info`, so an
operator reading the Langfuse UI can map a token back to a cluster.
"""

from __future__ import annotations

import hashlib
import re

PREFIX = "oap"

# The Langfuse LLM-connection provider name the platform's judge is registered
# under. It doubles as the ownership marker on evaluator objects: v4 evaluator
# names are not identifiers, so "is this evaluator ours?" is answered by its
# modelConfig.provider rather than by its name. Shared by the project loop
# (which writes it) and the per-cluster loop (which filters on it).
JUDGE_PROVIDER = "oap-bifrost-judge"

# Separates an evaluator from the judge alias it is bound to, in the evaluator
# object name (D4, "The judge is a property of the evaluator"). Catalog names and
# judge aliases must not contain it, or the name would be ambiguous.
JUDGE_SEPARATOR = "--"

_TOKEN_LEN = 8
_TOKEN_RE = re.compile(r"^[0-9a-f]{%d}$" % _TOKEN_LEN)


def owner_token(cluster: str) -> str:
    """Return the fixed-length, fixed-alphabet token identifying one cluster."""
    if not cluster:
        raise ValueError("cluster name is required to derive an owner token")
    digest = hashlib.sha256(cluster.encode("utf-8")).hexdigest()
    return digest[:_TOKEN_LEN]


def config_hash(judge: str, sampling: float) -> str:
    """Hash the part of a rule group that is not in the name already.

    Rules are grouped by (evaluator, judge, sampling); the evaluator is spelled
    out in the name, so only the judge and the sampling rate need hashing. The
    sampling rate is formatted to a fixed precision first, so 0.1 and 0.100 do
    not produce two rules for one group.
    """
    payload = f"{judge}\x00{sampling:.6f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_TOKEN_LEN]


def rule_name(cluster: str, evaluator: str, judge: str, sampling: float) -> str:
    """Name of the evaluation rule for one (evaluator, judge, sampling) group."""
    return "-".join(
        [PREFIX, owner_token(cluster), evaluator, config_hash(judge, sampling)]
    )


def evaluator_name(evaluator: str, judge: str, default_judge: str) -> str:
    """Name of the evaluator object that carries a given judge model.

    The judge model is the evaluator's `modelConfig` at the pin, and an
    evaluation rule has no model field, so one evaluator object exists per
    catalog entry per judge alias. The default judge takes the bare name, which
    keeps existing names stable when a second alias is added later.
    """
    if judge == default_judge:
        return evaluator
    return f"{evaluator}{JUDGE_SEPARATOR}{judge}"


def owns(name: str, cluster: str) -> bool:
    """True when `name` is a rule this cluster's reconciler created.

    Exact on the owner token, which is what makes this safe for cluster names
    that are prefixes of each other (see the module docstring).
    """
    parts = name.split("-")
    if len(parts) < 4 or parts[0] != PREFIX:
        return False
    if not _TOKEN_RE.match(parts[1]):
        return False
    return parts[1] == owner_token(cluster)


def is_platform_rule(name: str) -> bool:
    """True when `name` looks like a rule any OAP cluster owns.

    Used only for reporting: a rule matching this but not `owns()` belongs to
    another cluster and must never be modified or deleted.
    """
    parts = name.split("-")
    return len(parts) >= 4 and parts[0] == PREFIX and bool(_TOKEN_RE.match(parts[1]))
