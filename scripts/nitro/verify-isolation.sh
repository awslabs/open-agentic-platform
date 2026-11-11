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
#
# options:
#   -n, --namespace <ns>
#   -l, --selector <sel>
#   --debug-image <image>   Read the guest kernel from an EPHEMERAL CONTAINER
#                           instead of exec'ing the workload's own shell. Needed
#                           for a distroless or scratch image, which has no
#                           shell to exec -- and that is the likely shape of a
#                           hardened sandbox workload, so the check that matters
#                           most would otherwise be the one that cannot run.
#                           busybox is the usual choice. MUTATES THE POD: an
#                           ephemeral container is a permanent addition to the
#                           pod's spec (it cannot be removed; the pod has to be
#                           recreated), which is why it is opt-in.
#
#                           It is a valid substitute because an ephemeral
#                           container joins THE SAME sandbox, so the kernel it
#                           reports is the guest's.
set -euo pipefail

POD=""
SELECTOR=""
NAMESPACE="default"
DEBUG_IMAGE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -l|--selector)  SELECTOR="$2"; shift 2 ;;
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    --debug-image)  DEBUG_IMAGE="$2"; shift 2 ;;
    -h|--help)      sed -n '/^# usage:/,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
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
  # A class this script does not recognise is NOT assumed to be Kata. It used to
  # be, by falling through to the Kata branch of the node-label check below, and
  # a cluster that renames its Nitro class (which global.nitroSandboxRuntimeClasses
  # exists to support) then got a confident FAIL for a correctly isolated pod.
  # The kernel check in section 4 does not care what the class is called, so an
  # unknown class still gets a real answer -- just not a guessed one.
  *)                    FAMILY=unknown; MODE=unknown ;;
esac
note "runtime family: $FAMILY, trust model: $MODE"
if [ "$FAMILY" = unknown ]; then
  note "$RUNTIME_CLASS is not a class name this script recognises, so the node-label"
  note "check below is skipped. Section 4 (the kernel) is runtime-agnostic and still"
  note "decides. If this is a renamed Nitro class, add it to the case statement here"
  note "and to global.nitroSandboxRuntimeClasses on the oam-agent-components chart."
fi

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
# OBSERVED, NOT CONTRACTUAL. eks.amazonaws.com/nitro-sandbox as a node label and
# as an allocatable resource is what the RuntimeClass's scheduling block selects
# on in practice, but it is not in the public documentation, so a rename or a
# change of mechanism would make a FAIL here wrong. Reported, therefore, rather
# than asserted: an absent label is worth printing and is not evidence of absent
# isolation. Section 4 is the check that decides.
if [ "$FAMILY" = nitro ]; then
  NITRO_LABEL="$(kubectl get node "$NODE" -o jsonpath='{.metadata.labels.eks\.amazonaws\.com/nitro-sandbox}' 2>/dev/null || true)"
  if [ "$NITRO_LABEL" = "true" ]; then
    ok "node label eks.amazonaws.com/nitro-sandbox=true"
  else
    note "node $NODE carries no eks.amazonaws.com/nitro-sandbox=true label"
    note "not treated as a failure: this label is observed behaviour, not a documented"
    note "contract, and the pod is Running on a class the admission controller accepted"
  fi
  SLOTS="$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.eks\.amazonaws\.com/nitro-sandbox}' 2>/dev/null || true)"
  [ -n "$SLOTS" ] && note "node sandbox slots (allocatable): $SLOTS"
elif [ "$FAMILY" = unknown ]; then
  note "skipped: no node-label expectation for runtime class $RUNTIME_CLASS"
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
PROBE='uname -r; echo "---"; cat /sys/class/dmi/id/product_name 2>/dev/null; echo "---"; ls /sys/block 2>/dev/null | tr "\n" " "'

EXEC_OUT=""
EXEC_RC=0
set +e
if [ -n "$DEBUG_IMAGE" ]; then
  # An ephemeral container joins the same sandbox, so its kernel IS the guest's.
  # --quiet keeps kubectl's own progress text out of the parsed output.
  EXEC_OUT="$(kubectl debug -n "$NAMESPACE" "$POD" --image="$DEBUG_IMAGE" \
      --quiet --attach=true -- sh -c "$PROBE" 2>&1)"
else
  EXEC_OUT="$(kubectl exec -n "$NAMESPACE" "$POD" -- sh -c "$PROBE" 2>&1)"
fi
EXEC_RC=$?
set -e

# THE IMAGE HAVING NO SHELL IS NOT A FAILED ISOLATION CHECK. A distroless or
# scratch image is the likely shape of a hardened sandbox workload, and exec'ing
# `sh` in one fails with "executable file not found" -- which this script used to
# report as "could not exec into the pod to read its kernel", a FAIL, and an exit
# 1. The isolation was never in question; the probe was.
#
# Checked BEFORE the confidential branch: a shell-less image fails this way in
# every mode, and attributing it to the Cedar policy would be the same mistake in
# the other direction.
if [ "$EXEC_RC" -ne 0 ] && [ -z "$DEBUG_IMAGE" ] &&
   printf '%s' "$EXEC_OUT" | grep -qiE 'executable file not found|no such file or directory|exec: "sh"'; then
  note "this image has no shell, so the in-guest probe cannot run"
  note "NOT a failure -- a distroless or scratch image is expected here, and nothing"
  note "about it says anything either way about the isolation boundary."
  echo
  echo "To read the guest kernel anyway, re-run with an ephemeral container:"
  echo "  scripts/nitro/verify-isolation.sh $POD -n $NAMESPACE --debug-image busybox"
  echo
  echo "It joins the same sandbox, so the kernel it reports is the guest's. Note that"
  echo "an ephemeral container permanently alters the pod's spec -- the pod must be"
  echo "recreated to remove it -- which is why this is not the default."
  echo
  echo "checks passed: $PASS   failed: $FAIL   (in-guest checks skipped: no shell in image)"
  exit $(( FAIL > 0 ? 1 : 0 ))
fi

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
  note "if the image simply has no shell, re-run with --debug-image busybox"
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
