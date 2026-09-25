# Platform Portability (binding)

Applies to installation and provisioning: `Taskfile.yml`, `config.yaml`, anything
under `workshop/`, and any change that asks the base platform
(appmod-blueprints) for a new input.

## The tenet

**Every platform capability is a customer capability, not a workshop feature.** A
customer running in their own account must be able to reach a working platform
with whatever they have: no registered domain, an existing VPC or none, and no
pre-provisioned learning environment. A workshop is one caller of that
capability, never the reason it exists.

The practical test: if a capability only works when some other system has already
staged the environment, it is not a platform capability yet.

This is the same rule `oam-authoring.md` §1 applies to a developer's OAM
Application, one layer down. There it is "a developer must not need to know where
their workload runs." Here it is "an operator must not need a curated account to
install."

## 1. No ambient environment coupling

Provisioning logic must never **require** any of the following. Reading them as an
optional convenience is fine; depending on them is not.

- CloudFormation stack lookups to discover configuration
- Environment variables injected by a specific learning platform, for example
  anything named `WS_*` or `WORKSHOP_*`
- Files sourced from outside the repository, for example `/etc/profile.d/*`
- Hardcoded IDE or workspace paths, for example `/home/ec2-user/environment`
- A specific repository checkout name or location
- Resources discovered only by a tag that a specific bootstrap writes

Two failure modes to watch for, because both have already happened upstream:

**Absence treated as "wait" rather than "not applicable."** Gating on a file that
a foreign bootstrap writes turns a self-paced install into a timeout followed by
an unexplained fallback. Gate on the real precondition instead, such as the
configured VPC being describable.

**Ambient values used to work around a templating bug.** If a task variable does
not survive into a subshell, pass the value explicitly. Reaching for an
environment variable that happens to exist in one environment converts a local
bug into a portability bug, and it fails silently off that environment.

## 2. Cluster providers must be capability-symmetric

A capability available on one cluster provider must be available on all of them,
or it does not exist as a platform contract. Consumers select a provider for
reasons unrelated to features, so an asymmetric capability silently removes
choice.

When a contract is documented as generic, implementing it in one provider only is
an incomplete change, not a phased one. Either land it everywhere or do not
document it as a contract.

## 3. How values are allowed to arrive

In order of preference:

1. **Explicit configuration.** `config.local.yaml` is the contract. Every input a
   customer must vary belongs there, with a documented default.
2. **Discovery as a fallback.** Auto-detecting a value is a convenience layer over
   an explicit field, never a replacement for it. It must degrade to a clear error
   or a documented default, never to a wrong guess.
3. **Repository-relative files for asynchronous values.** When a value cannot be
   known before install starts, such as a CloudFront hostname that does not exist
   until the distribution deploys, write it to a repo-relative file and have the
   consumer poll. Files survive backgrounded subshells, which exported variables
   do not.

Never a fourth option. If none of these three can carry a value, the design is
wrong.

## 4. Exposure without a customer domain

Supporting a customer who owns no domain is a first-class install mode, not
workshop scaffolding. It means:

- The platform serves plain HTTP at the load balancer and something in front
  terminates TLS. In the base platform this is `insecure: true`.
- `domain` is empty at config time and resolved later from the async file above.
- There is exactly one hostname, so every endpoint is path-routed. Charts must
  not assume a per-service subdomain exists, and must not request a certificate
  for one.

Charts in this repo already implement the last point by guarding on the ingress
host (`gitops/addons/charts/langfuse/templates/ingress.yaml:9`,
`gitops/addons/charts/agent-gateway/templates/ingress.yaml:30`). Keep new charts
consistent with that guard rather than adding a second mechanism.

## Known upstream violations

Recorded against appmod-blueprints at `737f081f`, so expect drift. These are
examples of the rules above, not a work list for this repo.

- `cluster-providers/kind-crossplane/Taskfile.yaml:49` accepts only
  `hub.vpcCidr` and always creates a VPC. `kind-kro-ack/Taskfile.yaml:30-37`
  additionally imports an existing one via `hub.network.vpcId`. Rule 2.
- `kind-kro-ack` polls `private/async-domain` through a dedicated
  `hub:wait-for-domain` task (`:580`) and gates `hub:seed` on it (`:905-911`).
  `kind-crossplane` reads that file in one unrelated task only (`:1138`), so its
  main install path cannot use the documented contract. Rule 2.
- Both providers resolve the repo root as
  `${WORKSPACE_PATH:-/home/ec2-user/environment}/${WORKING_REPO:-platform-on-eks-workshop}`
  (`kind-kro-ack:200`, `kind-crossplane:204`). Rule 1, second failure mode.
- `workshop/create-config.sh:355` waits on the existence of
  `/etc/profile.d/workshop.sh`, a file with no writer in that repository, as a
  proxy for VPC readiness. Rule 1, first failure mode.

## Review checklist

- [ ] The change works in an account with no domain, no pre-created VPC, and no
      learning-environment bootstrap
- [ ] Every new input is an explicit field in `config.yaml` with a default, and is
      documented
- [ ] Any auto-detection has an explicit override and fails loudly rather than
      guessing
- [ ] No CloudFormation lookup, `WS_*` variable, sourced external file, or
      hardcoded workspace path was added
- [ ] A capability added for one cluster provider was added for all, or is not
      described as a platform contract
- [ ] Values needed inside a backgrounded subshell are passed explicitly
- [ ] Verified by running with a non-default value, not by reading the code
