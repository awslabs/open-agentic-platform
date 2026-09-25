# Install with no registered domain

Reach the platform over HTTPS through a free `*.cloudfront.net` hostname, for a customer
account with no domain to spare.

```bash
cd workshop && task install
```

## What this directory owns

CloudFront, and nothing else. It sets two values in `../config.local.yaml`, `domain` and
`insecure`, and calls the two CloudFront scripts the platform already ships. Everything else
is delegated:

| Layer | Command | Responsibility |
|---|---|---|
| `workshop/` | `task install` | reserve a hostname, set `domain` + `insecure`, attach the origin |
| repo root | `task install` | OAP: base platform, spokes, agentic components |
| appmod-blueprints | `task install` | hub cluster and platform addons |

Neither the root Taskfile nor the platform knows CloudFront exists. `insecure` is a generic
platform contract meaning "the consumer terminates TLS upstream", so the root simply passes it
through.

## Why a hostname has to exist first

The platform needs its ingress hostname at install time. Keycloak realm URLs, the OIDC issuer,
every ingress host rule, and the ArgoCD and Backstage base URLs are all derived from `domain`,
and ArgoCD begins deploying addons at the end of the install. Installing with an empty domain
misconfigures all of them.

Without a domain of your own that is circular: a CloudFront hostname comes from a
distribution, a distribution needs an origin, the origin is the platform's load balancer, and
the load balancer is created by the install that needs the hostname.

The way out is that `create-distribution` returns its `DomainName` immediately and CloudFront
does not validate the origin at creation time. So the name is reserved against a deliberately
unresolvable placeholder (`placeholder.invalid`, reserved by RFC 2606) in about a second, and
`domain` becomes an ordinary static config value. The real origin is attached after the
install.

## The three steps

| Step | Task | Duration | State afterwards |
|---|---|---|---|
| reserve | `task reserve` | ~1s | hostname exists, distribution serves errors |
| install | `cd .. && task install` | ~30-40 min platform, then OAP | platform up, ALB created near the end |
| attach | `task attach` | ~9 min | VPC origin deploys, then the origin swap |

CloudFront then needs a few minutes to propagate. The distribution serving errors between
reserve and attach is expected: nothing depends on it being reachable during the install.

`task install` runs all three in order.

## What `insecure: true` does

It is required in this mode, and it does two things:

- the ALB serves plain HTTP, because CloudFront terminates TLS
- the platform creates the ALB as `internal` with the predictable name
  `<clusterName>-platform`, which is what `task attach` looks for and what a CloudFront VPC
  origin requires

## Traffic path

CloudFront reaches the ALB through a **VPC origin**, so the ALB has no public address and
platform traffic never crosses the internet. Letting CloudFront reach an internet-facing ALB as
an ordinary custom origin would remove the 9-minute wait, but since `insecure` means the ALB
speaks HTTP, that traffic would be plaintext over the public internet. Restricting the ALB's
security group to CloudFront's origin-facing prefix list controls who may connect without
encrypting anything. That is why attach is a step rather than an option.

## Prerequisites

Required:

- an IAM Identity Center instance in the target region plus an admin group, recorded under
  `identityCenter` in `../config.local.yaml`
- AWS credentials covering EKS, EC2, IAM, Secrets Manager, ELBv2, CloudFront, AMP and Grafana
- `task`, `yq`, `jq`, `kubectl`, `helm`, `aws`, and `kind` with a container runtime
- `platform.ref` in `../config.local.yaml` pointing at a revision that ships the CloudFront
  scripts. They arrived in appmod-blueprints PR #888 and no release tag contains them yet, so
  that means `main`. `task platform` checks this and fails early if not.

Deliberately not required, do not pre-create these:

- a VPC. The platform creates its own.
- an ALB. The load balancer controller creates it during the install.
- a CloudFront distribution or VPC origin. Reserve and attach own them.

## Provider

Set `platform.clusterProvider` in `../config.local.yaml` to `kind-kro-ack`. That is the
provider this flow was tested end to end on. `kind-crossplane` is expected to work, since
nothing here needs a pre-existing VPC, but is unverified for CloudFront. `task reserve` warns
if you are on anything else and continues.

Be aware that appmod's `config.schema.json` restricts `clusterProvider` to `kind-crossplane`
and `byoc`, so an editor will flag `kind-kro-ack` even though `cluster-providers/kind-kro-ack`
exists and appmod's own `workshop/create-config.sh` defaults to it. The schema is an editor
hint (`config.yaml:3`) and is not enforced at install, so this is an upstream schema defect
rather than a real constraint.

## Re-running

Both scripts are idempotent. `task reserve` returns the existing distribution's hostname rather
than creating a second one, matching on the distribution `Comment` of `<clusterName>-platform`.
`task attach` exits having changed nothing when the distribution already points at the current
ALB.

Re-running `task attach` is also the fix for one specific failure. An ALB's scheme is
immutable, so if it is ever replaced the VPC origin is left bound to a load balancer that no
longer exists and every platform URL hangs with `curl 000`. A VPC origin cannot be re-pointed
while attached to a distribution, so the repair is create-new, swap, delete-old, which attach
performs automatically when it sees the mismatch.

## Teardown

CloudFront resources are not removed by either script. A distribution cannot be deleted while
enabled, and a VPC origin cannot be deleted while a distribution references it, so the order is
disable the distribution, wait for `Deployed`, delete it, then delete the VPC origin.

`task <provider>:destroy` in the platform does call `scripts/sweep-cloudfront.py`, which covers
that sequence for distributions whose `Comment` starts with the cluster name. Two things it
does not cover: Aurora **DB clusters** (it deletes `devlake-*` DB *instances* only, so an
Aurora cluster survives) and target groups. Check for both after a teardown rather than
trusting the exit code.

## Environment notes

- A stale `public.ecr.aws` credential in `~/.docker/config.json` or the OS keychain causes a
  hard 403 on Helm chart pulls instead of falling back to anonymous. Refresh it with
  `aws ecr-public get-login-password --region us-east-1 | helm registry login --username AWS
  --password-stdin public.ecr.aws`.
- With no Docker daemon, `kind` needs `KIND_EXPERIMENTAL_PROVIDER=podman`. The platform
  Taskfiles do not set it.
