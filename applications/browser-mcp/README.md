# browser-mcp

Fronts Amazon Bedrock AgentCore Browser as an MCP server, so agents get browser
tools through agentgateway like any other MCP tool.

```
agent --(JWT via agentgateway)--> [browser-mcp] --(SigV4 / CDP wss)--> AgentCore Browser
```

Node-only. The pod holds no browser: it signs CDP connections and routes MCP
traffic, so one pod serves many concurrent browser sessions. The only child
process is the pinned `chrome-devtools-mcp` CLI, one per active session.

See [DESIGN.md](DESIGN.md) for the architecture, decisions, and verified results.

## Behaviour

- **Lazy.** Nothing is provisioned at startup. Tools are discovered at boot
  without a browser, so `initialize` and `tools/list` cost nothing. An AgentCore
  browser session is minted on the first real `tools/call`.
- **Isolated.** Each MCP session gets its own AgentCore browser session and its
  own `chrome-devtools-mcp` child, so conversations never share cookies or pages.
- **Self-releasing.** An idle session's browser is stopped and transparently
  re-minted on the next call. Abandoned MCP sessions are reaped.

## Configuration

All via environment. Region and credentials come from EKS Pod Identity (the
`aws-service-identity` trait), so the same image and OAM work in any region.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTCORE_BROWSER_NAME` | — | Browser to use. Resolved to an id at runtime. A built-in id such as `aws.browser.v1` is used directly. Required unless `AGENTCORE_BROWSER_ID` is set. |
| `AGENTCORE_BROWSER_ID` | — | Explicit browser id, skips name resolution. |
| `AWS_REGION` | — | Injected by the platform. Required. |
| `MCP_PORT` / `MCP_PATH` | `8000` / `/mcp` | HTTP surface for agentgateway. |
| `SESSION_TIMEOUT_SECONDS` | `900` | TTL requested when minting a browser session. Enforced by AWS, so it is the only bound on a leak if this pod dies hard. Matches the AgentCore default. |
| `SESSION_IDLE_SECONDS` | `300` | Release a session's browser after this long with no tool calls. `0` disables. |
| `MCP_SESSION_IDLE_SECONDS` | `900` | Reap an MCP session with no requests at all. `0` disables. |
| `REAPER_INTERVAL_SECONDS` | `60` | How often to scan for abandoned sessions. |
| `MAX_BROWSER_SESSIONS` | `25` | Cap on concurrent live browser sessions per pod. Keep under the account quota. |
| `TOOLS_ALLOW` / `TOOLS_DENY` | — | Comma-separated tool names to narrow the advertised surface. `TOOLS_ALLOW` wins. |
| `BROWSER_READY_TIMEOUT_SECONDS` | `300` | Budget for ONE initialisation cycle to resolve the browser. |
| `INIT_RETRY_SECONDS` | `15` | Pause between initialisation cycles. Initialisation retries forever; it never gives up, so the pod recovers on its own once the dependency appears. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`. |

Endpoints:

- `/mcp` — MCP (StreamableHTTP)
- `/healthz` — liveness. Answers as soon as the process is serving HTTP, and stays
  200 even if AWS is unreachable, so a slow or failing dependency can never cause a
  restart loop.
- `/readyz` — readiness. `503 {"status":"initializing"}` until the browser id and
  tool catalog are resolved, then `200` with session and tool counts.

Point the liveness probe at `/healthz` and the readiness probe at `/readyz`. Using
one path for both reintroduces the restart loop this split exists to prevent: the
server has to call AWS before it can serve, and on a first deploy that call can fail
for minutes while a freshly attached IAM policy propagates.

## Image

```
public.ecr.aws/z0a4o2j5/browser-mcp:0.1.4
```

Multi-arch (`linux/arm64` + `linux/amd64`), 184 MB, no Chromium bundled.
Manifest list digest `sha256:57df2de8f256956678748abb4c52bddb6dc3305e073a80ce000b59f297911fa9`.
Pin by digest in production if you want immutability.

Tags: `0.1.4` (tini reaps orphaned watchdogs; child death releases the session),
`0.1.3` (initialisation retries forever, so the pod always self-heals),
`0.1.2` (listens before initialising; split liveness/readiness),
`0.1.1` (TTL default 900s), `0.1.0` (initial, TTL default 3600s).

All tunables are environment variables, so a team overrides them per application
through the `mcp-server` component's `env` list with no rebuild. See
`platform/oam/examples/example-browser-mcp.yaml`.

## Releasing resources

All four teardown paths stop the AgentCore browser session, so sessions are never
left running to burn down their TTL:

| Trigger | Behaviour |
|---|---|
| Client sends `DELETE /mcp` (`terminateSession()`) | Browser session stopped, MCP session dropped. |
| Session idle past `SESSION_IDLE_SECONDS` | Browser stopped; MCP session kept alive and re-mints on the next call. |
| Client disappears without `DELETE` | Reaper drops the session after `MCP_SESSION_IDLE_SECONDS`. |
| Pod receives `SIGTERM` (rollout, scale-down) | All live sessions released before exit. |

Note that the MCP client's `close()` alone does NOT release the server session;
`terminateSession()` is what sends the DELETE. The reaper exists precisely because
clients cannot be trusted to do so.

## Restricting which tools agents see

All 29 upstream tools are advertised by default. There are two independent ways to
narrow that, and neither requires rebuilding the image:

1. **At agentgateway (recommended, per application).** The `mcp-server` component
   takes an `authPolicy` that emits an `AgentgatewayPolicy` with CEL match
   expressions bound to the backend, so the gateway enforces tool-level
   authorization for every caller:

   ```yaml
   properties:
     authPolicy:
       action: Allow          # or Deny
       matchExpressions:
         - 'mcp.tool.name in ["navigate_page", "take_snapshot", "click", "fill"]'
   ```

2. **At this server** via `TOOLS_ALLOW` / `TOOLS_DENY`, which changes what is
   advertised in `tools/list` at all. Useful for trimming agent context rather
   than for enforcing access.

Use the gateway policy for authorization, and `TOOLS_ALLOW` if you also want the
agent to stop seeing tools it should not use.

## Local development

Build:

```bash
podman build --platform linux/arm64 -t browser-mcp:dev .
```

Run against the built-in browser (never attach a shell to it; run detached):

```bash
eval "$(aws configure export-credentials --format env)"
podman run -d --name bmcp -p 8010:8000 \
  -e AWS_REGION=us-west-2 \
  -e AGENTCORE_BROWSER_NAME=aws.browser.v1 \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  browser-mcp:dev
```

Tests hit a running server and self-terminate, so they cannot hang:

```bash
node test/e2e-multisession.js            # lazy start, real navigation, 2 isolated sessions, teardown
BASE_URL=http://localhost:8011 \
  node test/idle-eviction.js             # needs SESSION_IDLE_SECONDS=5 on the server
BASE_URL=http://localhost:8012 \
  node test/abandoned-session.js         # needs MCP_SESSION_IDLE_SECONDS=5 REAPER_INTERVAL_SECONDS=5
```

Both require real AWS credentials with `bedrock-agentcore` browser session
permissions, and they do start (and stop) real AgentCore browser sessions.
