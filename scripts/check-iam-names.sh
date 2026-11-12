#!/usr/bin/env bash
# Cross-repo check of the install-scoped AWS names (issue #92).
#
# The naming rule lives in several places that must agree byte for byte:
#   - this repo: agentcore-memory, agentcore-browser, agentcore-code-interpreter and
#     eks-read-access (platform/oam/definitions/components/), which emit IAM Policy
#     objects and the AgentCore memory name;
#   - appmod-blueprints: the aws-service-identity trait (role name, and the Policy
#     object names its accessFor attachments reference), the component-iam-policy
#     trait (platform/oam/definitions/traits/), and the aiops-agent chart's
#     hand-written names (workshop/aiops-agent/chart/templates/).
#
# For a matrix of installs (cluster, region) and Applications (namespace, appName),
# this renders the real definitions as the platform does (helm template with
# global.clusterName / global.awsRegion, then vela dry-run --offline) and checks:
#   N1  every RolePolicyAttachment's policyArnRef names a Policy object of the render
#   N2  each Policy object name equals its crossplane.io/external-name, equals the rule
#       below (independent Python implementation), <= 128 chars, IAM charset, and is a
#       valid Kubernetes object name
#   N3  the PodIdentity roleName follows the role rule, <= 64 chars
#   N4  the AgentCore memory name follows the memory rule and AgentCore's pattern
#   N5  every label value is <= 63 chars and a valid label value
#   N6  no policy or role name is shared between two installs; memory names only
#       between two regions of the same cluster (a memory is regional)
#   N7  the aiops-agent chart's eks-read-mcp role and eks-read-access policy names follow the rules
#   N8  with global.clusterName empty, each of the four components fails the render
# --self-test breaks the reference implementation (hash length) and passes only if every
# row that compares a name with the reference then fails (negative control).
#
# usage: scripts/check-iam-names.sh <appmod-blueprints checkout> [--self-test]
# Requires helm, vela (1.11+, used offline: no cluster, no AWS calls), python3 + PyYAML.
# Exit code: number of failed rows (capped at 255); with --self-test, 0 when the control holds.
set -euo pipefail
APPMOD="${1:?usage: $0 <appmod-blueprints checkout> [--self-test]}"
SELFTEST="${2:-}"
OAP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for t in helm vela python3; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done
python3 -c 'import yaml' 2>/dev/null || { echo "missing: python3 PyYAML" >&2; exit 1; }
[ -d "$APPMOD/gitops/addons/charts/kubevela/templates/traits" ] || { echo "not an appmod-blueprints checkout: $APPMOD" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# vela dry-run --offline still builds a client from a kubeconfig; point it at nothing.
cat > "$WORK/kubeconfig" <<'EOF'
apiVersion: v1
kind: Config
clusters: [{name: none, cluster: {server: "https://127.0.0.1:1"}}]
users: [{name: none, user: {token: none}}]
contexts: [{name: none, context: {cluster: none, user: none}}]
current-context: none
EOF
# appmod's kubevela chart depends on vela-core; render only its traits, with its globals.
mkdir -p "$WORK/kv/templates"
printf 'apiVersion: v2\nname: kubevela-traits\nversion: 0.0.0\n' > "$WORK/kv/Chart.yaml"
printf 'global:\n  awsRegion: ""\n  clusterName: ""\n' > "$WORK/kv/values.yaml"
cp "$APPMOD"/gitops/addons/charts/kubevela/templates/traits/{aws-service-identity,component-policy}.yaml "$WORK/kv/templates/"
# A plain Deployment workload to carry the traits (the agent component needs a gateway).
cat > "$WORK/workload.yaml" <<'EOF'
apiVersion: core.oam.dev/v1beta1
kind: ComponentDefinition
metadata: {name: check-workload, namespace: vela-system}
spec:
  workload: {definition: {apiVersion: apps/v1, kind: Deployment}}
  schematic:
    cue:
      template: |
        output: {
          apiVersion: "apps/v1"
          kind:       "Deployment"
          metadata: name: context.name
          spec: {
            selector: matchLabels: app: context.name
            template: {
              metadata: labels: app: context.name
              spec: {serviceAccountName: context.name, containers: [{name: context.name, image: "busybox"}]}
            }
          }
        }
        parameter: {}
EOF

# defs <cluster> <region> <dir>: the platform's definitions for one install.
defs() {
  mkdir -p "$3"; cp "$WORK/workload.yaml" "$3/"
  { helm template oap "$OAP/gitops/addons/charts/oam-agent-components" \
      --set global.clusterName="$1" --set global.awsRegion="$2" --set global.awsAccountId=111122223333
    echo "---"
    helm template kv "$WORK/kv" --set global.clusterName="$1" --set global.awsRegion="$2"
  } | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get("kind") in ("ComponentDefinition", "TraitDefinition"):
        yaml.safe_dump(d, open(sys.argv[1] + "/" + d["kind"] + "-" + d["metadata"]["name"] + ".yaml", "w"), sort_keys=False)
' "$3"
}
dryrun() { KUBECONFIG="$WORK/kubeconfig" vela dry-run --offline -d "$1" -f "$2" 2>"$3"; }

# Matrix. Cluster names cover lower case, a "-"/"_" mix with upper case (EKS allows
# both), a name starting with a digit, and EKS's 100-character maximum.
LONG="c$(printf 'x%.0s' $(seq 1 98))z"
INSTALLS="cluster-a:us-west-2 cluster-b:us-west-2 cluster-a:eu-west-1 peeks-hub:us-west-2 Agent_Platform:us-west-2 9-numeric:us-west-2 $LONG:us-west-2"
# The long appName stays within the 63 characters KubeVela puts in the app.oam.dev/name label.
APPS="agents:my-app team-a:an-application-name-for-truncation-check"
for inst in $INSTALLS; do
  c="${inst%%:*}"; r="${inst##*:}"; d="$WORK/defs-$c-$r"; defs "$c" "$r" "$d"
  for a in $APPS; do
    ns="${a%%:*}"; app="${a##*:}"; f="$WORK/app-$ns-$app.yaml"
    cat > "$f" <<EOF
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: $app, namespace: $ns}
spec:
  components:
    - {name: agent-memory, type: agentcore-memory}
    - {name: web-browser, type: agentcore-browser, properties: {browserName: web_browser}}
    - {name: code-interp, type: agentcore-code-interpreter, properties: {interpreterName: code_interp}}
    - {name: eks-read, type: eks-read-access}
    - name: data-svc
      type: check-workload
      traits:
        - {type: component-iam-policy, properties: {service: s3}}
    - name: my-agent
      type: check-workload
      traits:
        - type: aws-service-identity
          properties: {accessFor: [agent-memory, web-browser, code-interp, eks-read, data-svc]}
EOF
    out="$WORK/out-$c-$r-$ns.yaml"
    if ! dryrun "$d" "$f" "$WORK/err" > "$out"; then
      echo "render failed: cluster=$c region=$r namespace=$ns" >&2; grep -v 'connection refused\|external packages' "$WORK/err" >&2 || true; exit 1
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$c" "$r" "$ns" "$app" "$out" >> "$WORK/renders.tsv"
  done
done

# aiops-agent chart: the agent runs on <clusterPrefix>-hub, namespace aiops-agent.
for inst in peeks:us-west-2 Peeks_X:eu-west-1; do
  p="${inst%%:*}"; r="${inst##*:}"; out="$WORK/aiops-$p-$r.yaml"
  helm template aiops "$APPMOD/workshop/aiops-agent/chart" --set clusterPrefix="$p" --set region="$r" \
    --set accountId=111122223333 --set cloudfrontDomain=example.invalid --set gitlabDomain=gitlab.example.invalid > "$out"
  printf '%s\t%s\t%s\n' "$p" "$r" "$out" >> "$WORK/aiops.tsv"
done

# N8: the four components refuse an empty cluster name.
defs "" us-west-2 "$WORK/defs-empty"
for spec in "agentcore-memory|{}" "agentcore-browser|{browserName: web_browser}" "agentcore-code-interpreter|{interpreterName: code_interp}" "eks-read-access|{}"; do
  t="${spec%%|*}"; p="${spec#*|}"
  printf 'apiVersion: core.oam.dev/v1beta1\nkind: Application\nmetadata: {name: my-app, namespace: agents}\nspec:\n  components:\n    - {name: c, type: %s, properties: %s}\n' "$t" "$p" > "$WORK/empty.yaml"
  if dryrun "$WORK/defs-empty" "$WORK/empty.yaml" "$WORK/err" >/dev/null; then echo "$t accepted"; 
  elif grep -q 'global.clusterName is empty' "$WORK/err"; then echo "$t refused"; else echo "$t other-error"; fi
done > "$WORK/empty.txt"

python3 - "$WORK" "$SELFTEST" <<'PY'
import hashlib, re, sys, yaml
work, selftest = sys.argv[1], sys.argv[2] == "--self-test"
H = 7 if selftest else 8  # --self-test: a wrong hash length must make every name row fail
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def policy(cluster, region, ns, app, comp):
    base = (cluster.replace("_", "-").lower() + "-" + app + "-" + comp)[:100]
    return f"{base}-{sha(f'{region}/{cluster}/{ns}/{app}/{comp}')[:H]}-iam-policy"
def role(cluster, region, ns, sa):
    return f"{(cluster + '-' + sa)[:50]}-{sha(f'{region}/{cluster}/{ns}/{sa}')[:H]}-role"
def memory(cluster, ns, comp):
    raw = (cluster.replace("-", "_") + "_" + (ns + "_" + comp).replace("-", "_"))[:30]
    return "M" + raw + "_" + sha(f"{cluster}/{ns}/{comp}")[:2 * H]
DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
LABEL = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
fails, rows, named = 0, 0, 0
def row(ok, what, name_row=False):
    # name_row: the row compares a rendered name with the reference rule
    global fails, rows, named
    rows += 1; named += name_row
    if not ok:
        fails += 1
        if not selftest: print("FAIL ", what)
seen = {"policy": {}, "role": {}, "memory": {}}
for line in open(f"{work}/renders.tsv"):
    c, r, ns, app, path = line.rstrip("\n").split("\t")
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    pols = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Policy"}
    tag = f"[{c[:20]} {r} {ns}]"
    for d in docs:
        for k, v in (d["metadata"].get("labels") or {}).items():
            row(len(v) <= 63 and LABEL.match(v), f"N5 {tag} label {k}={v} ({len(v)} chars) on {d['kind']} {d['metadata']['name']}")
        if d["kind"] == "RolePolicyAttachment":
            ref = d["spec"]["forProvider"]["policyArnRef"]["name"]
            row(ref in pols, f"N1 {tag} attachment {d['metadata']['name']} -> {ref}: no such Policy object")
    for comp in ("agent-memory", "web-browser", "code-interp", "eks-read", "data-svc"):
        want = policy(c, r, ns, app, comp)
        ok = want in pols and pols[want]["metadata"].get("annotations", {}).get("crossplane.io/external-name") == want
        row(ok and len(want) <= 128 and re.match(r"^[\w+=,.@-]+$", want) and len(want) <= 253 and DNS.match(want),
            f"N2 {tag} {comp}: expected Policy {want}, render has {sorted(pols)}", True)
        seen["policy"].setdefault(want, set()).add((c, r))
    pi = [d for d in docs if d["kind"] == "PodIdentity"][0]
    want = role(c, r, ns, "my-agent")
    row(pi["spec"]["roleName"] == want and len(want) <= 64, f"N3 {tag} role {pi['spec']['roleName']} != {want}", True)
    seen["role"].setdefault(want, set()).add((c, r))
    mem = [d for d in docs if d["kind"] == "Memory"][0]["spec"]["forProvider"]["name"]
    want = memory(c, ns, "agent-memory")
    row(mem == want and re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$", mem), f"N4 {tag} memory {mem} != {want}", True)
    seen["memory"].setdefault(want, set()).add(c)
for kind in ("policy", "role"):
    for n, owners in seen[kind].items():
        row(len(owners) == 1, f"N6 {kind} {n} shared by installs {sorted(owners)}")
for n, owners in seen["memory"].items():
    row(len(owners) == 1, f"N6 memory {n} shared by clusters {sorted(owners)}")
for line in open(f"{work}/aiops.tsv"):
    p, r, path = line.rstrip("\n").split("\t")
    text = open(path).read(); hub = f"{p}-hub"
    # eks-read-mcp's role is the principal of the chart's EKS AccessEntries (hub and spokes).
    for sa in ("eks-read-mcp",):
        want = role(hub, r, "aiops-agent", sa)
        row(want in text, f"N7 aiops-agent prefix={p} {r}: role {want} not in the chart render", True)
    want = policy(hub, r, "aiops-agent", "aiops-agent-hub", "eks-read-access")
    row(want in text, f"N7 aiops-agent prefix={p} {r}: eks-read-access policy {want} not in the chart render", True)
for line in open(f"{work}/empty.txt"):
    t, res = line.split()
    row(res == "refused", f"N8 {t} with global.clusterName empty: {res}, expected refused")
if selftest:
    ok = fails == named
    print(f"self-test: {fails} of {rows} rows failed with a broken reference; {named} name rows " + ("all failed: OK" if ok else "should have failed: the check is not comparing names"))
    sys.exit(0 if ok else 1)
print(f"{rows - fails}/{rows} rows passed")
sys.exit(min(fails, 255))
PY
