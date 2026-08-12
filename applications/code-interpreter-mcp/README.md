# code-interpreter-mcp

An MCP server that exposes Amazon Bedrock AgentCore Code Interpreter to agents through
agentgateway. Agents get a stateful Python sandbox as ordinary MCP tools, with no AWS SDK and
no AWS credentials of their own.

`DESIGN.md` covers why it is built this way and what was measured. This file is how to run it.

## Quick start

The fastest path uses the AWS built-in interpreter, which needs no provisioning:

```bash
npm install
eval "$(aws configure export-credentials --format env)"
AWS_REGION=us-west-2 node src/server.js
```

Then, in another shell:

```bash
BASE_URL=http://localhost:8000 node test/e2e-multisession.js
```

To use a provisioned interpreter instead, set `AGENTCORE_CODE_INTERPRETER_NAME` to its name.
The server resolves the name to an id at startup, retrying until it appears.

## Deploying on the platform

Two OAM components: one provisions the interpreter, one runs this server.

```bash
kubectl apply -f ../../platform/oam/examples/example-code-interpreter-mcp.yaml
kubectl apply -f ../../platform/oam/examples/example-agent-with-code-interpreter.yaml
```

An agent consumes it with configuration only, no image change:

```yaml
mcpServers:
  - name: code-interpreter-mcp
traits:
  - type: gateway-identity
```

## Tools

| Tool | Use it for | Notes |
|---|---|---|
| `executeCode` | computation | python, javascript, typescript. `clearContext` resets variables. |
| `executeCommand` | shell, synchronous | The right choice for nearly every command. |
| `startCommandExecution` | shell, asynchronous | Returns a `taskId`. Only for jobs lasting minutes. |
| `getTask` | polling a background job | Needs a `taskId`. Terminal statuses carry stdout, stderr, exit code. |
| `stopTask` | cancelling a background job | Cancelling a finished task returns an error; that is expected. |
| `writeFiles` / `readFiles` / `listFiles` / `removeFiles` | the sandbox filesystem | Paths are relative to the session working directory. |

Two behaviours worth knowing before you build on this:

- **Sessions are stateful.** Variables and files persist across calls within one MCP session.
  Each MCP session gets its own interpreter, so conversations never share state.
- **Idle sessions are released** after `SESSION_IDLE_SECONDS`, which discards variables *and*
  files. Release is suppressed while a background task is still running, so
  `startCommandExecution` is safe across quiet periods.

## Configuration

All environment variables, so a team tunes them through the `mcp-server` component's `env`
list without rebuilding the image.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCORE_CODE_INTERPRETER_NAME` | *(empty)* | Interpreter name, resolved to an id at boot. Empty uses the AWS built-in. |
| `AGENTCORE_BUILTIN_ID` | `aws.codeinterpreter.v1` | The built-in used when no name is set. |
| `SESSION_TIMEOUT_SECONDS` | `900` | TTL requested per session. AWS enforces it, and it bounds background tasks. |
| `SESSION_IDLE_SECONDS` | `300` | Release an idle session. Suppressed while tasks are running. |
| `MAX_INTERPRETER_SESSIONS` | `25` | Cap on concurrent live sessions for this pod. |
| `INTERPRETER_READY_TIMEOUT_SECONDS` | `300` | Budget for one initialisation cycle. |
| `INIT_RETRY_SECONDS` | `15` | Pause between cycles. Initialisation never gives up. |
| `MCP_SESSION_IDLE_SECONDS` | `1800` | Reap an MCP session whose client vanished. |
| `REAPER_INTERVAL_SECONDS` | `60` | Sweep frequency. |
| `TOOLS_ALLOW` / `TOOLS_DENY` | *(empty)* | Comma-separated. Narrow the advertised surface. |
| `MCP_PORT` / `MCP_PATH` | `8000` / `/mcp` | Listener. |
| `AWS_REGION` | *(required)* | Injected by the `aws-service-identity` trait on the platform. |

## Endpoints

| Path | Purpose |
|---|---|
| `/mcp` | StreamableHTTP MCP endpoint (POST, GET, DELETE) |
| `/healthz` | Liveness. 200 as soon as the process serves HTTP. |
| `/readyz` | Readiness. 503 until the interpreter is resolved, then reports session counters. |

Keeping liveness and readiness separate is deliberate: a cold start can take minutes when a
freshly attached IAM policy is still propagating, and liveness must not kill the pod while
that happens.

## Tests

Each needs a running server and real AWS credentials. All run against the live service.

| Test | Proves | Notes |
|---|---|---|
| `test/e2e-multisession.js` | lazy activation, session isolation, file round-trip, error mapping, teardown | `BASE_URL=...` |
| `test/tool-matrix.js` | every advertised tool works with only its required arguments | Run this after any schema change. |
| `test/task-eviction.js` | a running background task suppresses idle eviction | Needs `SESSION_IDLE_SECONDS=15`; pass `IDLE=15`. |
| `test/gateway.js` | the real agent path through agentgateway with a JWT | Needs `GW_URL` and `TOKEN`. |
| `test/concurrency-cost.js` | per-session cost and activation timing | `N=8 PARALLEL=1` for a stampede. |

```bash
# the two that need non-default settings
AWS_REGION=us-west-2 MCP_PORT=8032 SESSION_IDLE_SECONDS=15 node src/server.js
BASE_URL=http://localhost:8032 IDLE=15 node test/task-eviction.js

GW_URL=https://<gateway> MCP_PATH=/mcp/code-interpreter-mcp TOKEN=$(...) \
  DIRECT_URL=http://localhost:8040 node test/gateway.js
```

`test/tool-matrix.js` earns its place: the schema once advertised `executeCode`'s `language`
parameter as optional while the service rejected calls without it, and every other test passed
it explicitly. A model that read the schema hit a hard failure and then answered from its own
knowledge while claiming it had used the sandbox. Run the matrix whenever a tool schema
changes.

## Building

```bash
podman build --platform linux/arm64 -t code-interpreter-mcp:dev -f Dockerfile .
```

Published as `public.ecr.aws/z0a4o2j5/code-interpreter-mcp`, multi-arch for amd64 and arm64.
The image is a single Node process with no children, so there is no init shim; Node is PID 1.
