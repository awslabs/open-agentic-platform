# Fleet members (examples, not active)

A fleet member registers a cluster with ArgoCD. A git generator scans
`fleet/members/*/values.yaml` and mints an ArgoCD **cluster secret** per match, and every
ApplicationSet then fans out across those secrets. One member here becomes tens of
Applications.

Files in this directory are named `<cluster>.values.yaml.example`, so they do **not** match
that glob and nothing is registered until you opt in. `fleet/members/` is otherwise empty,
matching appmod-blueprints, which ships it empty with an `_example`.

## Why these are examples

`spoke-dev` and `spoke-prod` were real members here. That registered both spokes on every
install regardless of the `spokes` map in `config.local.yaml`, which feeds only the
imperative `task spokes:install`. On a hub-only install the result was cluster secrets for
two clusters that were never created, and ~60 Applications stuck `Unknown` against them.

## Adding a spoke

Registration and provisioning are separate, and you need both:

1. **Register** it so ArgoCD gets a cluster secret:

   ```bash
   mkdir -p gitops/fleet/members/spoke-dev
   cp gitops/fleet/members/_example/spoke-dev.values.yaml.example \
      gitops/fleet/members/spoke-dev/values.yaml
   ```

2. **Provision** it, in the file matching your `platform.clusterProvider`:

   | Provider | Declare the cluster in |
   |---|---|
   | `kind-crossplane` | `fleet/spoke-values/tenants/<tenant>/crossplane-clusters/values.yaml` |
   | `kind-kro-ack` | `fleet/spoke-values/tenants/<tenant>/kro-clusters/` (`values.yaml` plus `clusters/<name>.json`) |

   These paths are globbed by different ApplicationSets (`clusters` and `clusters-kro`), so a
   declaration written for one provider is silently ignored under the other. This repo
   currently ships only the crossplane path.

Registering without provisioning gives the `Unknown` Applications described above.
Provisioning without registering creates a cluster ArgoCD will not deploy to.

Set `resourcePrefix` to your own value rather than inheriting one from a reference
environment.
