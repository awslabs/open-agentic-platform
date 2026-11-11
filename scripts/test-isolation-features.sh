#!/usr/bin/env bash
# Sandbox / isolation: FEATURE assertions. PASS/FAIL per row; exit code = number
# of failed rows.
#
# HOW THIS DIFFERS FROM test-isolation-matrix.sh, which is the other half of the
# same job. The matrix answers "does it render, does it compose, and does every
# forbidden combination still fail". All four of its sections are about
# ACCEPT/REJECT. None of them look at WHAT WAS EMITTED.
#
# That gap is not academic: a guard can fire correctly while the feature it
# guards emits the wrong field name, the wrong value, or nothing at all, and the
# matrix reports a clean run. This file asserts the CONTENT -- the env var a
# session-isolated agent needs in order to claim a sandbox, the RBAC that lets it,
# the runtime class per trust model, the annotations the Nitro runtime reads, and
# the card an operator reads back.
#
# Every assertion is on a field a consumer actually depends on. Where a value is
# only meaningful to something outside this repo (the sandbox agent reading an
# annotation, an agent image reading an env var), the name is spelled out in full
# rather than globbed, because a rename is exactly the regression worth catching.
#
# CONTACTS NO CLUSTER and no AWS, but needs a READABLE KUBECONFIG: vela builds a
# client before it honours --offline. Nothing in it is contacted.
#
# usage: scripts/test-isolation-features.sh [-v]
# Requires: helm, vela (>=1.10), python3 + PyYAML.
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
    printf 'PASS  %-5s %-62s %s\n' "$1" "$2" "$3"
  else
    printf 'FAIL  %-5s %-62s expected=%s actual=%s\n' "$1" "$2" "$3" "$4"
    FAILS=$((FAILS + 1))
  fi
}

# ---------------------------------------------------------------- definitions
defs_for() { # defs_for <tag> [--set k=v ...] -> prints the definitions dir
  local tag="$1"; shift
  local dir="$WORK/defs-$tag"
  [ -d "$dir" ] && { echo "$dir"; return; }
  local chart="$WORK/chart-$tag"
  mkdir -p "$dir" "$chart"
  cp -R "$OAP/gitops/addons/charts/oam-agent-components/." "$chart/"
  rm -f "$chart/templates/"*.yaml
  for src in components traits; do
    vela def render "$OAP/platform/oam/definitions/$src" -o "$chart/templates" \
      --message "# gen" > "$chart/vela-$src.log" 2>&1 || {
      echo "vela def render failed for $src ($tag):" >&2
      cat "$chart/vela-$src.log" >&2; exit 1; }
  done
  helm template oap-features "$chart" "$@" > "$chart/rendered.yaml" 2>"$chart/helm.err" || {
    echo "helm template failed for $tag:" >&2; cat "$chart/helm.err" >&2; exit 1; }
  python3 - "$chart/rendered.yaml" "$dir" <<'PY'
import sys, yaml, os
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get('kind') in ('ComponentDefinition', 'TraitDefinition'):
        yaml.safe_dump(d, open(os.path.join(sys.argv[2], '%s-%s.yaml' % (d['kind'], d['metadata']['name'])), 'w'),
                       default_flow_style=False, width=100000)
PY
  local n
  n="$(find "$dir" -name '*.yaml' | wc -l | tr -d ' ')"
  [ "$n" -ge 2 ] || { echo "definition set '$tag' has $n definitions; refusing" >&2; exit 1; }
  echo "$dir"
}

die_defs() { echo "cannot build definition set '$1' -- see the error above." >&2; exit 1; }

# render <app.yaml> <defs> <out.yaml> -> rc; stdout stays clean for $() callers
render() {
  if vela dry-run --offline -d "$2" -f "$1" > "$3" 2> "$3.err"; then
    return 0
  fi
  [ "$VERBOSE" = 1 ] && sed 's/^/        /' "$3.err" >&2
  return 1
}

app() { local f="$WORK/app-$1.yaml"; cat > "$f"; echo "$f"; }

# q <rendered.yaml> <python-expr-over-`objs`> -> the value, or the literal
# string ABSENT. One python invocation per assertion is slower than batching and
# very much clearer at the call site, which matters more here: a reader has to be
# able to see exactly which field each row is about.
q() {
  local f="$1"; shift
  python3 - "$f" "$*" <<'PY' 2>/dev/null || echo ABSENT
import sys, yaml
objs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]


def obj(kind, name):
    for d in objs:
        if d.get('kind') == kind and (d.get('metadata') or {}).get('name') == name:
            return d
    raise KeyError('%s/%s' % (kind, name))


def podspec(kind, name):
    d = obj(kind, name)
    for path in (('spec', 'template', 'spec'), ('spec', 'podTemplate', 'spec')):
        cur = d
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur:
            return cur
    raise KeyError('no pod spec in %s/%s' % (kind, name))


def podmeta(kind, name):
    d = obj(kind, name)
    for path in (('spec', 'template', 'metadata'), ('spec', 'podTemplate', 'metadata')):
        cur = d
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur:
            return cur
    return {}


def env(kind, name, var):
    for c in podspec(kind, name).get('containers', []):
        for e in c.get('env', []):
            if e['name'] == var:
                return e.get('value')
    raise KeyError(var)


def ann(kind, name, key):
    return (podmeta(kind, name).get('annotations') or {})[key]


def kinds():
    return sorted('%s/%s' % (d['kind'], (d.get('metadata') or {}).get('name')) for d in objs)


v = eval(sys.argv[2])
print('ABSENT' if v is None else v)
PY
}

AGENT_MIN='        description: "m"
        systemMessage: "m"'

echo "== definitions =="
D_NITRO="$(defs_for nitro)" || die_defs nitro
D_SESSION="$(defs_for session --set global.sessionIsolationEnabled=true)" || die_defs session
D_KATA="$(defs_for kata --set global.sandboxRuntimeClass=kata-clh --set global.confidentialSandboxRuntimeClass= --set global.nitroSandboxRuntimeClasses=)" || die_defs kata
printf 'rendered 3 definition sets (nitro default, session-enabled, kata-only)\n\n'

# A correctly shaped stub policy: the components shape-check cedarPolicy.
POLICY='cGVybWl0KHByaW5jaXBhbCwgYWN0aW9uLCByZXNvdXJjZSk7IC8vIGZlYXR1cmUtdGVzdCBzdHViLCBsb25nIGVub3VnaCBmb3IgdGhlIHNoYXBlIGNoZWNr'

# =============================================================== F. agent, sandbox: true
echo "== F. type: agent -- sandbox: true, per trust model =="

f_iso="$(app f1 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: f1, namespace: default}
spec:
  components:
    - name: f1-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        sandboxConntrackLimit: 4096
        sandboxDnsQueriesPerSecond: 64
YAML
)"
render "$f_iso" "$D_NITRO" "$WORK/o-f1.yaml" || { echo "FAIL F: isolated agent did not render"; FAILS=$((FAILS+1)); }
row F1 "isolated agent selects the isolated class" \
  "isolated-sandbox" "$(q "$WORK/o-f1.yaml" "podspec('Rollout','f1-agent')['runtimeClassName']")"
row F2 "no policy annotation without cedarPolicy" \
  "ABSENT" "$(q "$WORK/o-f1.yaml" "ann('Rollout','f1-agent','sandbox.amazonaws.com/policy')")"
row F3 "conntrack limit annotation, as a string" \
  "4096" "$(q "$WORK/o-f1.yaml" "ann('Rollout','f1-agent','sandbox.amazonaws.com/conntrack-limit')")"
row F4 "dns rate annotation uses the runtime's own key name" \
  "64" "$(q "$WORK/o-f1.yaml" "ann('Rollout','f1-agent','sandbox.amazonaws.com/dns-packets-per-second')")"
row F5 "isolated mode does NOT set enableServiceLinks" \
  "ABSENT" "$(q "$WORK/o-f1.yaml" "podspec('Rollout','f1-agent').get('enableServiceLinks')")"

f_conf="$(app f6 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: f6, namespace: default}
spec:
  components:
    - name: f6-agent
      type: agent
      properties:
$AGENT_MIN
        sandbox: true
        sandboxMode: confidential
        cedarPolicy: "$POLICY"
YAML
)"
render "$f_conf" "$D_NITRO" "$WORK/o-f6.yaml" || { echo "FAIL F: confidential agent did not render"; FAILS=$((FAILS+1)); }
row F6 "confidential agent selects the confidential class" \
  "confidential-sandbox" "$(q "$WORK/o-f6.yaml" "podspec('Rollout','f6-agent')['runtimeClassName']")"
row F7 "cedarPolicy lands on the POD TEMPLATE annotation" \
  "$POLICY" "$(q "$WORK/o-f6.yaml" "ann('Rollout','f6-agent','sandbox.amazonaws.com/policy')")"
# Required by eks-nitro-sandbox-genpolicy --profile confidential, not hardening.
row F8 "confidential sets enableServiceLinks: false" \
  "False" "$(q "$WORK/o-f6.yaml" "podspec('Rollout','f6-agent')['enableServiceLinks']")"

f_plain="$(app f9 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: f9, namespace: default}
spec:
  components:
    - name: f9-agent
      type: agent
      properties:
$AGENT_MIN
YAML
)"
render "$f_plain" "$D_NITRO" "$WORK/o-f9.yaml" || { echo "FAIL F: plain agent did not render"; FAILS=$((FAILS+1)); }
row F9 "an agent WITHOUT sandbox gets no runtime class at all" \
  "ABSENT" "$(q "$WORK/o-f9.yaml" "podspec('Rollout','f9-agent').get('runtimeClassName')")"

# The Kata path is the one a live cluster regressed on, so it is asserted, not assumed.
render "$f_iso" "$D_KATA" "$WORK/o-f10.yaml" || { echo "FAIL F: kata agent did not render"; FAILS=$((FAILS+1)); }
row F10 "the SAME Application on a Kata cluster selects kata-clh" \
  "kata-clh" "$(q "$WORK/o-f10.yaml" "podspec('Rollout','f1-agent')['runtimeClassName']")"

# =============================================================== G. isolation: session
echo
echo "== G. type: agent -- isolation: session (the per-session front door) =="

f_sess="$(app g <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: g, namespace: default}
spec:
  components:
    - name: analyst
      type: agent
      properties:
$AGENT_MIN
        isolation: session
        sandboxTemplate: analyst-pool
        sessionKey: subject
    - name: analyst-pool
      type: sandbox
      properties:
        description: "analyst tools"
        image: public.ecr.aws/docker/library/python:3.12-slim
        warmPool: 3
YAML
)"
render "$f_sess" "$D_SESSION" "$WORK/o-g.yaml" || { echo "FAIL G: session app did not render"; FAILS=$((FAILS+1)); }

# These four env vars ARE the contract with the agent image: it reads them to
# claim a sandbox per session. A rename here is silent -- the agent falls back to
# running tools in-process, which is the shared behaviour under an isolated name.
row G1 "AGENT_ISOLATION tells the image which mode it is in" \
  "session" "$(q "$WORK/o-g.yaml" "env('Rollout','analyst','AGENT_ISOLATION')")"
row G2 "SANDBOX_WARM_POOL names the pool to claim from" \
  "analyst-pool" "$(q "$WORK/o-g.yaml" "env('Rollout','analyst','SANDBOX_WARM_POOL')")"
row G3 "SANDBOX_NAMESPACE is the Application's namespace" \
  "default" "$(q "$WORK/o-g.yaml" "env('Rollout','analyst','SANDBOX_NAMESPACE')")"
row G4 "AGENT_SESSION_KEY carries the chosen key strategy" \
  "subject" "$(q "$WORK/o-g.yaml" "env('Rollout','analyst','AGENT_SESSION_KEY')")"

# Without this RBAC the env vars are decoration: the claim is a 403 at runtime.
row G5 "claimer Role can create sandboxclaims" \
  "True" "$(q "$WORK/o-g.yaml" "any(r.get('resources')==['sandboxclaims'] and 'create' in r.get('verbs',[]) for r in obj('Role','analyst-sandbox-claimer')['rules'])")"
row G6 "claimer Role can read sandboxes but NOT create them" \
  "True" "$(q "$WORK/o-g.yaml" "any(r.get('resources')==['sandboxes'] and 'create' not in r.get('verbs',[]) for r in obj('Role','analyst-sandbox-claimer')['rules'])")"
row G7 "RoleBinding targets the agent's own ServiceAccount" \
  "analyst" "$(q "$WORK/o-g.yaml" "obj('RoleBinding','analyst-sandbox-claimer')['subjects'][0]['name']")"
row G8 "the agent front door is still a Rollout (blue-green kept)" \
  "True" "$(q "$WORK/o-g.yaml" "'Rollout/analyst' in kinds()")"

# =============================================================== H. type: sandbox
echo
echo "== H. type: sandbox -- the pool itself =="
row H1 "a warm pool is emitted" \
  "True" "$(q "$WORK/o-g.yaml" "'SandboxWarmPool/analyst-pool' in kinds()")"
row H2 "warmPool: 3 reaches the pool's replicas" \
  "3" "$(q "$WORK/o-g.yaml" "obj('SandboxWarmPool','analyst-pool')['spec']['replicas']")"
row H3 "the pool references the template by name" \
  "analyst-pool-template" "$(q "$WORK/o-g.yaml" "obj('SandboxWarmPool','analyst-pool')['spec']['sandboxTemplateRef']['name']")"
row H4 "the template's pods are isolated too" \
  "isolated-sandbox" "$(q "$WORK/o-g.yaml" "podspec('SandboxTemplate','analyst-pool-template')['runtimeClassName']")"
row H5 "sandbox pods do not mount a SA token by default" \
  "False" "$(q "$WORK/o-g.yaml" "podspec('SandboxTemplate','analyst-pool-template')['automountServiceAccountToken']")"
# The card is how an operator reads back what they actually got.
row H6 "the sandbox card reports the runtime class in force" \
  "isolated-sandbox" "$(q "$WORK/o-g.yaml" "obj('ConfigMap','analyst-pool-sandbox')['data']['runtimeClass']")"
row H7 "the card reports the trust model" \
  "isolated" "$(q "$WORK/o-g.yaml" "obj('ConfigMap','analyst-pool-sandbox')['data']['mode']")"
row H8 "the card reports warm replicas, matching the pool" \
  "3" "$(q "$WORK/o-g.yaml" "obj('ConfigMap','analyst-pool-sandbox')['data']['warmReplicas']")"

f_netiso="$(app h9 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: h9, namespace: default}
spec:
  components:
    - name: h9-pool
      type: sandbox
      properties:
        description: "untrusted code"
        image: public.ecr.aws/docker/library/python:3.12-slim
        service: false
        networkIsolated: true
        conntrackLimit: 1024
        dnsQueriesPerSecond: 8
YAML
)"
render "$f_netiso" "$D_NITRO" "$WORK/o-h9.yaml" || { echo "FAIL H: netiso pool did not render"; FAILS=$((FAILS+1)); }
row H9 "networkIsolated emits the annotation the runtime reads" \
  "true" "$(q "$WORK/o-h9.yaml" "ann('SandboxTemplate','h9-pool-template','sandbox.amazonaws.com/network-isolated')")"
row H10 "and the card says so, so it is auditable" \
  "true" "$(q "$WORK/o-h9.yaml" "obj('ConfigMap','h9-pool-sandbox')['data']['networkIsolated']")"
row H11 "service: false emits no Service for the pool" \
  "True" "$(q "$WORK/o-h9.yaml" "not any(k.startswith('Service/') for k in kinds())")"
row H12 "pool-level conntrack tuning reaches the template" \
  "1024" "$(q "$WORK/o-h9.yaml" "ann('SandboxTemplate','h9-pool-template','sandbox.amazonaws.com/conntrack-limit')")"

f_sconf="$(app h13 <<YAML
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: h13, namespace: default}
spec:
  components:
    - name: h13-pool
      type: sandbox
      properties:
        description: "regulated"
        image: public.ecr.aws/docker/library/python:3.12-slim
        service: false
        mode: confidential
        cedarPolicy: "$POLICY"
YAML
)"
render "$f_sconf" "$D_NITRO" "$WORK/o-h13.yaml" || { echo "FAIL H: confidential pool did not render"; FAILS=$((FAILS+1)); }
row H13 "a confidential pool selects the confidential class" \
  "confidential-sandbox" "$(q "$WORK/o-h13.yaml" "podspec('SandboxTemplate','h13-pool-template')['runtimeClassName']")"
row H14 "and sets enableServiceLinks: false for the generator" \
  "False" "$(q "$WORK/o-h13.yaml" "podspec('SandboxTemplate','h13-pool-template')['enableServiceLinks']")"
row H15 "the pool's policy lands on the template's pod annotations" \
  "$POLICY" "$(q "$WORK/o-h13.yaml" "ann('SandboxTemplate','h13-pool-template','sandbox.amazonaws.com/policy')")"

echo
if [ "$FAILS" -eq 0 ]; then
  echo "all feature rows passed"
else
  echo "failed rows: $FAILS"
fi
exit "$FAILS"
