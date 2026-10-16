# agents

Sample agentic workload for the Platform Engineering on Amazon EKS workshop.

There is no source code and no image to build. The agent runs the platform's
prebuilt agent image and the MCP server uses a prebuilt time server image, so the
only thing you change is the deployment manifest.

| Path | What it is |
|---|---|
| `deployment/templates/application.yaml` | Starting manifest: one agent, no tools |
| `deployment/templates/time-server.yaml` | MCP server component the workshop merges into the Application (amd64 image, pinned by digest) |
| `deployment/dev/` | Synced by Argo CD application `agents-dev-cd` to the dev spoke |
| `deployment/prod/` | Synced by Argo CD application `agents-prod-cd` to the prod spoke |
| `oap-curl.sh` | Sends a message to an agent and prints the reply |

Talk to the agent running on the cluster in your current kubectl context:

```bash
./oap-curl.sh agent assistant "What time is it in UTC?"
```
