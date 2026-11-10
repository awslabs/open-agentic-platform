#!/usr/bin/env bash
# Sandbox / isolation: RENDER-TIME capability matrix. PASS/FAIL per row; exit
# code = number of failed rows.
#
# Needs NO cluster and NO AWS. It renders the real definitions the way the
# platform does -- helm template (which substitutes the globals INTO the CUE)
# then `vela dry-run --offline` -- and asks three questions a single-example
# check cannot:
#
#   A. Does every shipped example still render?
#   B. Does each capability still work ALONGSIDE the others in one Application?
#      Isolation touches the pod template, which is also where observability,
#      memory, MCP wiring and the identity traits land. "renders on its own" is
#      not the same as "composes".
#   C. Does every combination that must FAIL still fail? A guard that stops
#      firing is worse than one that never existed, because the thing it was
#      protecting looks fine.
#   D. Do two capabilities, or two Applications, ever emit the SAME Kubernetes
#      object? This is the class of bug that renders perfectly and breaks on
#      apply: KubeVela applies both, the second wins, and whichever Application
#      syncs last owns a resource the other still believes it manages. Nothing
#      in a per-Application render can see it, so it is computed here by
#      intersecting emitted (kind, namespace, name) tuples.
#
# usage: scripts/test-isolation-matrix.sh [-v]
# Requires: helm, vela (>=1.10, used offline), python3 + PyYAML.
set -uo pipefail

VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

for t in helm vela python3; do
  command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }
done
python3 -c 'import yaml' 2>/dev/null || { echo "missing: python3 PyYAML" >&2; exit 1; }

OAP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILS=0

row() { # row <id> <description> <expected> <actual>
  if [ "$3" = "$4" ]; then
    printf 'PASS  %-5s %-64s %s\n' "$1" "$2" "$3"
  else
    printf 'FAIL  %-5s %-64s expected=%s actual=%s\n' "$1" "$2" "$3" "$4"
    FAILS=$((FAILS + 1))
  fi
}

# ---------------------------------------------------------------- definitions
# One render per distinct set of Helm globals, cached, because `helm template`
# plus two `vela def render` passes is the slow part and most rows share a
# configuration.
defs_for() { # defs_for <tag> [--set k=v ...] -> prints the definitions dir
  local tag="$1"; shift
  local dir="$WORK/defs-$tag"
  [ -d "$dir" ] && { echo "$dir"; return; }
  local chart="$WORK/chart-$tag"
  mkdir -p "$dir" "$chart"
  cp -R "$OAP/gitops/addons/charts/oam-agent-components/." "$chart/"
  rm -f "$chart/templates/"*.yaml
  vela def render "$OAP/platform/oam/definitions/components" -o "$chart/templates" --message "# gen" >/dev/null 2>&1
  vela def render "$OAP/platform/oam/definitions/traits" -o "$chart/templates" --message "# gen" >/dev/null 2>&1
  helm template oap-matrix "$chart" "$@" > "$chart/rendered.yaml" 2>"$chart/helm.err" || {
    echo "helm template failed for $tag:" >&2; cat "$chart/helm.err" >&2; exit 1; }
  python3 - "$chart/rendered.yaml" "$dir" <<'PY'
import sys, yaml, os
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get('kind') in ('ComponentDefinition', 'TraitDefinition'):
        yaml.safe_dump(d, open(os.path.join(sys.argv[2], '%s-%s.yaml' % (d['kind'], d['metadata']['name'])), 'w'),
                       default_flow_style=False, width=100000)
PY
  echo "$dir"
}

# render <app.yaml> <defs-dir> <out.yaml> -> prints ok | reject
render() {
  if vela dry-run --offline -d "$2" -f "$1" > "$3" 2> "$3.err"; then
    echo ok
  else
    [ "$VERBOSE" = 1 ] && sed 's/^/        /' "$3.err"
    echo reject
  fi
}

app() { # app <name> <<'YAML' ... YAML   -> prints the path
  local f="$WORK/app-$1.yaml"
  cat > "$f"
  echo "$f"
}

# Shorthand for an agent component's required fields.
AGENT_MIN='        description: "m"
        systemMessage: "m"'

echo "== definitions =="
# nitro: the shipped defaults. session: per-session isolation switched on.
D_NITRO="$(defs_for nitro)"
D_SESSION="$(defs_for session --set global.sessionIsolationEnabled=true)"
D_KATA="$(defs_for kata --set global.sandboxRuntimeClass=kata-clh --set global.confidentialSandboxRuntimeClass=)"
D_NORUNTIME="$(defs_for noruntime --set global.sandboxRuntimeClass= --set global.confidentialSandboxRuntimeClass=)"
printf 'rendered 4 definition sets (nitro default, session-enabled, kata-only, no-runtime)\n\n'

# ---------------------------------------------------------------- A. examples
echo "== A. every shipped example renders =="
for f in "$OAP"/platform/oam/examples/example-*.yaml; do
  b="$(basename "$f" .yaml)"
  # Applications are the only thing dry-run takes; skip raw manifests.
  kind="$(python3 -c 'import sys,yaml;d=next(yaml.safe_load_all(open(sys.argv[1])));print((d or {}).get("kind",""))' "$f")"
  [ "$kind" = "Application" ] || continue
  # The per-session tiers need the platform switch; everything else uses defaults.
  d="$D_NITRO"
  grep -qE '^\s+isolation:\s+(session|tools)\s*$' "$f" && d="$D_SESSION"
  # The confidential examples ship placeholder policies on purpose; they still
  # have to RENDER, which is what this checks.
  rc="$(render "$f" "$d" "$WORK/out-$b.yaml")"
  # aws-service-identity and service-rollout are defined in appmod-blueprints,
  # not here (steering §2 names them as the exception), so an example using one
  # cannot be rendered from this repo alone. Report it as SKIP rather than FAIL:
  # a failure row for a missing upstream definition trains people to ignore the
  # output. scripts/check-iam-names.sh covers these by taking an
  # appmod-blueprints checkout.
  if [ "$rc" = reject ] && grep -q "trait definition of .* not found" "$WORK/out-$b.yaml.err" 2>/dev/null; then
    printf 'SKIP  %-5s %-64s %s\n' "A:$b" "needs an appmod-blueprints trait" \
      "$(sed -n 's/.*parse trait(\([^)]*\)).*/\1/p' "$WORK/out-$b.yaml.err" | head -1)"
    continue
  fi
  row "A:$b" "renders" ok "$rc"
done

# ---------------------------------------------------- B. capabilities compose
echo
echo "== B. capabilities compose in one Application (expect ok) =="

b_sandbox_memory="$(app b1 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b1, namespace: default}
spec:
  components:
    - name: b1-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        memory: {provider: agentcore, config: {memoryId: "m-1", region: us-west-2}}
YAML
)"
row B1 "sandbox + agentcore memory" ok "$(render "$b_sandbox_memory" "$D_NITRO" "$WORK/o-b1.yaml")"

b_sandbox_eval="$(app b2 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b2, namespace: default}
spec:
  components:
    - name: b2-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        evaluation: {enabled: true, evaluators: ["goal-completion"]}
YAML
)"
row B2 "sandbox + online evaluation" ok "$(render "$b_sandbox_eval" "$D_NITRO" "$WORK/o-b2.yaml")"

b_sandbox_mcp="$(app b3 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b3, namespace: default}
spec:
  components:
    - name: b3-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        mcpServers: [{name: mcp-time}, {name: code-interpreter-mcp}]
        sandboxConntrackLimit: 8192
        sandboxDnsQueriesPerSecond: 300
      traits:
        - type: gateway-identity
YAML
)"
row B3 "sandbox + MCP servers + throttles + gateway-identity" ok "$(render "$b_sandbox_mcp" "$D_NITRO" "$WORK/o-b3.yaml")"

b_sandbox_decentral="$(app b4 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b4, namespace: default}
spec:
  components:
    - name: b4-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        observability: {mode: decentralized}
        replicas: 3
        autoPromotionEnabled: false
        scaleDownDelaySeconds: 120
YAML
)"
row B4 "sandbox + decentralized observability + replicas + blue-green" ok "$(render "$b_sandbox_decentral" "$D_NITRO" "$WORK/o-b4.yaml")"

b_conf_all="$(app b5 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b5, namespace: default}
spec:
  components:
    - name: b5-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        sandboxMode: confidential
        cedarPolicy: "cGVybWl0KHByaW5jaXBhbCwgYWN0aW9uLCByZXNvdXJjZSk7"
        memory: {provider: agentcore, config: {memoryId: "m-1", region: us-west-2}}
        evaluation: {enabled: true, evaluators: ["helpfulness"]}
        mcpServers: [{name: mcp-time}]
      traits:
        - type: gateway-identity
YAML
)"
row B5 "confidential + memory + evaluation + MCP + identity" ok "$(render "$b_conf_all" "$D_NITRO" "$WORK/o-b5.yaml")"

b_session_full="$(app b6 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b6, namespace: default}
spec:
  components:
    - name: b6-agent
      type: agent
      properties:
$AGENT_MIN
        isolation: session
        sandboxTemplate: b6-pool
        sessionKey: subject
        memory: {provider: agentcore, config: {memoryId: "m-1", region: us-west-2}}
        evaluation: {enabled: true, evaluators: ["goal-completion"]}
        mcpServers: [{name: mcp-time}]
        replicas: 2
      traits:
        - type: gateway-identity
    - name: b6-pool
      type: sandbox
      properties:
        description: "pool"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        warmPool: 2
YAML
)"
row B6 "isolation:session + memory + evaluation + MCP + identity" ok "$(render "$b_session_full" "$D_SESSION" "$WORK/o-b6.yaml")"

b_session_sandbox="$(app b7 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b7, namespace: default}
spec:
  components:
    - name: b7-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        sandboxMode: confidential
        cedarPolicy: "cGVybWl0KHByaW5jaXBhbCwgYWN0aW9uLCByZXNvdXJjZSk7"
        isolation: session
        sandboxTemplate: b7-pool
    - name: b7-pool
      type: sandbox
      properties:
        description: "pool"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        mode: confidential
        cedarPolicy: "cGVybWl0KHByaW5jaXBhbCwgYWN0aW9uLCByZXNvdXJjZSk7"
        warmPool: 1
YAML
)"
row B7 "confidential front door + confidential per-session pool" ok "$(render "$b_session_sandbox" "$D_SESSION" "$WORK/o-b7.yaml")"

b_sandbox_netiso="$(app b8 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b8, namespace: default}
spec:
  components:
    - name: b8-pool
      type: sandbox
      properties:
        description: "no network"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        networkIsolated: true
        service: false
        conntrackLimit: 1024
        dnsQueriesPerSecond: 50
        warmPool: 0
        readOnlyRootFilesystem: false
        automountServiceAccountToken: true
        envVarsInjection: Overrides
YAML
)"
row B8 "sandbox: networkIsolated + service:false + every other knob" ok "$(render "$b_sandbox_netiso" "$D_NITRO" "$WORK/o-b8.yaml")"

b_kata="$(app b9 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b9, namespace: default}
spec:
  components:
    - name: b9-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        memory: {provider: milvus, config: {url: "http://milvus:19530", collectionName: c}}
        mcpServers: [{name: mcp-time}]
      traits:
        - type: gateway-identity
YAML
)"
row B9 "Kata cluster: sandbox + memory + MCP + identity" ok "$(render "$b_kata" "$D_KATA" "$WORK/o-b9.yaml")"

b_two_agents="$(app b10 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: b10, namespace: default}
spec:
  components:
    - name: b10-a
      type: agent
      properties:
$AGENT_MIN
        isolation: session
        sandboxTemplate: b10-pool
    - name: b10-b
      type: agent
      properties:
$AGENT_MIN
        isolation: session
        sandboxTemplate: b10-pool
    - name: b10-pool
      type: sandbox
      properties:
        description: "shared pool"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        warmPool: 3
YAML
)"
row B10 "two agents sharing ONE sandbox pool" ok "$(render "$b_two_agents" "$D_SESSION" "$WORK/o-b10.yaml")"

# ---------------------------------------------------- C. guards still fire
echo
echo "== C. every guard still fires (expect reject) =="

c_conf_nopolicy="$(app c1 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c1, namespace: default}
spec:
  components:
    - name: c1-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        sandboxMode: confidential
YAML
)"
row C1 "confidential without cedarPolicy" reject "$(render "$c_conf_nopolicy" "$D_NITRO" "$WORK/o-c1.yaml")"
row C2 "confidential on a Kata-only cluster" reject "$(render "$c_conf_nopolicy" "$D_KATA" "$WORK/o-c2.yaml")"

c_sandbox_noclass="$(app c3 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c3, namespace: default}
spec:
  components:
    - name: c3-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
YAML
)"
row C3 "sandbox:true with no runtime class configured" reject "$(render "$c_sandbox_noclass" "$D_NORUNTIME" "$WORK/o-c3.yaml")"

c_session_gate="$(app c4 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c4, namespace: default}
spec:
  components:
    - name: c4-agent
      type: agent
      properties:
$AGENT_MIN
        isolation: session
        sandboxTemplate: c4-pool
YAML
)"
row C4 "isolation:session without sessionIsolationEnabled" reject "$(render "$c_session_gate" "$D_NITRO" "$WORK/o-c4.yaml")"

c_session_notpl="$(app c5 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c5, namespace: default}
spec:
  components:
    - name: c5-agent
      type: agent
      properties:
$AGENT_MIN
        isolation: session
YAML
)"
row C5 "isolation:session without sandboxTemplate" reject "$(render "$c_session_notpl" "$D_SESSION" "$WORK/o-c5.yaml")"

c_tools="$(app c6 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c6, namespace: default}
spec:
  components:
    - name: c6-agent
      type: agent
      properties:
$AGENT_MIN
        isolation: tools
        sandboxTemplate: c6-pool
YAML
)"
row C6 "isolation:tools (refused by design)" reject "$(render "$c_tools" "$D_SESSION" "$WORK/o-c6.yaml")"

c_netiso_svc="$(app c7 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c7, namespace: default}
spec:
  components:
    - name: c7-pool
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        networkIsolated: true
YAML
)"
row C7 "sandbox: networkIsolated with default service:true" reject "$(render "$c_netiso_svc" "$D_NITRO" "$WORK/o-c7.yaml")"

c_conf_netiso="$(app c8 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c8, namespace: default}
spec:
  components:
    - name: c8-pool
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        mode: confidential
        cedarPolicy: "cGVybWl0"
        networkIsolated: true
        service: false
YAML
)"
row C8 "sandbox: confidential + networkIsolated" reject "$(render "$c_conf_netiso" "$D_NITRO" "$WORK/o-c8.yaml")"

c_sbx_conf_nopolicy="$(app c9 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c9, namespace: default}
spec:
  components:
    - name: c9-pool
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
        mode: confidential
YAML
)"
row C9 "sandbox: confidential without cedarPolicy" reject "$(render "$c_sbx_conf_nopolicy" "$D_NITRO" "$WORK/o-c9.yaml")"

c_sbx_noruntime="$(app c10 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c10, namespace: default}
spec:
  components:
    - name: c10-pool
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
YAML
)"
row C10 "sandbox component with no isolation runtime configured" reject "$(render "$c_sbx_noruntime" "$D_NORUNTIME" "$WORK/o-c10.yaml")"

# The pre-existing guard, which this branch must not have broken: evaluation
# only works on the centralized observability path.
c_eval_decentral="$(app c11 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: c11, namespace: default}
spec:
  components:
    - name: c11-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        observability: {mode: decentralized}
        evaluation: {enabled: true}
YAML
)"
row C11 "PRE-EXISTING: evaluation + decentralized observability" reject "$(render "$c_eval_decentral" "$D_NITRO" "$WORK/o-c11.yaml")"

# ------------------------------------------------- D. object-name collisions
echo
echo "== D. no two components or Applications emit the same object =="

# An agent and a sandbox sharing one component name BOTH own ServiceAccount
# <name>. Rendering cannot see it -- each component renders fine -- so it is
# computed from the emitted objects.
d_same_name="$(app d1 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: d1, namespace: default}
spec:
  components:
    - name: collide
      type: agent
      properties:
$AGENT_MIN
    - name: collide
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
YAML
)"
d1_rc="$(render "$d_same_name" "$D_NITRO" "$WORK/o-d1.yaml")"

# A sandbox named X emits X-template. A sandbox named X-template emits a WARM
# POOL named X-template. Two sandboxes named X and X-template therefore fight
# over one name -- a trap a developer can walk into by naming pools after each
# other.
d_tpl_name="$(app d2 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: d2, namespace: default}
spec:
  components:
    - name: pool
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
    - name: pool-template
      type: sandbox
      properties:
        description: "p"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
YAML
)"
d2_rc="$(render "$d_tpl_name" "$D_NITRO" "$WORK/o-d2.yaml")"

# Two separate Applications in ONE namespace, each declaring a pool of the same
# name. Neither render can see the other.
d_app_a="$(app d3a <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: d3a, namespace: default}
spec:
  components:
    - name: shared-pool
      type: sandbox
      properties:
        description: "team A"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
YAML
)"
d_app_b="$(app d3b <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: d3b, namespace: default}
spec:
  components:
    - name: shared-pool
      type: sandbox
      properties:
        description: "team B"
        image: public.ecr.aws/amazonlinux/amazonlinux:2023
YAML
)"
render "$d_app_a" "$D_NITRO" "$WORK/o-d3a.yaml" >/dev/null
render "$d_app_b" "$D_NITRO" "$WORK/o-d3b.yaml" >/dev/null

# A realistic healthy pairing, to prove the detector is not just saying "yes".
render "$b_session_full" "$D_SESSION" "$WORK/o-b6.yaml" >/dev/null

dupes() { # dupes <rendered.yaml>... -> prints colliding "kind/ns/name", or nothing
  python3 - "$@" <<'PY'
import sys, yaml, collections
seen = collections.defaultdict(set)
for path in sys.argv[1:]:
    try:
        docs = [d for d in yaml.safe_load_all(open(path)) if d]
    except Exception:
        continue
    for d in docs:
        k, m = d.get('kind'), d.get('metadata') or {}
        if not k or not m.get('name'):
            continue
        # The Application-level owner, so the same object emitted by two
        # Applications is a collision while one object listed twice is not.
        owner = (m.get('labels') or {}).get('app.oam.dev/name', path)
        seen['%s/%s/%s' % (k, m.get('namespace', 'default'), m['name'])].add(owner)
hits = sorted(key for key, owners in seen.items() if len(owners) > 1)
print(' '.join(hits))
PY
}

# Within one Application: COUNT occurrences of (kind, name), rather than
# distinct component labels.
#
# The label version of this was wrong and the matrix caught it: when two
# components share a NAME they also share app.oam.dev/component, so a set of
# labels has size one and the collision looks clean. Two documents with the same
# kind and name are a collision whatever the labels say.
dupes_within() { # dupes_within <rendered.yaml>
  python3 - "$1" <<'PY'
import sys, yaml, collections
c = collections.Counter()
for d in (x for x in yaml.safe_load_all(open(sys.argv[1])) if x):
    k, m = d.get('kind'), d.get('metadata') or {}
    if k and m.get('name'):
        c['%s/%s' % (k, m['name'])] += 1
print(' '.join(sorted(key for key, n in c.items() if n > 1)))
PY
}

# Which workload kinds an Application actually emitted. A same-name collision
# does not merely duplicate an object -- it can REPLACE one component's workload
# with the other's, which this is how you see.
workloads() { # workloads <rendered.yaml>
  python3 - "$1" <<'PY'
import sys, yaml
kinds = sorted({d['kind'] for d in yaml.safe_load_all(open(sys.argv[1]))
                if d and d.get('kind') in ('Rollout', 'SandboxWarmPool')})
print(','.join(kinds) or 'none')
PY
}

d1_dupes="$(dupes_within "$WORK/o-d1.yaml" 2>/dev/null)"
d2_dupes="$(dupes_within "$WORK/o-d2.yaml" 2>/dev/null)"
d3_dupes="$(dupes "$WORK/o-d3a.yaml" "$WORK/o-d3b.yaml" 2>/dev/null)"
b6_dupes="$(dupes_within "$WORK/o-b6.yaml" 2>/dev/null)"

# An agent and a sandbox on one name is a REAL collision, and KubeVela does not
# reject it: it resolves a component's type by NAME, so the second definition
# wins for both and the agent's Rollout is silently REPLACED by the sandbox's
# warm pool -- while the agent's Services, Agent Card and HTTPRoute still point
# at a workload that no longer exists. Nothing errors.
#
# This is generic KubeVela behaviour rather than anything specific to these
# components, but pairing an agent with its own pool in one Application makes it
# easy to walk into, so it is asserted here and documented in sandbox.cue, the
# session example, and NITRO-DESIGN.md §6.
if [ "$d1_rc" = reject ] || [ -n "$d1_dupes" ]; then d1_state=detected; else d1_state=undetected; fi
row D1 "agent + sandbox on the SAME component name collides" detected "$d1_state"
[ -n "$d1_dupes" ] && printf '      duplicated: %s\n' "$d1_dupes"
row D1b "...and the agent's Rollout is lost, not just duplicated" SandboxWarmPool "$(workloads "$WORK/o-d1.yaml")"

# NOT a collision, and asserted as clean so a future rename does not quietly
# make it one: sandbox "X" emits SandboxTemplate X-template, while sandbox
# "X-template" emits SandboxWarmPool X-template. Same name, DIFFERENT kinds,
# which Kubernetes allows. Only kind+name together have to be unique.
row D2 "sandbox 'X' + sandbox 'X-template' (same name, different kinds)" clean \
  "$([ -z "$d2_dupes" ] && [ "$d2_rc" = ok ] && echo clean || echo "collides:${d2_dupes:-render-rejected}")"

row D3 "two Applications, one namespace, same pool name collide" detected \
  "$([ -n "$d3_dupes" ] && echo detected || echo undetected)"
[ -n "$d3_dupes" ] && printf '      objects: %s\n' "$d3_dupes"

# The control: the shipped pairing of an agent and its pool must be clean.
row D4 "agent + its own pool emit no shared object (control)" clean \
  "$([ -z "$b6_dupes" ] && echo clean || echo "collides:$b6_dupes")"

echo
echo "failed rows: $FAILS"
exit $((FAILS > 255 ? 255 : FAILS))
