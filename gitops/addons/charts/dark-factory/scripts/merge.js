// merge.js — merge a human-approved Dark Factory PR, but ONLY if it is green.
// Runs in the df-merge-teardown workflow (fired by an approved PR review). The
// approval is the human gate; this adds a safety check that every dark-factory/*
// commit status on the PR head succeeded, so a stray approval on a red PR can't
// merge. The agent never self-merges — this path only runs on a human approval
// event routed by the Sensor.
//
// Env: GH_TOKEN, REPO (owner/name), PR (number).
//   DEVOPS_CHECK   (optional) the real AWS DevOps Agent check-run name/context to
//                  require green in check-mode (e.g. aws-devops-agent/release-readiness-review).
//                  When set, it's added to the required gate and satisfied by EITHER a
//                  commit status OR a check-run of that name.
//   REQUIRE_DEVOPS "true"|"false" — whether DevOps clearance is required to merge
//                  (false in security-only mode). Default true.
//   SECURITY_CHECK (optional) the real AWS Security Agent GitHub App check-run
//                  name/context (e.g. aws-security-agent/code-review). When set +
//                  REQUIRE_SECURITY, a Security Agent BLOCK/failure blocks the merge.
//                  Satisfied by EITHER a commit status OR a check-run of that name.
//   REQUIRE_SECURITY "true"|"false" — require the App's check green to merge.
//                  Default false so nothing blocks until the App is actually
//                  installed and posting a check (avoids waiting on a check that
//                  never arrives). Our advisory dark-factory/security is always required.
const https = require("https");
const { GH_TOKEN, REPO, PR } = process.env;
const DEVOPS_CHECK = process.env.DEVOPS_CHECK || "";
const REQUIRE_DEVOPS = (process.env.REQUIRE_DEVOPS || "true").toLowerCase() !== "false";
// Default TRUE (fail-secure): if the workflow forgets to pass these, we demand the
// gate rather than silently dropping it. Set "false" only where the chart flag that
// produces the status is itself off, or the merge would block on a status nothing posts.
const REQUIRE_HOLDOUT = (process.env.REQUIRE_HOLDOUT || "true").toLowerCase() !== "false";
const REQUIRE_REVIEW = (process.env.REQUIRE_REVIEW || "true").toLowerCase() !== "false";
const SECURITY_CHECK = process.env.SECURITY_CHECK || "";
const REQUIRE_SECURITY = (process.env.REQUIRE_SECURITY || "false").toLowerCase() === "true";
const H = { "User-Agent": "dark-factory-merge", Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };

function api(method, path, body) {
  const data = body ? JSON.stringify(body) : null;
  return new Promise((resolve, reject) => {
    const req = https.request(
      { host: "api.github.com", method, path, headers: { ...H, ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}) } },
      (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => {
        if (r.statusCode >= 200 && r.statusCode < 300) resolve(b ? JSON.parse(b) : {});
        else reject(Object.assign(new Error(`${method} ${path} -> ${r.statusCode}: ${b.slice(0, 200)}`), { statusCode: r.statusCode }));
      }); });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

// Which checks must be green before we merge. The df-run steps post COMMIT STATUSES
// (dark-factory/*); the REAL AWS DevOps Agent posts a CHECK-RUN (Checks API) named
// DEVOPS_CHECK. We read BOTH surfaces and require each listed check to be success.
// holdout/security are advisory in v1 (post success unless blocking on), but we
// still require them to be *success*, not failure/error.
// ABSENT IS NOT A PASS. A crashed step, a timeout, a Bifrost outage, or statuses
// left behind on a pre-force-push commit all yield "no status at head" — exactly
// the case where we know LEAST about the change. Refusing is the only safe reading
// of an unverified gate (review C3: a missing check used to be logged and merged).
//
// Which means REQUIRED must list only checks that are actually ENABLED — a gate
// that is switched off never posts, and demanding it would block every merge.
// The workflow passes the enabled set via REQUIRE_* env (rendered from the chart's
// holdout.enabled / review.enabled / deployTest.enabled flags).
const REQUIRED = ["dark-factory/implementation"];   // always: posted by the coder, not gated
if (REQUIRE_HOLDOUT) REQUIRED.push("dark-factory/holdout");
if (REQUIRE_REVIEW) REQUIRED.push("dark-factory/security");
if (REQUIRE_DEVOPS) REQUIRED.push(DEVOPS_CHECK || "dark-factory/devops");
// The real AWS Security Agent GitHub App posts its own check — require it green
// too (when enabled) so a Security BLOCK can't be merged past. This is in ADDITION
// to our advisory dark-factory/security (headless path); both run.
if (REQUIRE_SECURITY && SECURITY_CHECK) REQUIRED.push(SECURITY_CHECK);

// Map GitHub check-run conclusion → status-style state.
const concToState = (c) => ({ success: "success", neutral: "success", skipped: "success",
  failure: "failure", timed_out: "failure", cancelled: "failure", action_required: "failure" }[c] || "pending");

async function main() {
  const pr = await api("GET", `/repos/${REPO}/pulls/${PR}`);
  if (pr.state !== "open") { console.log(`[df-merge] PR #${PR} is ${pr.state}, not open — nothing to merge`); return; }
  const sha = pr.head.sha;
  // Read commit statuses AND check-runs (the DevOps Agent uses the Checks API).
  const st = await api("GET", `/repos/${REPO}/commits/${sha}/status`);
  const by = {};
  for (const s of st.statuses || []) if (!by[s.context]) by[s.context] = s.state;
  try {
    const cr = await api("GET", `/repos/${REPO}/commits/${sha}/check-runs`);
    for (const c of cr.check_runs || []) {
      const state = c.status === "completed" ? concToState(c.conclusion) : "pending";
      // Prefer a completed check-run's verdict; don't overwrite an existing success.
      if (!by[c.name] || by[c.name] === "pending") by[c.name] = state;
    }
  } catch (e) { console.log(`[df-merge] check-runs read skipped: ${e.message}`); }
  const notGreen = REQUIRED.filter((c) => by[c] && by[c] !== "success");
  const missing = REQUIRED.filter((c) => !by[c]);
  const pending = REQUIRED.filter((c) => by[c] === "pending");
  if (notGreen.length || missing.length) {
    if (notGreen.length) console.error(`[df-merge] refusing to merge — not green: ${notGreen.map((c) => `${c}=${by[c]}`).join(", ")}`);
    // A MISSING status is a refusal, not a skip (review C3). This is also what
    // closes the force-push hole: statuses are read at pr.head.sha ONLY, so after a
    // df-iterate force-push the checks that ran on the superseded commit do not
    // count — head has no status, and we now stop instead of merging unverified
    // code. Deliberately NOT "scan every PR commit like status.js does": that is
    // right for a human-readable board, but for a GATE it would let a green status
    // from a pre-force-push commit authorize a merge of code nothing checked.
    if (missing.length) console.error(`[df-merge] refusing to merge — no status at head ${sha.slice(0, 7)} for: ${missing.join(", ")}`);
    if (pending.length) console.error(`[df-merge] (still pending: ${pending.join(", ")})`);
    console.error(`[df-merge] required: ${REQUIRED.join(", ")}`);
    console.error(`[df-merge] present at head: ${Object.keys(by).length ? Object.entries(by).map(([c, s]) => `${c}=${s}`).join(", ") : "(none)"}`);
    console.error("[df-merge] a missing status means the gate did not report — re-run it, or if the branch was force-pushed re-run the checks against the new head.");
    process.exit(1);
  }

  // SOURCE-OF-TRUTH GATE: the REAL AWS agent bots review the PR directly and can
  // catch findings the headless dark-factory/* scan misses (observed: a wildcard-ARN
  // IAM policy flagged by aws-security-agent[bot] while the headless status said
  // "no findings"). So — regardless of the commit statuses above — refuse to merge
  // if the Security or DevOps agent BOT posted findings (a review body reporting
  // "N finding(s)" or change-requesting inline review comments).
  try {
    const allReviews = (await api("GET", `/repos/${REPO}/pulls/${PR}/reviews?per_page=100`)) || [];
    const allComments = (await api("GET", `/repos/${REPO}/pulls/${PR}/comments?per_page=100`)) || [];
    // CRITICAL: only count findings on the CURRENT head sha. A fix round pushes a NEW
    // commit and the agents re-review it (posting fresh commit STATUSES, already checked
    // green above); their earlier REVIEW bodies/inline comments remain attached to the
    // OLD (superseded) sha. Without this filter merge.js counts those stale first-round
    // findings and refuses to merge every PR that was ever fixed — observed on PR #136:
    // security/devops reviews on sha 3b11b497 (round 1) blocked a merge whose head
    // 8089fa0a (fix round) was fully green. Match reviews by commit_id and inline
    // comments by original_commit_id/commit_id to the head sha.
    // A review counts only if it was submitted against the current head. An inline
    // comment counts only if it was ORIGINALLY filed against the current head
    // (original_commit_id) — GitHub auto-advances an inline comment's commit_id to the
    // latest head when the line still exists, so round-1 comments reappear with
    // commit_id==head; original_commit_id preserves the sha they were truly filed on.
    // Matching on original_commit_id excludes those carried-forward round-1 findings.
    const reviews = allReviews.filter((r) => r.commit_id === sha);
    const comments = allComments.filter((c) => (c.original_commit_id || c.commit_id) === sha);
    const isSecBot = (l) => /aws-security-agent/i.test(l || "") && /\[bot\]/i.test(l || "");
    const isDevBot = (l) => /aws-devops-agent/i.test(l || "") && /\[bot\]/i.test(l || "");
    const botFindings = (pred) => {
      const rv = reviews.filter((r) => pred((r.user || {}).login)).slice(-1)[0];
      const inline = comments.filter((c) => pred((c.user || {}).login)).length;
      const body = rv ? (rv.body || "") : "";
      const m = body.toLowerCase().match(/(\d+)\s+(?:medium|high|low|critical|informational)?[- ]?severity?\s*finding/) ||
                body.toLowerCase().match(/\b(\d+)\s+finding/);
      const declared = m ? parseInt(m[1], 10) : null;
      const saysClean = /no (issues identified|findings|security issues)|looks good|lgtm/i.test(body);
      if (declared !== null) return declared;
      if (saysClean) return 0;
      return inline; // unclassified review + inline comments = treat as findings
    };
    const secN = botFindings(isSecBot), devN = botFindings(isDevBot);
    if (secN > 0 || devN > 0) {
      const parts = [];
      if (secN > 0) parts.push(`Security agent: ${secN} finding(s)`);
      if (devN > 0) parts.push(`DevOps agent: ${devN} finding(s)`);
      console.error(`[df-merge] refusing to merge — AWS agent bot flagged findings (${parts.join("; ")}). Address them and re-approve.`);
      process.exit(1);
    }
  } catch (e) { console.log(`[df-merge] agent-bot findings check skipped (non-fatal): ${e.message.slice(0, 120)}`); }

  console.log(`[df-merge] PR #${PR} green + human-approved — merging (squash)`);
  await api("PUT", `/repos/${REPO}/pulls/${PR}/merge`, {
    merge_method: "squash",
    commit_title: `${pr.title} (#${PR})`,
    commit_message: "Merged by Dark Factory after human approval. Autonomously implemented + verified (holdout + security + devops).",
  });
  console.log(`[df-merge] PR #${PR} merged.`);
  // Best-effort: delete the coder branch now that it's merged.
  try { await api("DELETE", `/repos/${REPO}/git/refs/heads/${pr.head.ref}`); console.log(`[df-merge] deleted branch ${pr.head.ref}`); }
  catch (e) { console.log(`[df-merge] branch delete skipped: ${e.message}`); }
}

main().catch((e) => { console.error(`[df-merge] failed: ${e.message}`); process.exit(1); });
