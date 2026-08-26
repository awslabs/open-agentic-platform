// entrypoint.js — the in-VM coder for Flow B P1 (runs on Kata micro-VM start).
//
// This is the UNTRUSTED side of the trust boundary. It holds NO cloud creds and
// NO Kubernetes API access (no SA token). Its only credentials are a Bifrost key
// and a short-TTL GitHub token, both read from projected tmpfs (mode 0400).
// Because it can't talk to the k8s API, it SELF-REPORTS through GitHub — the
// df-run workflow polls GitHub for the PR + the dark-factory/implementation
// commit status this script sets.
//
// Driven entirely by env injected via SandboxClaim.spec.env (contract verified
// against the live operator, envVarsInjectionPolicy=Allowed):
//   DF_REPO           owner/name of the target repo
//   DF_ISSUE_NUMBER   the GitHub issue number (the spec)
//   DF_BRANCH         df/issue-<n>
//   DF_BASE_BRANCH    base to branch from (default main)
//   DF_ISSUE_TITLE    issue title (for the PR title)
//   CODER_PROFILE     claude-code | kiro
//   BIFROST_URL       LLM gateway (from the SandboxTemplate)
//
// Flow: fetch issue → SPEC.md → checkout df/issue-N → coder implements →
// build+test → push → open PR → set commit status success/failure.
const fs = require("fs");
const http = require("http");
const https = require("https");
const { URL } = require("url");
const { execFileSync, spawn } = require("child_process");

const WORKSPACE = process.env.WORKSPACE || "/workspace";
const BIFROST_URL = process.env.BIFROST_URL || "http://bifrost.bifrost.svc.cluster.local:8080";
const REPO = process.env.DF_REPO;
const ISSUE = process.env.DF_ISSUE_NUMBER;
const BRANCH = process.env.DF_BRANCH || `df/issue-${ISSUE}`;
const BASE = process.env.DF_BASE_BRANCH || "main";
const TITLE = process.env.DF_ISSUE_TITLE || `Dark Factory: issue #${ISSUE}`;
// Agentic engine the VM runs. Both engines are first-class and selectable:
//   claude → claude -p (Claude Code, default)   |   kiro → kiro run --headless
// Accept the new CODER_ENGINE and the legacy CODER_PROFILE (claude-code|kiro);
// normalize either to a bare engine id.
const ENGINE = (() => {
  const raw = (process.env.CODER_ENGINE || process.env.CODER_PROFILE || "claude").toLowerCase();
  return raw.startsWith("kiro") ? "kiro" : "claude";
})();
const PROFILE = ENGINE; // back-compat alias used in a few log lines
// AWS DevOps Agent — release-readiness review via the coding-agent plugin, run
// BEFORE the PR opens (docs §6.2). Modes: "claude-plugin" (Claude Code DevOps
// Agent plugin) | "off". On a clear verdict the coder applies DF_DEVOPS_CLEAR_LABEL
// so the hub's Security Agent step runs next (DevOps-first ordering).
const DEVOPS_AGENT_MODE = (process.env.DF_DEVOPS_AGENT_MODE || "off").toLowerCase();
const DEVOPS_CLEAR_LABEL = process.env.DF_DEVOPS_CLEAR_LABEL || "needs-security-review";
const DEVOPS_CLEAR_VERDICTS = (process.env.DF_DEVOPS_CLEAR_VERDICTS || "Safe to Release,Proceed with Caution")
  .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);

const GH_TOKEN_PATH = process.env.GH_TOKEN_PATH || "/etc/secrets/gh-token";
const BIFROST_KEY_PATH = process.env.BIFROST_KEY_PATH || "/etc/secrets/bifrost-api-key";
// LLM observability (traces/cost/tokens) is provided by BIFROST's telemetry, which
// already exports full per-call traces to Langfuse tagged user-agent=dark-factory-coder
// (prompt, response, model, tokens). We deliberately do NOT post a redundant
// coder-side trace here — Bifrost's is richer. See docs/dark-factory §7a.

function readSecret(p) {
  try { return fs.readFileSync(p, "utf8").trim(); } catch { return null; }
}
function sh(cmd, args, opts = {}) {
  return execFileSync(cmd, args, {
    cwd: opts.cwd || WORKSPACE, encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"], env: opts.env || process.env,
    maxBuffer: 64 * 1024 * 1024,
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Minimal GitHub REST helper (self-report bus — no k8s API available).
// GitHub is the completion bus the df-run workflow polls, so a transient network
// blip on a report call (observed: "socket hang up" on the final success POST)
// must NOT be allowed to mark a good run as failed. Retry transient transport
// errors (ECONNRESET / socket hang up) and 5xx with a short backoff.
function ghOnce(method, path, body) {
  const token = readSecret(GH_TOKEN_PATH);
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = https.request(
      { host: "api.github.com", method, path,
        headers: {
          "User-Agent": "dark-factory-coder", Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
          ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}),
        } },
      (res) => {
        let buf = ""; res.on("data", (c) => (buf += c));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(buf ? JSON.parse(buf) : {});
          else { const e = new Error(`GitHub ${method} ${path} → ${res.statusCode}: ${buf}`); e.statusCode = res.statusCode; reject(e); }
        });
      });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

async function gh(method, path, body) {
  let lastErr;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try { return await ghOnce(method, path, body); }
    catch (e) {
      // Retry only transient failures — never a 4xx (bad request / already exists).
      const transient = e.statusCode === undefined || e.statusCode >= 500 || e.statusCode === 429;
      if (!transient || attempt === 4) throw e;
      lastErr = e;
      await sleep(500 * attempt);
    }
  }
  throw lastErr;
}

// Upsert ONE marker-based PR comment (edit in place, no spam) — mirrors the
// hub-side comment.js so the coder's own steps (coding complete, local testing)
// leave a visible mark on the PR just like the review agents do.
async function postStickyComment(prNumber, marker, bodyMd) {
  if (!prNumber) return;
  const full = `<!-- ${marker} -->\n${bodyMd.trim()}`;
  try {
    let existing = null;
    for (let page = 1; page <= 5 && !existing; page++) {
      const cs = await gh("GET", `/repos/${REPO}/issues/${prNumber}/comments?per_page=100&page=${page}`);
      if (!Array.isArray(cs) || !cs.length) break;
      existing = cs.find((c) => (c.body || "").includes(`<!-- ${marker} -->`));
    }
    if (existing) await gh("PATCH", `/repos/${REPO}/issues/comments/${existing.id}`, { body: full });
    else await gh("POST", `/repos/${REPO}/issues/${prNumber}/comments`, { body: full });
    console.log(`[coder] posted PR comment ${marker}`);
  } catch (e) { console.error(`[coder] comment ${marker} non-fatal: ${e.message}`); }
}

async function fetchIssueSpec() {
  const issue = await gh("GET", `/repos/${REPO}/issues/${ISSUE}`);
  let spec = `# ${issue.title}\n\n${issue.body || ""}\n`;
  // No profile/scaffold-hint injection: the issue text states the stack ("a Spring
  // Boot service", "Terraform for an S3 bucket"), the coder generates idiomatic
  // code from that, and build/test is discovered from the resulting marker files.
  // Iterate mode (df-iterate): a human left a change request on the PR. Append it
  // so the coder revises the EXISTING branch to address the feedback, rather than
  // re-implementing from scratch. DF_ITERATE_NOTE is injected by the df-iterate
  // claim; absent on a first (df-run) pass.
  // Prefer the base64 form: the revision note (esp. auto-fed agent findings) is
  // arbitrary markdown with newlines/quotes/braces that CANNOT be injected raw into
  // the SandboxClaim env YAML (it broke the manifest). status.js/df-iterate base64
  // it into DF_ITERATE_NOTE_B64; decode here. Fall back to plain DF_ITERATE_NOTE.
  const note = iterateNote();
  if (note && note.trim()) {
    spec += `\n---\n\n## Revision requested (address this feedback on the existing branch)\n\n${note}\n`;
  }
  return spec;
}

// Resolve the revision note from DF_ITERATE_NOTE_B64 (preferred, safe for arbitrary
// text) or the legacy plain DF_ITERATE_NOTE.
function iterateNote() {
  const b64 = process.env.DF_ITERATE_NOTE_B64;
  if (b64 && b64.trim()) { try { return Buffer.from(b64.trim(), "base64").toString("utf8"); } catch (_) { /* fall through */ } }
  return process.env.DF_ITERATE_NOTE || "";
}

function checkout() {
  const token = readSecret(GH_TOKEN_PATH);
  const url = `https://x-access-token:${token}@github.com/${REPO}.git`;
  const dir = `${WORKSPACE}/repo`;
  const iterating = !!(iterateNote() && iterateNote().trim());
  if (!fs.existsSync(dir)) {
    // On iterate, start from the existing coder branch (build on prior work);
    // otherwise branch fresh from BASE.
    if (iterating) {
      try { sh("git", ["clone", "--depth", "1", "--branch", BRANCH, url, dir]); }
      catch { sh("git", ["clone", "--depth", "1", "--branch", BASE, url, dir]); }
    } else {
      sh("git", ["clone", "--depth", "1", "--branch", BASE, url, dir]);
    }
  }
  sh("git", ["checkout", "-B", BRANCH], { cwd: dir });
  sh("git", ["config", "user.email", "dark-factory@noreply"], { cwd: dir });
  sh("git", ["config", "user.name", "Dark Factory"], { cwd: dir });
  return dir;
}

// Bifrost does User-Agent-prefix routing: any request whose UA starts with
// "claude-cli" is run through a Claude-Code-specific request transform that is
// broken on this build and returns `400 Unexpected field type` — REGARDLESS of
// the body (the identical body + any other UA returns 200; verified by header
// binary-search against the live gateway). We can't patch Bifrost from inside
// the untrusted VM, so we front it with a tiny localhost shim that rewrites the
// UA to a generic value and transparently forwards everything else — including
// SSE streams (Claude Code sends stream:true). Claude Code points at this shim
// via ANTHROPIC_BASE_URL; the shim proxies to the real Bifrost /anthropic route.
//
// The shim MUST run in its OWN process: we launch the coder CLI with the
// synchronous execFileSync (so we can await its exit), which blocks this Node
// event loop for the whole run — an in-process http.Server would never accept a
// connection (observed: ConnectionRefused). So we write the shim to a temp file
// and spawn `node` on it in the background, then wait for its port to open.
const SHIM_PORT = 8791;
function startBifrostUaShim(upstreamBase) {
  const shimSrc = `
const http=require("http"),https=require("https"),{URL}=require("url");
const up=new URL(${JSON.stringify(upstreamBase)});
const agent=up.protocol==="https:"?https:http;
http.createServer((cReq,cRes)=>{
  const headers={...cReq.headers,host:up.host,"user-agent":"dark-factory-coder"};
  const pReq=agent.request({protocol:up.protocol,hostname:up.hostname,port:up.port||(up.protocol==="https:"?443:80),method:cReq.method,path:up.pathname.replace(/\\/+$/,"")+cReq.url,headers},
    pRes=>{cRes.writeHead(pRes.statusCode,pRes.headers);pRes.pipe(cRes);});
  pReq.on("error",e=>{cRes.writeHead(502);cRes.end(String(e.message));});
  cReq.pipe(pReq);
}).listen(${SHIM_PORT},"127.0.0.1",()=>console.log("[ua-shim] listening on ${SHIM_PORT} -> "+up.href));
`;
  const shimPath = "/tmp/ua-shim.js";
  fs.writeFileSync(shimPath, shimSrc);
  const child = spawn("node", [shimPath], { stdio: "inherit", detached: false });
  child.unref();
  // Block until the shim's port is accepting (execFileSync below can't yield).
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try { execFileSync("node", ["-e", `require("net").connect(${SHIM_PORT},"127.0.0.1").on("connect",()=>process.exit(0)).on("error",()=>process.exit(1))`], { stdio: "ignore" }); break; }
    catch { execFileSync("sleep", ["0.2"]); }
  }
  return `http://127.0.0.1:${SHIM_PORT}`;
}

function runCoder(repoDir) {
  // Two LLM transports, selected by USE_BEDROCK:
  //   - Kata (Flow B, default): Bifrost gateway. The Kata VM is credential-less +
  //     in-cluster, so it reaches models through Bifrost's /anthropic route (which
  //     also gives centralized Langfuse observability). CLAUDE_CODE_USE_BEDROCK is
  //     deliberately UNSET here (it would make the CLI use the Bedrock SDK directly
  //     and ignore ANTHROPIC_BASE_URL).
  //   - Lambda MicroVM (Flow D): USE_BEDROCK=1. A MicroVM runs OUTSIDE the cluster
  //     network and can't reach Bifrost's ClusterIP; forcing it back in-cluster
  //     needed a VPC connector + internal NLB. Instead the MicroVM's EXECUTION ROLE
  //     grants bedrock:InvokeModel, so Claude Code calls Bedrock directly over public
  //     egress — no EKS network dependency. Trade-off: these calls bypass Bifrost's
  //     Langfuse telemetry (documented in flow-d-coder-in-microvm-design.md).
  const useBedrock = /^(1|true|yes)$/i.test(process.env.USE_BEDROCK || "");
  const baseEnv = {
    ...process.env,
    // The sandbox runs with readOnlyRootFilesystem, so $HOME (/home/node) is NOT
    // writable. Claude Code writes its config, session state, and — critically —
    // per-invocation SHELL SNAPSHOT files that its Bash tool sources before every
    // command into ~/.claude. If that dir can't be created, every Bash call (npm
    // install/test, git) fails and the agent loops retrying forever (observed:
    // a trivial change ran >15min with no commit). Point HOME + config dir at the
    // writable /tmp tmpfs so the CLI can persist and run shell commands.
    HOME: "/tmp/coder-home",
    CLAUDE_CONFIG_DIR: "/tmp/coder-home/.claude",
    XDG_CONFIG_HOME: "/tmp/coder-home/.config",
    XDG_CACHE_HOME: "/tmp/coder-home/.cache",
    // Non-interactive: never open a browser / prompt for login in headless mode.
    CI: "1",
  };
  let env;
  if (useBedrock) {
    // Bedrock-direct: creds come from the MicroVM execution role (Pod Identity /
    // instance creds); the CLI uses the Bedrock SDK. Model must be a real Bedrock
    // model ID (NOT a Bifrost alias). AWS_REGION comes from the runHookPayload/env.
    env = {
      ...baseEnv,
      CLAUDE_CODE_USE_BEDROCK: "1",
      AWS_REGION: process.env.AWS_REGION || process.env.CODER_REGION || "us-west-2",
      ANTHROPIC_MODEL: process.env.CODER_MODEL || "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      ANTHROPIC_SMALL_FAST_MODEL: process.env.CODER_SMALL_MODEL || process.env.CODER_MODEL || "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    };
    fs.mkdirSync("/tmp/coder-home/.claude", { recursive: true });
    console.log(`[coder] LLM: Bedrock-direct region=${env.AWS_REGION} model=${env.ANTHROPIC_MODEL}`);
  } else {
    // Bifrost is an Anthropic-compatible gateway. Route through the localhost
    // UA-shim so Bifrost doesn't apply its broken claude-cli request transform.
    // Bifrost auth is optional; send a placeholder so the CLI doesn't prompt.
    const key = readSecret(BIFROST_KEY_PATH) || "bifrost";
    const base = startBifrostUaShim(`${BIFROST_URL.replace(/\/+$/, "")}/anthropic`);
    env = {
      ...baseEnv,
      ANTHROPIC_BASE_URL: base,
      ANTHROPIC_API_KEY: key,
      // Bifrost maps model ALIASES → Bedrock model IDs. Claude Code's default
      // model name (e.g. claude-sonnet-4) isn't a Bifrost alias and returns
      // "provided model identifier is invalid" (400). Use the platform's Bifrost
      // alias (verified: 'claude-sonnet' → us.anthropic.claude-sonnet-4-5).
      ANTHROPIC_MODEL: process.env.CODER_MODEL || "claude-sonnet",
      ANTHROPIC_SMALL_FAST_MODEL: process.env.CODER_MODEL || "claude-sonnet",
    };
    fs.mkdirSync("/tmp/coder-home/.claude", { recursive: true });
    delete env.CLAUDE_CODE_USE_BEDROCK;
    console.log(`[coder] LLM: Bifrost base=${base} model=${env.ANTHROPIC_MODEL}`);
  }
  // Inherit stdio so the coder CLI's own output + errors stream into the pod
  // logs (kubectl logs), instead of being swallowed by execFileSync's exception.
  const opts = { cwd: repoDir, env, stdio: "inherit", maxBuffer: 64 * 1024 * 1024 };
  const prompt = `Implement the change described in ${WORKSPACE}/SPEC.md. Build and run unit tests until green. Commit your work.`;
  if (ENGINE === "kiro") {
    // Kiro CLI headless — the coder image carries the `kiro` binary; it reads the
    // same Bifrost/Bedrock env above. --headless drives it non-interactively.
    console.log("[coder] engine=kiro (kiro run --headless)");
    return execFileSync("kiro", ["run", "--headless", "--spec", `${WORKSPACE}/SPEC.md`], opts);
  }
  console.log("[coder] engine=claude (claude -p)");
  return execFileSync(
    "claude",
    ["-p", prompt, "--permission-mode", "bypassPermissions", "--verbose"],
    opts,
  );
}

// AWS DevOps Agent — release-readiness code review, run in-VM via the coding-agent
// plugin BEFORE the PR opens (docs §6.2). Returns { verdict, cleared, summary }.
// This is the REAL managed agent, invoked through the engine's plugin — NOT a
// linter/LLM stand-in. Because the plugin needs a one-time console connect
// (Agent Space + repo), when it isn't wired the review is reported as
// "not-connected" and cleared=false (NEVER a fake pass) so the hub sticky-status
// shows DevOps as not-run and Security is correctly skipped.
function runDevopsReview(repoDir) {
  if (DEVOPS_AGENT_MODE === "off") return { verdict: "skipped", cleared: false, summary: "DevOps Agent disabled" };
  const key = readSecret(BIFROST_KEY_PATH) || "bifrost";
  const base = startBifrostUaShim(`${BIFROST_URL.replace(/\/+$/, "")}/anthropic`);
  const env = {
    ...process.env,
    ANTHROPIC_BASE_URL: base, ANTHROPIC_API_KEY: key,
    ANTHROPIC_MODEL: process.env.CODER_MODEL || "claude-sonnet",
    ANTHROPIC_SMALL_FAST_MODEL: process.env.CODER_MODEL || "claude-sonnet",
    HOME: "/tmp/coder-home", CLAUDE_CONFIG_DIR: "/tmp/coder-home/.claude",
    XDG_CONFIG_HOME: "/tmp/coder-home/.config", XDG_CACHE_HOME: "/tmp/coder-home/.cache", CI: "1",
  };
  delete env.CLAUDE_CODE_USE_BEDROCK;
  const opts = { cwd: repoDir, env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], maxBuffer: 64 * 1024 * 1024, timeout: 15 * 60 * 1000 };
  // Ask the coding agent to invoke the DevOps Agent release-readiness review on
  // the working changes and print the verdict on the last line as DF_DEVOPS_VERDICT=.
  const prompt =
    "Use the AWS DevOps Agent plugin to run a release readiness code review on the current uncommitted+committed changes in this repo " +
    "(cross-repository dependency risks, internal standards compliance, access-control correctness; run static analysis). " +
    "Summarize the findings, then print EXACTLY one final line: DF_DEVOPS_VERDICT=<BLOCK|Proceed with Caution|Safe to Release>.";
  try {
    let out = "";
    if (ENGINE === "kiro") {
      out = execFileSync("kiro", ["run", "--headless", "--prompt", prompt], opts) || "";
    } else {
      out = execFileSync("claude", ["-p", prompt, "--permission-mode", "bypassPermissions"], opts) || "";
    }
    const m = String(out).match(/DF_DEVOPS_VERDICT=\s*(.+)\s*$/im);
    const verdict = m ? m[1].trim() : "unknown";
    const cleared = DEVOPS_CLEAR_VERDICTS.includes(verdict.toLowerCase());
    console.log(`[coder] AWS DevOps Agent verdict: ${verdict} (cleared=${cleared})`);
    return { verdict, cleared, summary: `AWS DevOps Agent: ${verdict}` };
  } catch (e) {
    // Plugin not connected / not installed → report honestly, do NOT fake-pass.
    const msg = (e.stderr || e.stdout || e.message || "").toString();
    const notConnected = /plugin|not installed|not connected|unknown command|No such|command not found/i.test(msg);
    console.error(`[coder] DevOps Agent review unavailable: ${msg.slice(0, 200)}`);
    return { verdict: notConnected ? "not-connected" : "error", cleared: false, summary: "AWS DevOps Agent not connected (one-time console setup required)" };
  }
}

function buildAndTest(repoDir) {
  // Build/test is DISCOVERED from the repo's own marker files — no per-language
  // platform config. Devs control build/test by their repo layout; the toolchains
  // live in the coder IMAGE (see coder/Dockerfile), not here. A repo Makefile with
  // a `test` target is the explicit dev-controlled override, checked first.
  //
  // The project may not be at the repo ROOT (e.g. an infra/ + app/ split), so we
  // probe a few conventional subdirs and run the toolchain in the first that has a
  // recognizable marker. Root wins if present; otherwise app/src/backend/server.
  const CANDIDATES = [".", "app", "src", "backend", "server"];
  const hasIn = (d, f) => fs.existsSync(`${repoDir}/${d}/${f}`.replace(/\/\.\//, "/"));
  const dirOf = (f) => CANDIDATES.find((d) => hasIn(d, f));
  const makeTestDir = () => {
    for (const d of CANDIDATES) {
      try { if (hasIn(d, "Makefile") && /^test:/m.test(fs.readFileSync(`${repoDir}/${d}/Makefile`, "utf8"))) return d; } catch { /* skip */ }
    }
    return null;
  };
  const wd = (d) => `${repoDir}/${d}`.replace(/\/\.$/, "");
  try {
    let d;
    if ((d = makeTestDir()) != null) { console.log(`[coder] Makefile test target in ${d}/`); sh("make", ["test"], { cwd: wd(d) }); }
    else if ((d = dirOf("package.json")) != null) { console.log(`[coder] node project in ${d}/`); sh("npm", ["install", "--no-audit", "--no-fund"], { cwd: wd(d) }); sh("npm", ["test"], { cwd: wd(d) }); }
    else if ((d = dirOf("go.mod")) != null) sh("go", ["test", "./..."], { cwd: wd(d) });
    else if ((d = ["pyproject.toml", "setup.py", "requirements.txt"].map(dirOf).find(Boolean)) != null) sh("python", ["-m", "pytest", "-q"], { cwd: wd(d) });
    else if ((d = dirOf("Cargo.toml")) != null) sh("cargo", ["test"], { cwd: wd(d) });
    else if ((d = dirOf("pom.xml")) != null) sh("mvn", ["-q", "test"], { cwd: wd(d) });
    else if ((d = ["build.gradle", "build.gradle.kts"].map(dirOf).find(Boolean)) != null) sh("./gradlew", ["test"], { cwd: wd(d) });
    else return { green: true, summary: "no recognized test suite — skipped (e.g. infra/config change)" };
    return { green: true, summary: "tests passed" };
  } catch (e) {
    return { green: false, summary: (e.stdout || e.stderr || e.message || "").toString().slice(-400) };
  }
}

async function main() {
  for (const [k, v] of Object.entries({ DF_REPO: REPO, DF_ISSUE_NUMBER: ISSUE })) {
    if (!v) { console.error(`[coder] missing required env ${k}`); process.exit(2); }
  }
  fs.mkdirSync(`${WORKSPACE}/artifacts`, { recursive: true });
  console.log(`[coder] issue #${ISSUE} of ${REPO} → branch ${BRANCH} (profile=${PROFILE})`);

  const spec = await fetchIssueSpec();
  fs.writeFileSync(`${WORKSPACE}/SPEC.md`, spec);
  const repoDir = checkout();

  let headSha = "";
  try {
    runCoder(repoDir);
    const test = buildAndTest(repoDir);
    if (!test.green) throw new Error(`tests not green: ${test.summary}`);

    // Nothing-to-do guard: if the coder produced no commits ahead of the base,
    // there's no diff — GitHub rejects the PR with 422 "No commits between…".
    // This happens when the requested change already exists on base. Report the
    // implementation status as success (the spec is satisfied) and exit cleanly
    // instead of crash-looping. The df-run poller sees success on the base head.
    let ahead = "1"; // default to "has changes" if we can't determine (fail open to PR)
    try {
      execFileSync("git", ["fetch", "--depth", "1", "origin", BASE], { cwd: repoDir, stdio: "ignore" });
      ahead = sh("git", ["rev-list", "--count", `origin/${BASE}..HEAD`], { cwd: repoDir }).trim();
    } catch { /* base unfetchable — proceed to PR, GitHub will validate */ }
    if (ahead === "0") {
      console.log(`[coder] no changes ahead of ${BASE} — the spec appears already satisfied; skipping PR`);
      headSha = sh("git", ["rev-parse", "HEAD"], { cwd: repoDir }).trim();
      await gh("POST", `/repos/${REPO}/statuses/${headSha}`, {
        state: "success", context: "dark-factory/implementation",
        description: "no changes needed — spec already satisfied on base",
      });
      process.exit(0);
    }
    // df/issue-N is bot-owned and single-writer (the df-run workflow holds a
    // per-issue mutex), so a plain --force is safe and correct. --force-with-lease
    // can't be used: the depth-1 clone never fetched origin/df/issue-N, so its
    // lease check fails ("stale info") whenever the branch already exists from a
    // prior run.
    // AWS DevOps Agent release-readiness review runs FIRST (before the PR opens),
    // per the DevOps-first ordering. Its verdict drives the handoff label so the
    // hub's Security Agent step runs only after DevOps clears.
    const devops = runDevopsReview(repoDir);

    sh("git", ["push", "-u", "origin", BRANCH, "--force"], { cwd: repoDir });
    headSha = sh("git", ["rev-parse", "HEAD"], { cwd: repoDir }).trim();

    // Open the PR (idempotent: ignore "already exists"). The coder opens the PR
    // BEFORE the hub-side verify steps run, so it can only mark them "running".
    // The workflow's sticky-status step rewrites the block below (between the
    // dark-factory:status marker) with the real verdicts once holdout/security/
    // devops finish — the "one live sticky status" (README §7). Keep this block's
    // shape in sync with that step.
    // DevOps line reflects how the review is driven:
    //   - mode "off" (default = check-gate): the AWS DevOps Agent GitHub App reviews
    //     the PR and posts its own check — so show "pending (GitHub App)", NOT skipped.
    //   - mode "claude-plugin" (label-gate): the coder drove it → show the verdict
    //     (or "not connected" honestly if the plugin isn't wired).
    // Either way the hub sticky-status step overwrites this block with the live
    // verdict (from the check-run / statuses) once verification completes.
    // NEUTRAL placeholder only. The coder opens the PR BEFORE the hub verify steps
    // + the AWS agents run, so it must NOT print per-step states (they'd be stale
    // guesses that confused readers: "Holdout: running…" long after it finished,
    // "Security: runs after DevOps…" while the bot was already done). The pipeline's
    // ONE consolidated review (status.js → dark-factory:verdict-review) is the
    // authoritative live status. We keep the dark-factory:status marker so status.js
    // can still replace this block with the final verdict summary at the end.
    const prBody = [
      `Closes #${ISSUE}.`,
      "",
      "<!-- dark-factory:status -->",
      "### 🏭 Dark Factory — verification",
      `- ✅ **Build + unit tests (in-VM):** ${test.summary}`,
      "",
      "⏳ **Verification in progress.** Hub gates (holdout, deploy-test) and the real",
      "AWS DevOps + Security agents are reviewing this PR. Results are posted as a",
      "single **consolidated verdict review** on this PR when they finish — that",
      "review (not this body) is the source of truth for merge readiness.",
      "",
      "_Autonomously implemented in a hardware-isolated micro-VM. DevOps + Security reviews are the real AWS Frontier Agents._",
    ].join("\n");
    let prNumber = "";
    try {
      const created = await gh("POST", `/repos/${REPO}/pulls`, {
        title: `Dark Factory: ${TITLE} (#${ISSUE})`, head: BRANCH, base: BASE,
        body: prBody,
        maintainer_can_modify: true,
      });
      prNumber = created && created.number ? String(created.number) : "";
    } catch (e) { if (!/already exists|A pull request already/i.test(e.message)) throw e; }
    // If the PR already existed, look up its number (labels attach to the PR).
    if (!prNumber) {
      try {
        const list = await gh("GET", `/repos/${REPO}/pulls?head=${REPO.split("/")[0]}:${BRANCH}&state=open`);
        if (Array.isArray(list) && list[0]) prNumber = String(list[0].number);
      } catch (_) {}
    }

    // Every step reports on the PR as a comment. The coder posts two: (1) coding
    // complete — what it changed; (2) local testing — the in-VM build+test result.
    // (The review agents post their own comments; the sticky PR-body block is the
    // consolidated board.) Files changed = git name-status vs base.
    let changed = "";
    try { changed = sh("git", ["diff", "--name-status", `origin/${BASE}...HEAD`], { cwd: repoDir }).trim(); } catch { try { changed = sh("git", ["show", "--name-status", "--oneline", "-1", "HEAD"], { cwd: repoDir }).trim(); } catch {} }
    const filesBlock = changed ? "```\n" + changed.slice(0, 1500) + "\n```" : "_(diff summary unavailable)_";
    await postStickyComment(prNumber, "dark-factory:coding",
      `### ✅ 🤖 Coding complete (engine: ${ENGINE})\n\nImplemented the change for issue #${ISSUE} on \`${BRANCH}\` in a hardware-isolated Kata micro-VM.\n\n**Files changed:**\n${filesBlock}`);
    await postStickyComment(prNumber, "dark-factory:local-test",
      `### ${test.green ? "✅" : "❌"} 🧪 Local testing (in-VM, before PR)\n\n**${test.green ? "Build + unit tests passed" : "Tests NOT green"}** — discovered from the repo's own marker files (no central config).\n\n${test.summary ? "```\n" + String(test.summary).slice(0, 800) + "\n```" : ""}`);

    // Post the AWS DevOps Agent verdict as its own commit status, and — when the
    // verdict clears — apply the handoff label so the hub's Security Agent step
    // runs next (DevOps-first ordering). When not connected/BLOCK, we leave the
    // label off (Security stays gated) and report honestly.
    if (DEVOPS_AGENT_MODE !== "off") {
      const dvState = devops.cleared ? "success" : (devops.verdict === "BLOCK" ? "failure" : "error");
      try {
        await gh("POST", `/repos/${REPO}/statuses/${headSha}`, {
          state: dvState, context: "dark-factory/devops",
          description: devops.summary.slice(0, 130),
        });
      } catch (_) {}
      if (devops.cleared && prNumber) {
        // Labels attach to the PR number (the hub devops-gate reads
        // /issues/<pr>/labels). Label the PR, not the issue.
        try {
          await gh("POST", `/repos/${REPO}/issues/${prNumber}/labels`, { labels: [DEVOPS_CLEAR_LABEL] });
          console.log(`[coder] AWS DevOps Agent cleared → applied '${DEVOPS_CLEAR_LABEL}' to PR #${prNumber} (Security Agent will run)`);
        } catch (e) { console.error(`[coder] could not apply handoff label: ${e.message}`); }
      } else if (devops.cleared && !prNumber) {
        console.error("[coder] DevOps cleared but PR number unknown — cannot apply handoff label");
      } else {
        console.log(`[coder] AWS DevOps Agent did NOT clear (${devops.verdict}) → Security Agent stays gated`);
      }
    }

    // Self-report SUCCESS on the head SHA — this is what df-run polls for.
    await gh("POST", `/repos/${REPO}/statuses/${headSha}`, {
      state: "success", context: "dark-factory/implementation",
      description: "implemented, built + tests green",
    });
    console.log(`[coder] done — PR opened, status success on ${headSha}`);
  } catch (e) {
    console.error(`[coder] failed: ${e.message}`);
    if (headSha) {
      try { await gh("POST", `/repos/${REPO}/statuses/${headSha}`, { state: "failure", context: "dark-factory/implementation", description: e.message.slice(0, 130) }); } catch (_) {}
    }
    process.exit(1);
  }
  // Keep the VM alive briefly so logs are collectible; the claim TTL / teardown reaps it.
  process.exit(0);
}

main();
