# AgentCore Code Interpreter: spike findings

Ran `probe.mjs` against the live service in `us-west-2` on 2026-08-11. Everything below
is measured output, not inference. This exists so the implementation (issue #51) starts
from facts rather than from the browser server's assumptions, which do not all carry over.

## 1. ARN resource type: `code-interpreter-custom`

```
CUSTOM: peeks_hub_agent_core_code_interpreter
  -> arn:aws:bedrock-agentcore:us-west-2:929819487611:code-interpreter-custom/peeks_hub_agent_core_code_interpreter-S8Z5DTeRiG
SYSTEM: AgentCore Code Interpreter
  -> arn:aws:bedrock-agentcore:us-west-2:aws:code-interpreter/aws.codeinterpreter.v1
```

Two different resource types: `code-interpreter-custom` for anything created via
`CreateCodeInterpreter`, and `code-interpreter` for the AWS built-in.

This is the same trap that broke the browser IAM policy. There, the docs' example scoped
to `browser/*` while a provisioned browser is actually `browser-custom/...`, so the
policy denied every call at runtime. **Scope code interpreter IAM to
`code-interpreter-custom/<name>-*`**, and expect `ConnectBrowserAutomationStream`-style
surprises for any action that turns out not to support resource-level permissions.
Verify with `aws iam simulate-custom-policy` against a real ARN before shipping.

## 1b. IAM: unlike the browser, everything scopes cleanly

Simulated against the real interpreter ARN with `aws iam simulate-custom-policy`:

```
StartCodeInterpreterSession   allowed      (on code-interpreter-custom/<name>-*)
InvokeCodeInterpreter         allowed
StopCodeInterpreterSession    allowed
GetCodeInterpreterSession     allowed
InvokeCodeInterpreter on a DIFFERENT interpreter   implicitDeny
```

This is a better posture than the browser, where `ConnectBrowserAutomationStream`, the
action that actually drives the browser, supports no resource-level permissions at all
(eight candidate ARN forms all denied in IAM's model) and therefore has to sit on `*`.
Here the equivalent action, `InvokeCodeInterpreter`, scopes to a single interpreter. So
the policy can be:

```
ListCodeInterpreters                          -> "*"   (collection read)
Get/Start/Get-session/List-sessions/Stop/Invoke -> arn:...:code-interpreter-custom/<name>-*
```

Only discovery needs a wildcard. Confirm at runtime anyway: the browser's docs example
looked right and still denied every call.

## 2. Three custom interpreters already exist

`peeks_hub_...`, `spoke_dev_...` and `spoke_prod_...`, provisioned by the existing
`gitops/addons/charts/crossplane-agentcore` chart (XRD `xagentcorecodeinterpreters`,
composition `agentcore-code-interpreter`, both live and Ready). So the **provisioning
layer already works**. What is missing is the path for an agent to *use* one. Do not
rebuild the Crossplane layer.

## 3. The built-in works with no provisioning

`aws.codeinterpreter.v1` accepted `StartCodeInterpreterSession` directly. Useful for
tests and for a default that costs nothing to set up.

## 4. Tool surface: 9 operations, one request/response API

`InvokeCodeInterpreter(codeInterpreterIdentifier, sessionId, name, arguments)` where
`name` is one of:

```
executeCode  executeCommand  startCommandExecution  getTask  stopTask
readFiles    writeFiles      listFiles              removeFiles
```

`arguments` is a single flat shape shared by all of them:
`code`, `language`, `clearContext`, `command`, `path`, `paths`, `content`,
`directoryPath`, `taskId`, `runtime`.

- `language`: `python` | `javascript` | `typescript`
- `runtime`: `python` | `nodejs` | `deno`

**No CDP and no child process.** This is the significant divergence from browser-mcp,
which had to spawn a `chrome-devtools-mcp` CLI per session because that tool cannot be
imported as a library. Here the AWS SDK is the whole client, which removes at a stroke:
the ~80Mi per-session cost, the telemetry watchdog, the zombie reaping problem that
needed `tini`, and the child-death detection path. Expect far higher session density per
pod, bounded by AWS session quotas rather than pod memory.

The cost of that: browser-mcp got its 29 tool schemas for free from the child's
`tools/list`. Here the 9 schemas must be hand-authored, and kept honest against the
`ToolArguments` shape above.

## 5. Results are nearly MCP-native

`InvokeCodeInterpreter` returns `stream: AsyncIterable<CodeInterpreterStreamOutput>`.
The union is one `result` member plus one member per exception type
(`AccessDenied`, `Conflict`, `InternalServer`, `ResourceNotFound`, `ServiceQuotaExceeded`,
`Throttling`, `Validation`, `$Unknown`), so a drain loop must treat any non-`result`
member as an error rather than waiting for a result that never arrives.

`CodeInterpreterResult` is `{ content: ContentBlock[], structuredContent?, isError? }`,
which lines up with an MCP tool result almost exactly. Observed:

| call | isError | content block types | structuredContent |
|---|---|---|---|
| `executeCode` python | false | `text` | `stdout, stderr, exitCode, executionTime` |
| `executeCommand` | false | `text` | same |
| `writeFiles` | false | `text` ("Successfully wrote all 1 files") | none |
| `listFiles` | false | 9 × `resource_link` | none |
| `readFiles` | false | `resource` | none |
| `executeCode` raising | **true** | `text` (full Python traceback) | same |

So the adapter is thin: pass `content` through, map `resource_link`/`resource` blocks to
MCP's equivalents, and propagate `isError` rather than throwing.

## 6. Session state persists across calls

Set `x = 6 * 7` in one `executeCode`, then a separate call printed `x is still 42`. So a
session is a live interpreter with retained variables and a retained filesystem, exactly
like the browser's page state. **Per-MCP-session isolation is required for the same
reason:** two conversations must not share a namespace or files. The browser server's
one-AgentCore-session-per-MCP-session model carries over directly.

Sandbox runtime observed: Python 3.12.13.

## 7. What carries over from browser-mcp, and what does not

Carries over:
- Lazy activation. Advertise tools at boot, provision nothing until the first real call.
- One AgentCore session per MCP session, with idle eviction, per-session TTL, a reaper
  for clients that vanish, and `SIGTERM` teardown.
- Initialisation that retries forever, because a freshly attached IAM policy takes
  minutes to propagate (measured at 5m22s on a cold browser deploy).
- Selector-based gateway target with `sessionRouting: Stateful`, which `mcp-server` now
  does by default, so this scales past one replica.
- Resource requests and limits sized from measurement, not guessed.

Does not carry over:
- `tini`, zombie reaping, child-death detection: no child processes exist.
- Per-session memory of ~80Mi. Needs its own measurement; expect it to be far smaller.
- The activation stampede seen with concurrent CDP attaches. Whether
  `StartCodeInterpreterSession` has a similar cold-start cliff is **unmeasured** and
  should be checked before advertising a concurrency cap.

## 7b. The async task trio, measured (probe-async.mjs)

| call | observed result |
|---|---|
| `startCommandExecution` | text "Successfully started a command execution task", `structuredContent: {taskId, taskStatus: "submitted", stdout, stderr}` |
| `getTask` while running | text "Task is working", `{taskStatus: "working"}` |
| `getTask` after 25s of **no calls** | `{taskStatus: "completed", stdout: "finished-at-13\r\n", stderr: "", exitCode: 0, executionTime: 20.1}` |
| `stopTask` on a completed task | `isError: true`, "Task cannot be canceled (current status: TaskStatus.COMPLETED)" |
| `stopTask` while running | success, and `getTask` then reports `{taskStatus: "canceled"}` |

Statuses seen: `submitted`, `working`, `completed`, `canceled`. `taskId` is a UUID in
`structuredContent`, not in the text.

**Consequence for session lifecycle.** The task executes server-side and survives idle
gaps: it completed while we made no calls at all. So idleness is not the hazard. The
hazard is our own idle eviction, which calls `StopCodeInterpreterSession` and takes the
sandbox, and therefore the task, with it. `getTask` also needs the same `sessionId`, so
there is no way to recover a task whose session we stopped.

Therefore: **suppress idle eviction while any task is `submitted` or `working`.** Track
task ids handed out by `startCommandExecution`, clear them when `getTask` reports a
terminal status, and keep the idle timer from firing while the set is non-empty. Note the
AWS-enforced `sessionTimeoutSeconds` still bounds everything, so a task cannot outlive
its session regardless; that bound should be documented rather than worked around.

`stopTask` on a finished task returning `isError` is normal service behaviour, not a
failure to handle: pass it through to the caller.

## 8. Open questions before implementation

- `sessionTimeoutSeconds` default and maximum. The browser defaults to 900s; unverified here.
- Account quotas on concurrent code interpreter sessions.
- ~~`startCommandExecution` / `getTask` / `stopTask` not probed.~~ Probed, see 7b. All
  nine tools will be exposed; per-application filtering can happen at the agent or via
  the gateway `authPolicy`.
- `filesystemConfigurations` and `certificates` on session start: not probed, likely how
  you mount shared storage or trust a private CA.
- Whether `sessionId` is genuinely optional on `InvokeCodeInterpreter` (the type says
  yes) and, if omitted, whether the service creates an implicit session that we would
  then leak.

## Reproducing

```bash
cd spikes/agentcore-code-interpreter
ln -sfn ../../applications/browser-mcp/node_modules node_modules   # vendored SDK, no install
eval "$(aws configure export-credentials --format env)"
AWS_REGION=us-west-2 node probe.mjs
```

Override the target with `CODE_INTERPRETER_ID=<id or arn>` to probe a custom interpreter
instead of the built-in.
