# code-interpreter-mcp

Amazon Bedrock AgentCore Code Interpreter exposed to agents as an **MCP server behind
agentgateway**, so code execution is consumed like any other MCP tool and is opted into per
application.

```
agent --(JWT via agentgateway)--> [code-interpreter-mcp] --(AWS SDK)--> AgentCore Code Interpreter
```

Tracked by issue #51.

## Why an MCP server rather than tools in the agent image

PR #43 compiled AgentCore tooling into `strands-agent-base`, so every agent inherited the AWS
SDK, AWS credentials and those tools whether it used them or not. Fronting the service with an
MCP server instead keeps agents generic, makes code execution opt-in per application, and puts
the tool surface under gateway policy.

The consumer side needs no image change. An agent picks this up with configuration only:

```yaml
mcpServers:
  - name: code-interpreter-mcp
traits:
  - type: gateway-identity
```

`MCP_SERVER_NAMES` becomes `<gateway>/mcp/code-interpreter-mcp`, and the agent authenticates
with the projected ServiceAccount token the `gateway-identity` trait mounts. That path is
established on this platform but has **not yet been exercised against this server**; see
Remaining verification.

## What the service actually is

Established by probing the live service. Raw output is in
`spikes/agentcore-code-interpreter/FINDINGS.md`.

- A **session is a live interpreter**. Variables persist between calls and so does its
  filesystem. Setting `x = 6 * 7` in one call and printing it in the next returned
  `x is still 42`. Two conversations must therefore never share a session.
- One API does everything: `InvokeCodeInterpreter(codeInterpreterIdentifier, sessionId, name,
  arguments)`, where `name` selects one of nine operations and `arguments` is a single flat
  shape (`code`, `language`, `clearContext`, `command`, `path`, `paths`, `content`,
  `directoryPath`, `taskId`, `runtime`).
- Results are close to MCP-native: `{ content: ContentBlock[], structuredContent, isError }`.
  `executeCode` yields a `text` block plus `stdout`/`stderr`/`exitCode`/`executionTime`, and a
  raised exception comes back as `isError: true` carrying the traceback.
- The response is a **stream** whose union is one `result` member plus one member per exception
  type. Anything that is not `result` must be treated as an error, or the drain loop waits
  forever for a result that never arrives.
- Sandbox runtime observed: Python 3.12.13.
- A custom interpreter's ARN uses the resource type `code-interpreter-custom`:
  `arn:aws:bedrock-agentcore:us-west-2:<acct>:code-interpreter-custom/<name>-<suffix>`. The AWS
  built-in is a different type and partition-owned:
  `arn:aws:bedrock-agentcore:us-west-2:aws:code-interpreter/aws.codeinterpreter.v1`.
- AWS appends a suffix to the requested name. Asking for
  `peeks_hub_agent_core_code_interpreter` produced
  `peeks_hub_agent_core_code_interpreter-S8Z5DTeRiG`, so a name is not an id and the server
  resolves one to the other at boot.

## Design

**Lazy activation.** Constructing a session provisions nothing. The tool catalog is static, so
`initialize` and `tools/list` cost nothing and an agent can see and reason about the tools for
free. An AgentCore session starts on the first real `tools/call`.

**One AgentCore session per MCP session.** Required by the state persistence above, not a
stylistic choice. Each MCP session gets its own interpreter, so namespaces and files are never
shared across conversations.

**Session affinity.** The `mcp-server` component registers a selector-based gateway target with
`sessionRouting: Stateful`, so the gateway pins each MCP session to the pod that owns it and
this server can run more than one replica. Session state is pod-local, so without that pinning
a second replica would break every session on its second call.

**Startup that cannot wedge.** Three properties, each of which prevents a specific failure:

- The listener binds *before* AWS initialisation, so `/healthz` answers immediately. Otherwise
  the liveness probe kills the pod mid-cold-start.
- `/healthz` is liveness and `/readyz` is readiness. Liveness reports only that the process
  serves HTTP; readiness gates traffic until the interpreter is resolved.
- Initialisation retries forever. Giving up at a budget leaves a pod that is alive but
  permanently unready, which requires a human to notice. Retrying converges whenever the
  dependency appears, and it needs to: a freshly attached IAM policy took **5m22s** to
  propagate in one measured case.

**Four teardown paths** release the AgentCore session: client `DELETE`, per-session idle
eviction, a reaper for clients that vanish without `DELETE`, and `SIGTERM`.

**Eviction is task-aware.** `startCommandExecution` runs a command in the background and
returns a `taskId`, and that task lives inside the AgentCore session. Idle eviction calls
`StopCodeInterpreterSession`, which would take the sandbox and the task with it, and `getTask`
needs the same `sessionId`, so a task whose session was stopped is unrecoverable. Sessions
therefore track outstanding task ids and do not arm the idle timer while any exist, clearing
them when `getTask` reports a terminal status (`completed`, `canceled`, `failed`) or `stopTask`
succeeds. The AWS-enforced `sessionTimeoutSeconds` still bounds everything, so a task cannot
outlive its session.

**No child process.** The AWS SDK is the whole client, so the server is a single Node process.
That is why `tini` is absent and Node is PID 1: there are no grandchildren to orphan and no
zombies to reap. Add `tini` the moment a child process appears here.

## Tools

All nine operations are advertised. Per-application narrowing is available two ways:
`TOOLS_ALLOW`/`TOOLS_DENY` to save agent context, and the gateway `authPolicy` to enforce what
a caller may actually invoke.

| Tool | Notes |
|---|---|
| `executeCode` | python / javascript / typescript, `clearContext` to reset variables |
| `executeCommand` | synchronous shell, the right choice for nearly everything |
| `startCommandExecution` | async shell, returns `taskId`, for jobs lasting minutes |
| `getTask` | poll a task; terminal statuses carry stdout, stderr and exit code |
| `stopTask` | cancel; cancelling a finished task returns an error, which is expected |
| `writeFiles` / `readFiles` / `listFiles` / `removeFiles` | sandbox filesystem |

Schemas are hand-authored against the flat `ToolArguments` shape. Tool *count* is not the
problem to solve here; overlap is. Two pairs collide: `executeCommand` and
`startCommandExecution` take identical input and differ only in whether they block, and
`executeCode` can read and write files itself, duplicating the file tools. So each description
says when **not** to use that tool, and the dependent tools state their precondition
(`getTask` and `stopTask` both require a `taskId` from `startCommandExecution`). The
descriptions are the disambiguation mechanism, and they are the part to edit if a model picks
the wrong tool.

## Provisioning

`agentcore-code-interpreter` is the OAM component that provisions the interpreter, defined in
CUE at `platform/oam/definitions/components/agentcore-code-interpreter.cue`. It emits the
Crossplane `CodeInterpreter` managed resource plus the scoped IAM policy that
`aws-service-identity`'s `accessFor` attaches by naming convention.

The component is optional. Leaving `AGENTCORE_CODE_INTERPRETER_NAME` unset uses the AWS
built-in, which needs no provisioning at all and is the fastest way to try the server.

Health is gated on `status.atProvider.codeInterpreterId != ""`, so a `dependsOn` from the MCP
server waits for a real interpreter id rather than for the resource object to merely exist.
`networkMode: PUBLIC` is verified working; the CRD does not enumerate the allowed values, so
the parameter is left open rather than guessed.

**The interpreter name must be unique in the account, and a collision does not self-heal.** A
create against a taken name fails with

```
ConflictException: CodeInterpreter with name '<name>' already exists in this account
```

and the provider then retries that same create indefinitely, leaving the resource
`Synced=False` with an empty `atProvider` and therefore permanently unhealthy. This is not
hypothetical. The interpreter provisioned by the `crossplane-agentcore` chart on this hub has
been in exactly that state since 2026-05-27, even though the interpreter itself exists in AWS
and reports `status: READY`. A sibling resource on the same chart shows the clearer symptom,
`cannot determine creation result - remove the crossplane.io/external-create-pending
annotation`, which is the signature of a create whose result was lost between the AWS call and
the state write. Once that happens the name is taken and the resource can never converge.

Two consequences. The default name is `<namespace>_<component>`, unique per namespace rather
than per cluster. And retry-forever initialisation is what makes a late-arriving interpreter
recoverable without a pod restart.

## IAM

The policy grants exactly the four APIs `src/agentcore.js` constructs commands for:
`ListCodeInterpreters` to resolve a name to an id, then `StartCodeInterpreterSession`,
`InvokeCodeInterpreter` and `StopCodeInterpreterSession`. `GetCodeInterpreter`,
`GetCodeInterpreterSession` and `ListCodeInterpreterSessions` are deliberately absent because
nothing calls them. `CreateCodeInterpreter` and `DeleteCodeInterpreter` are absent because
Crossplane provisions under its own credentials.

Everything except the collection read scopes to a single interpreter. Simulated with
`aws iam simulate-custom-policy` against the real ARN:

```
ListCodeInterpreters          on *              -> allowed
StartCodeInterpreterSession   on the real ARN   -> allowed
StopCodeInterpreterSession    on the real ARN   -> allowed
InvokeCodeInterpreter         on the real ARN   -> allowed

InvokeCodeInterpreter   on a DIFFERENT interpreter -> implicitDeny
CreateCodeInterpreter                              -> implicitDeny
DeleteCodeInterpreter                              -> implicitDeny
```

So:

```
ListCodeInterpreters              -> "*"   (collection read, not authorized per interpreter)
Start / Stop / Invoke session     -> arn:...:code-interpreter-custom/<name>-*
```

Two details that are easy to get wrong. The resource type must be `code-interpreter-custom`;
AgentCore's documented example policies use the non-custom type, which never matches a
provisioned resource and produces a policy that denies every call at runtime while looking
correct. And the trailing `-*` is required because of the name suffix AWS appends.

The account id comes from platform config (the cluster secret's `aws_account_id` annotation),
never from developer OAM, and falls back to `*` so the ARN stays valid if the global is unset.

## Measurements

From the published image under podman on arm64, against the live service in `us-west-2`. The
example's resource sizing derives from these.

**Per-session memory is not measurable.** Node's RSS inside the container was 83MB with zero
sessions, 78MB with eight live sessions (sampled twice, 20s apart), and 79MB after teardown.
The variation between states is smaller than the variation between samples of the same state,
so the honest conclusion is that a session costs less than the measurement noise, not that it
costs nothing. This follows from there being no per-session child process: a session is a
small JS object plus HTTP state.

`podman stats` was discarded as a source. It reported 43.61MB idle and 34.96MB with eight
sessions, which is not internally consistent.

The practical consequence is that pod memory does not bound session count, so
`MAX_INTERPRETER_SESSIONS` is a guardrail against the AWS account session quota, and the memory
limit is sized for request payloads (8mb JSON bodies, and `readFiles` can return large content)
rather than for concurrency. A sizing rule of the form
`limit >= baseline + per-session × max sessions` does not apply here and would badly
over-provision.

**No cold-start cliff.** Eight simultaneous activations, fired with `Promise.all`, each
completed in 1.3s to 1.4s with zero failures. An earlier sequential run produced 1.2s to 1.7s,
but sequential numbers say nothing about a stampede, so the parallel run is the one that
answers the question.

## Configuration

Every knob is an environment variable, so a team tunes it through the `mcp-server` component's
`env` list without rebuilding.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCORE_CODE_INTERPRETER_NAME` | *(empty)* | Interpreter name, resolved to an id at boot. Empty uses the AWS built-in. |
| `AGENTCORE_BUILTIN_ID` | `aws.codeinterpreter.v1` | The built-in used when no name is set. |
| `SESSION_TIMEOUT_SECONDS` | `900` | TTL requested per session. AWS enforces it, so it is the only bound if the pod dies hard, and it also bounds async tasks. |
| `SESSION_IDLE_SECONDS` | `300` | Release an idle session. Discards variables **and** files. Suppressed while async tasks are outstanding. |
| `MAX_INTERPRETER_SESSIONS` | `25` | Cap on concurrent live sessions for this pod. |
| `INTERPRETER_READY_TIMEOUT_SECONDS` | `300` | Budget for one initialisation cycle. |
| `INIT_RETRY_SECONDS` | `15` | Pause between cycles. Initialisation never gives up. |
| `MCP_SESSION_IDLE_SECONDS` | `1800` | Reap an MCP session whose client vanished. |
| `REAPER_INTERVAL_SECONDS` | `60` | Sweep frequency. |
| `TOOLS_ALLOW` / `TOOLS_DENY` | *(empty)* | Narrow the advertised surface. |
| `MCP_PORT` / `MCP_PATH` | `8000` / `/mcp` | Listener. |

`AWS_REGION` is injected by the `aws-service-identity` trait from the cluster's region, so it
never appears in a developer's OAM.

## Verified

Against the live service in `us-west-2` using the built-in interpreter, with the final run
against the published image.

`test/e2e-multisession.js`: **E2E PASS**

- 9 tools advertised with zero interpreter sessions at rest
- `tools/list` served while still at zero, so activation really is lazy
- state persisted across calls in one session (`secret=4242`)
- a second session could not see the first session's variable, and both were live at once
- `writeFiles` then `readFiles` returned `file:///e2e.txt: hello from e2e`
- `executeCommand` returned `shell-ok`
- a raised exception arrived as `isError: true` with the traceback, not a transport failure
- teardown released both sessions: `live=0 mcpSessions=0`

`test/task-eviction.js`: **TASK EVICTION PASS**

With `SESSION_IDLE_SECONDS=15` and a 30s background command, the session survived 40s of total
silence with `openAsyncTasks=1`, `getTask` then returned `taskStatus=completed` with the output
`task-survived`, and once terminal the session became evictable and was released. The server
log shows the arc: `idle eviction suppressed` → `Async task finished (completed)` →
`Releasing interpreter (idle)`.

**Component and example**: `generate.sh` produced only the intended definition; rendering with
`--set global.awsRegion=eu-west-1 --set global.awsAccountId=111122223333` showed both values
flowing with no leftover placeholders; `kubectl apply --dry-run=server` accepted the result.

**Image**: `public.ecr.aws/z0a4o2j5/code-interpreter-mcp:0.1.0`, an OCI index with
`linux/amd64` `sha256:3cc38c69ade2b46c402f2d43616297156fb11abbf1e41eabd6d45f0cab7c8e29` and
`linux/arm64` `sha256:4bfd16cb2762c3bb764c1aedb34f0d43dde865cd49f748b5a4848f03d5da1cd2`. 170MB.
Pulled fresh, served `/readyz` with 9 tools, PID 1 is `node`.

## Verified on the cluster

Deployed to `peeks-hub` from `platform/oam/examples/example-code-interpreter-mcp.yaml`, using
a provisioned custom interpreter rather than the built-in.

- **Provisioning**: the `CodeInterpreter` MR reached `Synced=True Ready=True` with
  `codeInterpreterId=agents_code_sandbox-oNLZikqPdJ`, and the IAM policy MR came up alongside
  it.
- **`dependsOn` gating works**: interpreter `Ready` at 14:14:34, MCP pod created at 14:14:35.
- **The retry loop earned its keep.** The pod logged 13 consecutive `ListCodeInterpreters`
  `AccessDenied` failures over 65 seconds while the freshly created IAM policy propagated,
  then resolved the interpreter and went ready, with **0 restarts** and `initSeconds=66`. This
  is the first time that path was exercised for this server; locally the permissions always
  existed already.
- **IAM as enforced by AWS** (read back with `get-policy-version`, not simulated):
  `ListCodeInterpreters` on `*`, and Start/Stop/Invoke session on
  `code-interpreter-custom/agents_code_sandbox-*`.
- **Gateway registration**: `sessionRouting: Stateful` with a selector target matching
  `agentgateway.dev/target: code-interpreter-mcp`, a label present on the stable Service only,
  not on preview.
- `test/e2e-multisession.js`: **E2E PASS** against the cluster pod.
- `test/gateway.js`: **GATEWAY PASS** through agentgateway with a real JWT. Tools visible
  without starting a session, a `tools/call` activating one, session state surviving the proxy
  hop, and a second gateway session isolated from the first.
- `test/tool-matrix.js`: **TOOL MATRIX PASS**, all nine tools called with only their required
  arguments.
- **A real agent used it.** The agent loaded 9 tools from
  `agentgateway-proxy...:8080/mcp/code-interpreter-mcp` and executed code on request, with
  zero tool-call failures in the server log.

### The defect a live agent found

The first agent run *looked* like a success and was not. Asked for the 25th Fibonacci number
and the sum of primes below 1000, the agent returned correct values and a plausible code
listing, while the server log showed:

```
Tool call failed  {tool: executeCode, err: "code and language fields are required in argument."}
```

The service rejects `executeCode` without `language`, but the schema advertised `language` as
optional with a default that nothing implemented. Every earlier test passed it explicitly, so
only a model reading the schema and omitting it triggered the failure. The model then answered
from its own knowledge and *claimed* it had used the sandbox, which is worse than an error,
because a plausible answer hides the fault.

Two changes came out of it:

- `normalizeArgs` in `src/tools.js` applies the default server-side, so a parameter documented
  as optional is now actually optional. Fixed in `0.1.1`.
- `test/tool-matrix.js` calls every advertised tool with only its schema-required arguments,
  and fails if any tool is broken when called the way its own schema permits. It also fails if
  a tool is advertised but untested, so the gap that hid this cannot silently return. Running
  it found that only `executeCode` had the problem: `listFiles` with no `directoryPath` and the
  rest were all fine.

Verification lesson worth keeping: the follow-up test asked the agent for
`sha256("oap-code-interpreter-verification-2026")` and compared it to a locally computed
digest. It matched exactly, which a model cannot fake. Famous numbers like Fibonacci and prime
sums are exactly the wrong probe, because the model already knows them.

## Relationship to browser-mcp

`applications/browser-mcp` fronts AgentCore Browser the same way and shares the platform
mechanisms: the `mcp-server` component, session affinity, lazy activation, the health and
readiness split, and retry-forever startup. Those were built and verified there first, which is
where the 5m22s IAM propagation figure and the liveness-probe failure mode come from.

Three places where its behaviour does **not** transfer, worth knowing if you work on both:

- **No child process here.** The browser spawns a `chrome-devtools-mcp` process per session and
  needs `tini` as PID 1 to reap orphaned grandchildren, plus child-death detection and drained
  child stderr. None of that applies to this server, and its absence is deliberate rather than
  an oversight.
- **Memory sizing is different in kind.** A live browser session costs ~80Mi, so memory bounds
  session count there. Here memory is flat in session count, so copying that sizing rule
  over-provisions and misidentifies the real constraint, which is the AWS session quota.
- **Concurrency behaves differently.** The browser's CDP attach has a cold-start cliff: five
  simultaneous activations gave one attach at 4.1s, three at ~61.5s past the MCP client's 60s
  default, and one that never attached. Eight simultaneous activations here all completed in
  ~1.3s. The cause of the browser's cliff was never identified, so this is a measured contrast
  rather than an explained one.

What does transfer is the IAM lesson. Both services use a `-custom` resource type for
provisioned resources while AWS's own documented example policies use the non-custom type, and
in the browser's case following the documentation produced a policy that denied every call at
runtime.

## Dependencies

`@aws-sdk/client-bedrock-agentcore` and `-control` at `3.1105.0`,
`@modelcontextprotocol/sdk` at `1.30.0`, `express` at `5.1.0`. These match the versions already
running in production in the sibling MCP server, which is why they were chosen. No SigV4
packages are needed: the SDK signs everything.
