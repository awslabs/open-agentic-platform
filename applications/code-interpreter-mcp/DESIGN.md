# code-interpreter-mcp

Amazon Bedrock AgentCore Code Interpreter exposed to agents as an **MCP server behind
agentgateway**, so code execution is consumed like any other MCP tool and is opted into
per application.

```
agent --(JWT via agentgateway)--> [code-interpreter-mcp] --(AWS SDK)--> AgentCore Code Interpreter
```

Tracked by issue #51. Sibling of `applications/browser-mcp`, and deliberately shaped the
same way so the two are operationally interchangeable, but materially simpler for one
reason: the Code Interpreter is an ordinary request/response API, while AgentCore Browser
exposes only a SigV4-signed CDP WebSocket.

## Why an MCP server rather than tools in the agent image

PR #43 compiled AgentCore tooling into `strands-agent-base`, so every agent inherited the
AWS SDK, AWS credentials and those tools whether it used them or not. Fronting the service
with an MCP server instead keeps agents generic, makes code execution opt-in per
application, and puts the tool surface under gateway policy.

The consumer side needs **no image change**. An agent picks this up with configuration only:

```yaml
mcpServers:
  - name: code-interpreter-mcp
traits:
  - type: gateway-identity
```

`MCP_SERVER_NAMES` becomes `<gateway>/mcp/code-interpreter-mcp`, and the agent authenticates
with the projected ServiceAccount token the `gateway-identity` trait mounts. Verified for
the browser server, where an agent loaded all 29 tools over exactly this path.

## What the service actually is

Established by probing the live service; see `spikes/agentcore-code-interpreter/FINDINGS.md`
for raw output.

- A **session is a live interpreter**. Variables persist between calls and so does its
  filesystem. Setting `x = 6 * 7` in one call and printing it in the next returned
  `x is still 42`. Two conversations must therefore never share a session.
- One API does everything: `InvokeCodeInterpreter(codeInterpreterIdentifier, sessionId,
  name, arguments)`, where `name` is one of nine operations and `arguments` is a single
  flat shape (`code`, `language`, `clearContext`, `command`, `path`, `paths`, `content`,
  `directoryPath`, `taskId`, `runtime`).
- Results are close to MCP-native: `{ content: ContentBlock[], structuredContent, isError }`.
  `executeCode` yields a `text` block plus `stdout`/`stderr`/`exitCode`/`executionTime`; a
  raised exception comes back as `isError: true` carrying the traceback.
- The response is a **stream** whose union is one `result` member plus one member per
  exception type. Anything that is not `result` must be treated as an error, or the drain
  loop waits forever for a result that never arrives.
- Sandbox runtime observed: Python 3.12.13.

## Design

**Lazy activation.** Constructing a session provisions nothing. The tool catalog is static,
so `initialize` and `tools/list` cost nothing and an agent can see and reason about the
tools for free. An AgentCore session starts on the first real `tools/call`.

**One AgentCore session per MCP session.** Required by the state persistence above, not a
stylistic choice. Each MCP session gets its own interpreter, so namespaces and files are
never shared across conversations.

**Session affinity.** The `mcp-server` component registers a selector-based gateway target
with `sessionRouting: Stateful`, so a session is pinned to the pod that owns it and this
server can run more than one replica. That mechanism was added and verified in #49.

**Startup that cannot wedge.** The listener binds *before* AWS initialisation, `/healthz`
is liveness and `/readyz` is readiness, and initialisation retries forever. All three exist
because browser-mcp got each one wrong first: init-before-listen let the liveness probe kill
the pod at ~100s, and giving up at the init budget left the pod alive but permanently
unready. A freshly attached IAM policy took **5m22s** to propagate in one measured case.

**Four teardown paths** release the AgentCore session: client `DELETE`, per-session idle
eviction, a reaper for clients that vanish without `DELETE`, and `SIGTERM`.

**Eviction is task-aware.** This is the one genuinely new mechanism relative to browser-mcp.
`startCommandExecution` runs a command in the background and returns a `taskId`; the task
lives inside the AgentCore session. Idle eviction calls `StopCodeInterpreterSession`, which
would take the sandbox and the task with it, and `getTask` needs the same `sessionId`, so a
task whose session we stopped is unrecoverable. So the session tracks outstanding task ids
and does not arm the idle timer while any exist, clearing them when `getTask` reports a
terminal status (`completed`, `canceled`, `failed`) or `stopTask` succeeds. The AWS-enforced
`sessionTimeoutSeconds` still bounds everything, so a task cannot outlive its session.

## Tools

All nine operations are advertised. Per-application narrowing is available two ways:
`TOOLS_ALLOW`/`TOOLS_DENY` to save agent context, and the gateway `authPolicy` to actually
enforce what a caller may invoke.

| Tool | Notes |
|---|---|
| `executeCode` | python / javascript / typescript, `clearContext` to reset variables |
| `executeCommand` | synchronous shell, the right choice for nearly everything |
| `startCommandExecution` | async shell, returns `taskId`, for jobs lasting minutes |
| `getTask` | poll a task; terminal statuses carry stdout, stderr and exit code |
| `stopTask` | cancel; cancelling a finished task returns an error, which is expected |
| `writeFiles` / `readFiles` / `listFiles` / `removeFiles` | sandbox filesystem |

Schemas are hand-authored, unlike browser-mcp which discovers 29 tools from its child
process. Two pairs overlap and the **descriptions** are where that is resolved, since tool
count is not the problem: `executeCommand` vs `startCommandExecution` take the same input,
and `executeCode` can read and write files itself. Each description therefore says when not
to use the tool, and the dependent tools state their precondition.

## Differences from browser-mcp

Does not need, because there is no child process:

- SigV4 signing by hand. The browser has to sign a WebSocket upgrade; the SDK covers
  everything here.
- `chrome-devtools-mcp` as a per-session child, and with it `tini`, orphan reaping, child
  death detection, and drained child stderr. Node is PID 1 with no children, so the zombie
  accumulation that forced `tini` into the browser image cannot occur.
- The ~80Mi per live session the browser pays for that child. Per-session cost here is
  **unmeasured** and expected to be far smaller, which is why the resource limits are not
  yet set (see RESUME.md).

Also unmeasured: whether `StartCodeInterpreterSession` has a cold-start cliff under
concurrency. The browser's CDP attach did: five simultaneous activations produced one attach
at 4.1s, three at ~61.5s past the client's 60s default, and one failure. `MAX_INTERPRETER_SESSIONS`
defaults to 25 and should be treated as a guardrail rather than a validated throughput
number until that is measured.

## IAM

Unlike the browser, everything scopes cleanly. Simulated against a real interpreter ARN with
`aws iam simulate-custom-policy`:

```
StartCodeInterpreterSession / InvokeCodeInterpreter / StopCodeInterpreterSession /
GetCodeInterpreterSession    -> allowed on code-interpreter-custom/<name>-*
InvokeCodeInterpreter on a DIFFERENT interpreter -> implicitDeny
```

So the policy is:

```
ListCodeInterpreters                                       -> "*"   (collection read)
Get / Start / GetSession / ListSessions / Stop / Invoke     -> arn:...:code-interpreter-custom/<name>-*
```

The resource type is **`code-interpreter-custom`** for anything created via
`CreateCodeInterpreter`, and `code-interpreter` for the AWS built-in
(`arn:aws:bedrock-agentcore:us-west-2:aws:code-interpreter/aws.codeinterpreter.v1`). This
matters because the browser hit exactly this trap: AgentCore's documented example policy
scopes to `browser/*` while a provisioned browser is `browser-custom/...`, so following the
docs produced a policy that denied every call at runtime.

Contrast worth keeping in mind: the browser's `ConnectBrowserAutomationStream`, the action
that actually drives the browser, supports no resource-level permissions at all (eight
candidate ARN forms all denied in IAM's model) and must sit on `*`. The equivalent action
here, `InvokeCodeInterpreter`, scopes to a single interpreter, so this component can be
locked down properly.

## Configuration

Every knob is an environment variable, so a team tunes it through the `mcp-server`
component's `env` list without rebuilding.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCORE_CODE_INTERPRETER_NAME` | *(empty)* | Interpreter name, resolved to an id at boot. Empty uses the AWS built-in, which needs no provisioning. |
| `AGENTCORE_BUILTIN_ID` | `aws.codeinterpreter.v1` | The built-in used when no name is set. |
| `SESSION_TIMEOUT_SECONDS` | `900` | TTL requested per session. AWS enforces it, so it is the only bound if the pod dies hard, and it also bounds async tasks. |
| `SESSION_IDLE_SECONDS` | `300` | Release an idle session. Discards variables **and** files. Suppressed while async tasks are outstanding. |
| `MAX_INTERPRETER_SESSIONS` | `25` | Cap on concurrent live sessions for this pod. Guardrail, not a measured limit. |
| `INTERPRETER_READY_TIMEOUT_SECONDS` | `300` | Budget for one initialisation cycle. |
| `INIT_RETRY_SECONDS` | `15` | Pause between cycles. Initialisation never gives up. |
| `MCP_SESSION_IDLE_SECONDS` | `1800` | Reap an MCP session whose client vanished. |
| `REAPER_INTERVAL_SECONDS` | `60` | Sweep frequency. |
| `TOOLS_ALLOW` / `TOOLS_DENY` | *(empty)* | Narrow the advertised surface. |
| `MCP_PORT` / `MCP_PATH` | `8000` / `/mcp` | Listener. |

`AWS_REGION` is injected by the `aws-service-identity` trait from the cluster's region, so
it never appears in a developer's OAM.

## Verified

Run locally against the live service in `us-west-2` using the built-in interpreter.

`test/e2e-multisession.js` — **E2E PASS**

- 9 tools advertised with zero interpreter sessions at rest
- `tools/list` served while still at zero, so activation really is lazy
- state persisted across calls in one session (`secret=4242`)
- a second session could not see the first session's variable, and both were live at once
- `writeFiles` then `readFiles` returned `file:///e2e.txt: hello from e2e`
- `executeCommand` returned `shell-ok`
- a raised exception arrived as `isError: true` with the traceback, not a transport failure
- teardown released both sessions: `live=0 mcpSessions=0`

`test/task-eviction.js` — **TASK EVICTION PASS**

With `SESSION_IDLE_SECONDS=15` and a 30s background command, the session survived 40s of
total silence with `openAsyncTasks=1`, `getTask` then returned `taskStatus=completed` with
the output `task-survived`, and once terminal the session became evictable and was released.
Server log shows the arc: `idle eviction suppressed` → `Async task finished (completed)` →
`Releasing interpreter (idle)`.

## Dependencies

Pinned to the versions `browser-mcp` already proves in production:
`@aws-sdk/client-bedrock-agentcore` and `-control` at `3.1105.0`,
`@modelcontextprotocol/sdk` at `1.30.0`, `express` at `5.1.0`. No SigV4 packages are needed
here.
