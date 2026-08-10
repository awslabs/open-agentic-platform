# OAM Component and Trait Authoring (binding)

Applies to everything under `platform/oam/definitions/` and the generated
definitions in `gitops/addons/charts/oam-agent-components/templates/`.

## 1. Developer OAM must be portable

An OAM `Application` written by a developer MUST deploy unchanged to any region,
account, and cluster. It is a bug if a developer has to know where their workload
runs.

**Never require ambient environment values in a developer's OAM.** That includes:

- AWS region
- AWS account id
- cluster name
- cluster endpoint, VPC, subnet, or security group ids
- environment names (`dev`/`prod`) used to select behavior

If a definition needs one of these, it takes it from platform configuration, not
from the developer.

### Where ambient values come from

The platform sources them per-cluster from the cluster secret's annotations and
passes them into the chart as Helm globals:

```
cluster secret annotation aws_region        -> global.awsRegion
cluster secret annotation aws_cluster_name  -> global.clusterName
```

That wiring lives in `gitops/addons/bootstrap/default/addons.yaml` under the
addon's `valuesObject.global`, and the chart's `values.yaml` carries a fallback.

### How to consume them in CUE

Put the Helm placeholder in the CUE parameter default. Helm substitutes it when
the chart renders, before KubeVela ever parses the CUE:

```cue
// +usage=AWS region. Defaults to the cluster's region so the same OAM Application
// is portable across regions; override only for a cross-region resource.
region: *"{{ .Values.global.awsRegion }}" | string
```

Keep it an overridable default rather than a fixed value, so the deliberate
cross-region case stays possible. Never write `region: *"us-east-1" | string`.

For workloads that need the region at runtime, the `aws-service-identity` trait
injects `AWS_REGION` and `AWS_DEFAULT_REGION` from `global.awsRegion`. Do not ask
the developer to set those in `env`.

Constraint to be aware of: Helm parses the file before CUE, so a placeholder must
be valid Helm template syntax. Nested double quotes such as
`{{ .Values.global.awsRegion | default \"us-west-2\" }}` break Helm parsing. Rely
on the chart's `values.yaml` default instead of a `default` function inside the
placeholder.

## 2. CUE is the only source of truth

Every ComponentDefinition and TraitDefinition in the chart MUST have a CUE source
in `platform/oam/definitions/components/` or `.../traits/`.

- **Never hand-edit the generated YAML.** Those files carry a `DO NOT EDIT`
  header and are overwritten.
- Regenerate with `platform/oam/generate.sh`. It needs a reachable KubeVela
  cluster, because `vela def render` resolves cluster packages:
  `KUBECONFIG=.platform/private/hub-kubeconfig ./generate.sh`
- After regenerating, diff and confirm **only** the definitions you intended
  changed. `generate.sh` renders every definition that has a CUE source, so an
  unrelated file appearing in the diff means its YAML has drifted from its source.

A definition whose YAML exists without a CUE source is a latent regression: the
next person to run `generate.sh` either silently deletes hand-added behavior or
cannot regenerate it at all. Adding the missing source is part of touching such a
file, not a follow-up.

### Known violations (fix when touched)

- `agentcore-code-interpreter.yaml` has no CUE source and hardcodes `us-east-1`.
  Slated for removal along with the rest of the in-image AgentCore tooling.
- `decentralized-observability-identity.yaml` has no CUE source.
- `agent.yaml` contains a `command: ["opentelemetry-instrument", "python", ...]`
  block that is absent from `agent.cue`, so regenerating drops it and breaks
  tracing. Tracked in issue #50, which also covers moving auto-instrumentation
  into the image where it belongs.
- `agent.yaml` still has `region: *"us-east-1"` defaults on the browser and
  codeInterpreter parameters, which go away with those parameters.

## 3. Component and trait shape

- A component that runs a workload owns its ServiceAccount, named
  `context.name`, and names its container `context.name`. That single identity
  anchor is what lets `aws-service-identity` and `gateway-identity` attach with no
  extra wiring.
- Use `context.name` / `context.namespace` / `context.appName`. Do not add `name`
  or `namespace` parameters: they let a developer place resources somewhere the
  traits do not follow, which silently breaks identity injection.
- Cross-component wiring is by naming convention, for example a component
  emitting an IAM policy named `<appName>-<component>-iam-policy` and
  `aws-service-identity`'s `accessFor` attaching it. These conventions are not
  checked at render time, so a rename on either side fails silently. Keep the
  convention documented next to both sides.
- Managed resource names are cluster-scoped. Prefer names that include the
  namespace when collisions across namespaces are possible.

## 4. Least privilege

IAM policies emitted by components should be scoped to the resource and the
actions actually needed. `"Action": ["service:*"]` on `"Resource": "*"` is not
acceptable in new work; the existing AgentCore components do this and should be
tightened.

## Review checklist

- [ ] No region, account id, or cluster name required in the example OAM app
- [ ] Every new or changed definition has a CUE source, and `generate.sh` output
      is committed
- [ ] `generate.sh` produced no unintended diffs in other definitions
- [ ] Verified by rendering with a non-default value, e.g.
      `helm template ... --set global.awsRegion=eu-west-1`, to prove the value
      actually flows rather than matching a coincidental default
- [ ] Definitions pass `kubectl apply --dry-run=server`
- [ ] IAM policies are scoped, not wildcards
