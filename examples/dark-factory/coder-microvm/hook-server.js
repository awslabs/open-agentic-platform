// Flow D — Lambda MicroVM hook server (the lambda-coder wrapper).
//
// Lambda MicroVM is a snapshot/hook runtime: the platform builds an image by
// starting THIS process and snapshotting it once the `ready` hook says "go", then
// resumes that snapshot per session and calls the `run` hook with the session's
// runHookPayload as the request body. Hooks are HTTP endpoints we serve on :8080.
//
// The dark-factory coder (entrypoint.js) is a ONE-SHOT batch job (clone → agent →
// push → PR, 5-15 min). It cannot run inside the 30s run hook, so /run just
// materializes the payload into the coder's file/env contract and BACKGROUND-SPAWNS
// entrypoint.js, then returns 200 immediately. The coder runs async; df-run's
// await-coder step polls GitHub for the PR (same as Kata). The VM stays alive because
// idlePolicy.maxIdleDurationSeconds > a coder run (idle = no inbound traffic).
//
// LLM: USE_BEDROCK=1 → entrypoint.js calls Bedrock DIRECTLY via the MicroVM execution
// role (no Bifrost / EKS-network dependency). See docs/dark-factory/flow-d-coder-in-microvm-design.md.
//
// KEPT MINIMAL: this is the exact shape that built cleanly (v2.0). The /run handler
// stays trivial and synchronous so the build's ready-hook completes fast. Observability
// is via direct endpoint probes, not a /status route (adding one correlated with a
// hung ready-hook build on the pre-GA controller).

const http = require("http");
const fs = require("fs");
const { spawn } = require("child_process");

const PORT = parseInt(process.env.HOOKS_PORT || "8080", 10);
const SECRETS_DIR = "/tmp/secrets";
// Run-id of the coder invocation currently in flight (or last completed). NOT a plain
// boolean: the VM is SUSPENDED after the first PR and RESUMED for a fix round, and the
// resumed process keeps its in-memory state — a one-shot `coderStarted=true` guard,
// frozen in the snapshot, made the resumed VM ignore the fix round's /run entirely (the
// coder never re-ran; the fix round reported "done" on the old sha). Instead we key on a
// per-invocation run-id (issue + iterate-note hash): a /run whose id differs from the
// one in flight starts a fresh coder (this is a new round after a resume); a /run that
// repeats the current id is a duplicate webhook and is ignored.
let currentRunId = null;
let coderRunning = false;

function runIdOf(d) {
  const note = d.iterateNoteB64 || d.iterateNote || "";
  // Cheap stable hash of issue+note so a fix round (new note) => new id => re-run.
  let h = 0; const s = `${d.issueNumber || ""}:${note}`;
  for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
  return `${d.issueNumber || "?"}#${(h >>> 0).toString(36)}`;
}

function startCoder(payload) {
  let d = {};
  try { d = JSON.parse(payload || "{}"); } catch (e) { console.log("[hook-server] payload not JSON:", e.message); }
  const rid = runIdOf(d);
  if (rid === currentRunId) { console.log(`[hook-server] /run duplicate for ${rid} — ignoring`); return; }
  if (coderRunning) { console.log(`[hook-server] /run for ${rid} but ${currentRunId} still running — ignoring`); return; }
  const isRerun = currentRunId !== null;   // a prior run existed => this is a post-resume fix round
  currentRunId = rid;
  coderRunning = true;
  // Truncate the coder log on each new run. Otherwise the previous round's
  // "done — PR opened on <old sha>" line lingers and the bridge's /logs grep matches it
  // instantly, suspending the VM before the fix-round coder has done anything.
  try { fs.writeFileSync("/tmp/coder.log", ""); } catch {}
  console.log(`[hook-server] /run accepted run-id=${rid}${isRerun ? " (post-resume re-run)" : ""}`);
  fs.mkdirSync(SECRETS_DIR, { recursive: true, mode: 0o700 });
  if (d.ghToken) fs.writeFileSync(`${SECRETS_DIR}/gh-token`, d.ghToken, { mode: 0o400 });
  const env = {
    ...process.env,
    USE_BEDROCK: "1",
    // MicroVM rootfs is read-only + there's no /workspace volume mount (unlike Kata,
    // where the operator mounts a writable workspace). entrypoint.js mkdir's
    // ${WORKSPACE}/artifacts and clones there, so point it at the writable tmpfs —
    // else it crashes EACCES on /workspace/artifacts before doing any work.
    WORKSPACE: "/tmp/workspace",
    GH_TOKEN_PATH: `${SECRETS_DIR}/gh-token`,
    AWS_REGION: d.region || process.env.AWS_REGION || "us-west-2",
    DF_ISSUE_NUMBER: d.issueNumber ? String(d.issueNumber) : "",
    DF_REPO: d.repo || "",
    DF_BRANCH: d.branch || (d.issueNumber ? `df/issue-${d.issueNumber}` : ""),
    DF_BASE_BRANCH: d.baseBranch || "main",
    DF_ISSUE_TITLE: d.issueTitle || "",
  };
  // Fix round (df-iterate): the bridge folds the human's change request into the
  // payload as iterateNoteB64. Without this, a Lambda fix round re-runs the coder
  // with NO instructions → it sees the PR already open and reports "done" on the
  // old sha with zero changes (the Kata path injects DF_ITERATE_NOTE_B64 as claim
  // env; the MicroVM has no claim env, so it must ride in on the runHookPayload).
  if (d.iterateNoteB64) env.DF_ITERATE_NOTE_B64 = d.iterateNoteB64;
  if (d.iterateNote) env.DF_ITERATE_NOTE = d.iterateNote;
  if (d.model) env.CODER_MODEL = d.model;
  console.log(`[hook-server] /run → spawning coder for issue #${env.DF_ISSUE_NUMBER} repo=${env.DF_REPO}`);
  // Capture the coder's stdout+stderr to /tmp/coder.log so /logs can return it —
  // runtime CloudWatch routing doesn't work on this runtime, and there's no shell,
  // so this file (read over the HTTP token) is the ONLY way to see what the coder did.
  const logFd = fs.openSync("/tmp/coder.log", "a");
  const child = spawn("node", ["/app/entrypoint.js"], { env, stdio: ["ignore", logFd, logFd], detached: true });
  child.unref();
  child.on("error", (e) => { coderRunning = false; try { fs.appendFileSync("/tmp/coder.log", "SPAWN-ERROR: " + e.message + "\n"); } catch {} });
  // Clear the in-flight flag when the coder exits so a resumed VM's next /run (fix round)
  // is accepted. `unref`'d + detached, but we still get 'exit' while this process lives.
  child.on("exit", (code) => { coderRunning = false; console.log(`[hook-server] coder run-id=${currentRunId} exited code=${code}`); });
}

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => { body += c; });
  req.on("end", () => {
    const ok = (o) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(o || { status: "ok" })); };
    switch (req.url) {
      case "/ready":     return ok({ status: "ready" });
      case "/validate":  return ok({ status: "valid" });
      case "/run":       startCoder(body); return ok({ status: "started" });
      case "/logs":      { let l=""; try { l=fs.readFileSync("/tmp/coder.log","utf8"); } catch {} return ok({ status:"ok", runId: currentRunId, running: coderRunning, log: l.slice(-6000) }); }
      case "/suspend":   return ok({ status: "suspended" });
      case "/resume":    return ok({ status: "resumed" });
      case "/terminate": return ok({ status: "terminated" });
      default:           return ok({ status: "ok", path: req.url });
    }
  });
});
server.listen(PORT, () => console.log(`[hook-server] listening on :${PORT} (lambda-coder, Bedrock-direct)`));
