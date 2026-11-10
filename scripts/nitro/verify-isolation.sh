#!/usr/bin/env bash
# Prove that a pod is REALLY running inside a hardware-isolated microVM, rather
# than in an ordinary container that merely asked to be.
#
# Why this needs proving. A RuntimeClass existing proves nothing; a pod carrying
# runtimeClassName proves nothing either. The failure mode worth catching is the
# quiet one: a cluster where the class name is wrong, or the admission controller
# never merged its scheduling, and the workload lands on a normal node as a
# normal container. Everything is Running and Healthy and no isolation exists.
#
# The decisive check is the KERNEL. A container shares its host's kernel -- it
# cannot do otherwise -- so a pod reporting a different kernel version from the
# node it runs on is, necessarily, in its own VM. Nothing else here is as strong:
# labels and class names are claims, and the kernel is a consequence.
#
# usage: scripts/nitro/verify-isolation.sh <pod> [-n <namespace>]
#        scripts/nitro/verify-isolation.sh -l app.kubernetes.io/name=my-agent [-n ns]
set -euo pipefail

POD=""
SELECTOR=""
NAMESPACE="default"

while [ $# -gt 0 ]; do
  case "$1" in
    -l|--selector)  SELECTOR="$2"; shift 2 ;;
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    -h|--help)      sed -n '1,18p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *)  POD="$1"; shift ;;
  esac
done

command -v kubectl >/dev/null || { echo "missing: kubectl" >&2; exit 1; }

if [ -z "$POD" ]; then
  [ -n "$SELECTOR" ] || { echo "need a pod name or -l <selector>" >&2; exit 2; }
  POD="$(kubectl get pod -n "$NAMESPACE" -l "$SELECTOR" \
          -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$POD" ] || { echo "no pod matched $SELECTOR in namespace $NAMESPACE" >&2; exit 1; }
fi

PASS=0
FAIL=0
ok()   { printf '  PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }
note() { printf '  ----  %s\n' "$1"; }

echo "pod: $NAMESPACE/$POD"
echo

RUNTIME_CLASS="$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.spec.runtimeClassName}' 2>/dev/null || true)"
NODE="$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)"
PHASE="$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"

echo "== 1. the pod asked for isolation =="
if [ -z "$RUNTIME_CLASS" ]; then
  bad "no runtimeClassName: this is an ordinary shared-kernel container"
  echo
  echo "If this is an OAP agent, it was deployed without sandbox: true, or the"
  echo "ComponentDefinition on this cluster predates it."
  exit 1
fi
ok "runtimeClassName: $RUNTIME_CLASS"

case "$RUNTIME_CLASS" in
  isolated-sandbox)     FAMILY=nitro; MODE=isolated ;;
  confidential-sandbox) FAMILY=nitro; MODE=confidential ;;
  kata-*)               FAMILY=kata;  MODE=isolated ;;
  *)                    FAMILY=unknown; MODE=unknown ;;
esac
note "runtime family: $FAMILY, trust model: $MODE"

# The one failure this catches that nothing else does: a runtimeClassName that is
# a Helm placeholder rather than a class name. It happens when the
# ComponentDefinition was applied without Helm templating it, and the pod is then
# rejected outright -- but the Application still reports fine.
case "$RUNTIME_CLASS" in
  *'{{'*|*'}}'*)
    bad "runtimeClassName contains an unsubstituted Helm placeholder"
    echo
    echo "The ComponentDefinition on this cluster was applied WITHOUT Helm"
    echo "substituting global.sandboxRuntimeClass. Re-sync the"
    echo "oam-agent-components Application, or apply the output of"
    echo "  helm template <chart> --set global.sandboxRuntimeClass=<class>"
    exit 1 ;;
esac

if ! kubectl get runtimeclass "$RUNTIME_CLASS" >/dev/null 2>&1; then
  bad "RuntimeClass $RUNTIME_CLASS does not exist on this cluster (the pod cannot start)"
else
  ok "RuntimeClass $RUNTIME_CLASS exists"
fi

echo
echo "== 2. the pod is running =="
if [ "$PHASE" = "Running" ]; then
  ok "phase: Running on node $NODE"
else
  bad "phase: $PHASE"
  if [ "$PHASE" = "Pending" ]; then
    echo
    echo "Pending with a valid class usually means NO NODE can take it. This is the"
    echo "designed failure -- not-Ready rather than fake-green -- and the reason is"
    echo "in the pod's events:"
    kubectl get events -n "$NAMESPACE" --field-selector "involvedObject.name=$POD" \
      -o custom-columns='REASON:.reason,MESSAGE:.message' 2>/dev/null | head -5 || true
    echo
    if [ "$FAMILY" = nitro ]; then
      echo "For Nitro: the cluster needs Kubernetes 1.36+, and either EKS Auto Mode"
      echo "(which provisions a sandbox-capable node on demand) or your own nodes from"
      echo "a Nitro Sandbox AMI launched with EnclaveOptions.Enabled=true."
    else
      echo "For Kata: kata_nodepool must be enabled (the nodes) AND"
      echo "agent_sandbox_kata (the runtime on them). One without the other leaves"
      echo "pods Pending."
    fi
  fi
  exit 1
fi

echo
echo "== 3. the node advertises sandbox capacity =="
if [ "$FAMILY" = nitro ]; then
  NITRO_LABEL="$(kubectl get node "$NODE" -o jsonpath='{.metadata.labels.eks\.amazonaws\.com/nitro-sandbox}' 2>/dev/null || true)"
  if [ "$NITRO_LABEL" = "true" ]; then
    ok "node label eks.amazonaws.com/nitro-sandbox=true"
  else
    bad "node $NODE is not labelled eks.amazonaws.com/nitro-sandbox=true"
    note "the RuntimeClass admission controller should have made this impossible"
  fi
  SLOTS="$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.eks\.amazonaws\.com/nitro-sandbox}' 2>/dev/null || true)"
  [ -n "$SLOTS" ] && note "node sandbox slots (allocatable): $SLOTS"
else
  KATA_LABEL="$(kubectl get node "$NODE" -o jsonpath='{.metadata.labels.katacontainers\.io/kata-runtime}' 2>/dev/null || true)"
  if [ -n "$KATA_LABEL" ]; then
    ok "node label katacontainers.io/kata-runtime=$KATA_LABEL"
  else
    bad "node $NODE carries no katacontainers.io/kata-runtime label"
  fi
fi

echo
echo "== 4. the pod has its own kernel (the decisive check) =="
NODE_KERNEL="$(kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.kernelVersion}' 2>/dev/null || true)"
note "node kernel:  ${NODE_KERNEL:-unknown}"

# In confidential mode exec is DENIED BY DESIGN -- the Cedar policy refuses it so
# the parent instance cannot introspect the guest. A failure here is therefore
# the feature working, not a problem, and the script says so rather than
# reporting a scary FAIL.
EXEC_OUT=""
EXEC_RC=0
set +e
EXEC_OUT="$(kubectl exec -n "$NAMESPACE" "$POD" -- sh -c 'uname -r; echo "---"; cat /sys/class/dmi/id/product_name 2>/dev/null; echo "---"; ls /sys/block 2>/dev/null | tr "\n" " "' 2>&1)"
EXEC_RC=$?
set -e

if [ "$EXEC_RC" -ne 0 ]; then
  if [ "$MODE" = confidential ]; then
    ok "exec is DENIED, which is the confidential guarantee working"
    note "the parent instance cannot introspect the guest, so neither can this script"
    note "to verify a confidential sandbox, check the attestation instead: have the"
    note "workload call kms:Decrypt against a key whose policy pins PCR17 and"
    note "PCR8:ServiceName. If it succeeds, the measurements matched."
    echo
    echo "checks passed: $PASS   failed: $FAIL   (in-guest checks skipped by design)"
    exit $(( FAIL > 0 ? 1 : 0 ))
  fi
  bad "could not exec into the pod to read its kernel"
  note "$EXEC_OUT"
  echo
  echo "checks passed: $PASS   failed: $FAIL"
  exit 1
fi

POD_KERNEL="$(printf '%s\n' "$EXEC_OUT" | sed -n '1p')"
PRODUCT="$(printf '%s\n' "$EXEC_OUT" | sed -n '3p')"
BLOCKDEVS="$(printf '%s\n' "$EXEC_OUT" | sed -n '5p')"

note "pod kernel:   ${POD_KERNEL:-unknown}"
if [ -n "$POD_KERNEL" ] && [ -n "$NODE_KERNEL" ]; then
  if [ "$POD_KERNEL" != "$NODE_KERNEL" ]; then
    ok "pod kernel differs from node kernel -- this is a real microVM"
  else
    bad "pod kernel is IDENTICAL to the node's: this is a shared-kernel container"
    note "isolation was requested and is not being delivered"
  fi
else
  note "could not compare kernels"
fi

echo
echo "== 5. corroborating signals =="
# Secondary, and deliberately not fatal: these are what the sandbox looks like
# from inside, and the exact strings are not contractual.
if [ "$FAMILY" = nitro ]; then
  case "$PRODUCT" in
    *.enclave) ok "DMI product name is $PRODUCT (the .enclave suffix marks a Nitro Sandbox)" ;;
    "")        note "DMI product name unreadable (a minimal image may not mount /sys/class/dmi)" ;;
    *)         note "DMI product name is $PRODUCT, with no .enclave suffix -- unexpected, but the kernel check above is what decides" ;;
  esac
  case "$BLOCKDEVS" in
    *vda*|*vdb*) ok "block devices are virtio ($BLOCKDEVS) -- a sandbox sees virtio-blk, a normal pod sees nvme" ;;
    *nvme*)      bad "block devices are NVMe ($BLOCKDEVS) -- that is the node's own storage" ;;
    "")          note "no block devices listed" ;;
    *)           note "block devices: $BLOCKDEVS" ;;
  esac
else
  note "DMI product name: ${PRODUCT:-unreadable}"
  note "block devices: ${BLOCKDEVS:-none listed}"
fi

echo
echo "checks passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
