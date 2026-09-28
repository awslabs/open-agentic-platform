# Integration test branch — tracked feature PRs (OAP)

Integration test branch (disposable; rebuild from PR heads; never merge to `main`).

## OAP feature PR under test
- **awslabs/open-agentic-platform#37** — `eks-read-access` ComponentDefinition +
  faster `langfuse-otel-auth` ExternalSecret self-heal.
  - **Integrated on this branch:** the otel-auth self-heal (`refreshInterval` 1h→1m).
  - **NOT on this branch (upstream-only):** the `eks-read-access` ComponentDefinition.
    The deployed PEEKS agent (appmod chart) grants eks-read discovery via an equivalent
    **inline** Crossplane IAM Policy (no ComponentDefinition dependency — more robust,
    same lesson as the `agent-fixed` outage), so the CD is the reusable upstream form
    for OAP-native agents rather than a dependency of this deployment.

## Companion appmod branch
- appmod-blueprints fork branch `integration/peeks-e2e-unified` — carries PR
  aws-samples/appmod-blueprints#926 (`peeks-agent`).

_Curated to the feature PRs deliberately under test; the branch also inherits the fork's
full merged history, which is not enumerated here._
