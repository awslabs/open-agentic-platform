// status.js — the Dark Factory "one live sticky status" (README §7). Runs in the
// hub-side sticky-status step AFTER every verify step. The coder wrote the PR body
// at PR-open time (before verification ran), so its holdout/security/devops lines
// are placeholders. This reads the authoritative dark-factory/* commit STATUSES
// from GitHub (what the verify steps posted) and rewrites the PR body's marker
// block in place with the real verdicts. Idempotent; non-fatal on error.
//
// Env: GH_TOKEN, REPO (owner/name), BRANCH (df/issue-N).
//   DEVOPS_CHECK (optional) real AWS DevOps Agent check-run name (Checks API), e.g.
//                aws-devops-agent/release-readiness-review. The DevOps row reads it
//                from check-runs (not commit statuses) so the real verdict shows.
//   SECURITY_CHECK (optional) real AWS Security Agent GitHub App check-run name.
//                When present on the PR, the Security row shows the App's inline-bot
//                verdict; otherwise it falls back to our headless dark-factory/security.
const https = require("https");
const { GH_TOKEN, REPO, BRANCH } = process.env;
// Auto-fix loop (status.js submits a bounded df-run revision on a ❌ agent verdict).
const AUTO_FIX = (process.env.AUTO_FIX_FINDINGS || "").toLowerCase() === "true";
const MAX_ITERATIONS = parseInt(process.env.MAX_ITERATIONS || "3", 10);
const ISSUE_NUMBER = process.env.ISSUE_NUMBER || "";
const BASE_BRANCH = process.env.BASE_BRANCH || "main";
const TRIGGER_LABEL = process.env.TRIGGER_LABEL || "dark-factory";
const ARGO_NAMESPACE = process.env.ARGO_NAMESPACE || "argo";
const DEVOPS_CHECK = process.env.DEVOPS_CHECK || "";
const SECURITY_CHECK = process.env.SECURITY_CHECK || "";
// When "true", the pipeline posts ONE consolidated verdict REVIEW summarizing both
// agents' results once verification is terminal (see the block near the end). This
// gives a consistent reviewer signal because the AWS agent Apps review autonomously
// + inconsistently and (confirmed) CANNOT be added via the requested_reviewers API.
const POST_VERDICT_REVIEW = (process.env.POST_VERDICT_REVIEW || "").toLowerCase() === "true";
const H = { "User-Agent": "dark-factory-status", Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };

function api(method, path, body) {
  const data = body ? JSON.stringify(body) : null;
  return new Promise((resolve, reject) => {
    const req = https.request(
      { host: "api.github.com", method, path, headers: { ...H, ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}) } },
      (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => {
        if (r.statusCode >= 200 && r.statusCode < 300) resolve(b ? JSON.parse(b) : {});
        else reject(new Error(`${method} ${path} -> ${r.statusCode}: ${b.slice(0, 150)}`));
      }); });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

const MARKER = "<!-- dark-factory:status -->";
const icon = (s) => (s === "success" ? "✅" : s === "failure" || s === "error" ? "❌" : s === "pending" ? "⏳" : "⬜");

async function main() {
  const owner = REPO.split("/")[0];
  const prs = await api("GET", `/repos/${REPO}/pulls?head=${owner}:${BRANCH}&state=open`);
  if (!prs.length) { console.log("[df-run] no open PR — nothing to update"); return; }
  const pr = prs[0];
  const concToState = (c) => ({ success: "success", neutral: "success", skipped: "success",
    failure: "failure", timed_out: "failure", cancelled: "failure", action_required: "failure" }[c] || "pending");
  // MULTI-SHA ROBUSTNESS: the hub verify steps (holdout/security/deploy-test) post
  // their commit statuses on the SHA that was HEAD when they ran. If the coder then
  // pushes another commit (an impl re-post, or an agent re-review moves head),
  // reading only pr.head.sha shows those steps as "not run" even though they passed
  // on an earlier commit. So collect statuses + check-runs across ALL PR commits,
  // oldest -> newest, letting a later commit's verdict override an earlier one.
  const by = {};
  let shas = [pr.head.sha];
  try {
    const commits = await api("GET", `/repos/${REPO}/pulls/${pr.number}/commits?per_page=100`);
    if (Array.isArray(commits) && commits.length) shas = commits.map((c) => c.sha); // oldest -> newest
  } catch (e) { /* fall back to head only */ }
  for (const sha of shas) {
    try {
      const st = await api("GET", `/repos/${REPO}/commits/${sha}/status`);
      // GitHub returns statuses newest-first; take the first (latest) per context on this commit.
      const seen = {};
      for (const s of st.statuses || []) {
        if (seen[s.context]) continue;
        seen[s.context] = 1;
        by[s.context] = { state: s.state, desc: s.description || "", url: s.target_url || "" };
      }
    } catch (e) { /* skip this commit */ }
    try {
      const cr = await api("GET", `/repos/${REPO}/commits/${sha}/check-runs`);
      for (const c of cr.check_runs || []) {
        const state = c.status === "completed" ? concToState(c.conclusion) : "pending";
        by[c.name] = { state, desc: (c.output && c.output.title) || c.conclusion || c.status };
      }
    } catch (e) { /* non-fatal */ }
  }
  const row = (ctx, label) => {
    const s = by[ctx.includes("/") ? ctx : `dark-factory/${ctx}`];
    if (!s) return `- ⬜ **${label}:** _not run_`;
    // A step can report success but be "not applicable" to this change (e.g. the
    // holdout gate when no hidden scenario matches a Terraform-only PR). Render
    // that as a neutral ⬜ n/a, not a green ✅ that would imply it actually ran.
    const na = /not applicable|n\/a/i.test(s.desc || "");
    const mark = na ? "⬜" : icon(s.state);
    return `- ${mark} **${label}:** ${s.desc || s.state}`;
  };

  // ── THE REAL AWS AGENTS ARE THE SOURCE OF TRUTH ──────────────────────────────
  // Both agents run TWICE on a PR and the copies can DISAGREE: the GitHub App bots
  // (aws-security-agent[bot], aws-devops-agent-*[bot]) review the PR directly, while
  // our hub-side steps drive the SAME agents headlessly + post dark-factory/* commit
  // statuses. The headless copy has been observed to miss findings the App bot caught
  // (e.g. a wildcard-ARN IAM policy) — so trusting the headless status produced a
  // FALSE "no findings / LGTM" next to a bot review listing real findings. Fix: the
  // consolidation reads the AGENT BOTS' OWN reviews as authoritative. The headless
  // dark-factory/* statuses are demoted to a fallback ONLY when a bot didn't post.
  //
  // The bots post a formal REVIEW (state COMMENTED) whose body begins with a summary,
  // plus INLINE review comments per finding. They do NOT emit a check-run for findings
  // and never use CHANGES_REQUESTED, so we parse the review body + count inline
  // comments rather than reading a state flag.
  const reviews = (await api("GET", `/repos/${REPO}/pulls/${pr.number}/reviews?per_page=100`).catch(() => [])) || [];
  const prComments = (await api("GET", `/repos/${REPO}/pulls/${pr.number}/comments?per_page=100`).catch(() => [])) || [];
  // ROUND-AWARENESS: only trust a bot review/comment tied to the CURRENT head SHA.
  // On a fix round the coder force-pushes a new commit; a prior round's "1 finding"
  // review still exists on the PR, so reading it would mirror a STALE verdict.
  const headSha = pr.head.sha;
  const forHead = (item) => (item.commit_id ? item.commit_id === headSha : true);
  const latestBotReview = (pred) => (reviews.filter((r) => pred((r.user || {}).login || "") && forHead(r)).slice(-1)[0]) || null;
  const inlineCountBy = (pred) => prComments.filter((c) => pred((c.user || {}).login || "") && forHead(c)).length;
  const isSecBot = (l) => /^aws-security-agent(\[bot\]|-.*\[bot\])?$/i.test(l) || /security-agent/i.test(l) && /\[bot\]/i.test(l);
  const isDevBot = (l) => /aws-devops-agent/i.test(l) && /\[bot\]/i.test(l);

  // Parse an agent bot review body into {state, desc}. A body that reports one or
  // more findings → failure; an explicit "no findings / no issues" → success; a bot
  // that only said it's "reviewing…" (no verdict yet) → pending.
  const parseAgentVerdict = (body, inlineFindings) => {
    const b = (body || "").toLowerCase();
    const m = b.match(/(\d+)\s+(?:medium|high|low|critical|informational)?[- ]?severity?\s*finding/) ||
              b.match(/identified\s+\*{0,2}(\d+)\b[^.]*finding/) || b.match(/\b(\d+)\s+finding/);
    const declaredNum = m ? parseInt(m[1], 10) : null;
    const saysClean = /no (issues identified|findings|security issues)|no issues were|looks good|lgtm/i.test(body || "");
    const stillReviewing = /is reviewing|will post feedback|analysis in progress/i.test(body || "") && declaredNum === null && !saysClean;
    const n = declaredNum !== null ? declaredNum : (inlineFindings > 0 ? inlineFindings : 0);
    if (stillReviewing) return { state: "pending", desc: "review in progress", n: null };
    if (n > 0) return { state: "failure", desc: `${n} finding(s) — changes requested`, n };
    if (saysClean || (declaredNum === 0)) return { state: "success", desc: "no findings", n: 0 };
    // A bot review with a body we couldn't classify + inline comments = treat as findings.
    if (inlineFindings > 0) return { state: "failure", desc: `${inlineFindings} finding(s) — changes requested`, n: inlineFindings };
    return null; // no usable bot signal
  };

  // Security: the dark-factory/security STATUS is authoritative — it's posted by the
  // security-agent step (security-wait.js), which waits for the bot's TERMINAL verdict
  // on THIS commit and encodes findings/clean there. Prefer it, so status.js and the
  // waiter never disagree (both parse the same bot, but the waiter is round/commit-aware
  // and won't resolve on stale/partial inline comments). Fall back to re-parsing the bot
  // review only if the status is somehow absent.
  const secBotReview = latestBotReview(isSecBot);
  const secBotInline = inlineCountBy(isSecBot);
  const secBot = secBotReview ? parseAgentVerdict(secBotReview.body, secBotInline) : null;
  const secResolved =
    (by["dark-factory/security"] ? { state: by["dark-factory/security"].state, desc: by["dark-factory/security"].desc || by["dark-factory/security"].state } : null)
    || secBot
    || (SECURITY_CHECK && by[SECURITY_CHECK] ? { state: by[SECURITY_CHECK].state, desc: by[SECURITY_CHECK].desc || by[SECURITY_CHECK].state } : null);
  const securityRow = secResolved
    ? `- ${icon(secResolved.state)} **Security review (AWS Security Agent):** ${secResolved.desc}`
    : `- ⬜ **Security review (AWS Security Agent):** _not run_`;

  // DevOps: the App bot's release-readiness verdict lives in its commit STATUS/check
  // (change approved / BLOCK / proceed-with-caution) — that IS the real bot. But it
  // also posts inline review comments; if the status says "approved" yet the bot left
  // change-requesting inline comments, surface that (do not silently call it clean).
  const devBotStatus = (DEVOPS_CHECK && by[DEVOPS_CHECK]) ? by[DEVOPS_CHECK] : by["dark-factory/devops"];
  const devInline = inlineCountBy(isDevBot);
  const devBlockedByStatus = devBotStatus && (devBotStatus.state === "failure" || /block|not (safe|ready)|changes? requested/i.test(devBotStatus.desc || ""));
  const devResolved = devBotStatus
    ? { state: devBlockedByStatus ? "failure" : devBotStatus.state, desc: devBotStatus.desc || devBotStatus.state, url: devBotStatus.url || "" }
    : null;
  // Surface the DevOps Agent's full release-readiness report link (target_url) so
  // reviewers can open the assessment, plus a count of its inline comments.
  const devopsRow = devResolved
    ? `- ${icon(devResolved.state)} **DevOps review (AWS DevOps Agent):** ${devResolved.desc}` +
      (devResolved.url ? ` — [view report ↗](${devResolved.url})` : "") +
      (devInline ? ` _(+${devInline} inline comment(s))_` : "")
    : `- ⬜ **DevOps review (AWS DevOps Agent):** _not run_`;

  // Overall (= merge readiness) = worst across the BLOCKING signals only: build +
  // the two real agent bots. Holdout is ADVISORY (holdout.blocking=false) — it's a
  // train/test quality signal, NOT a merge gate — so a red holdout is SHOWN in its
  // row but does NOT flip the verdict to "changes requested" (only a real Security/
  // DevOps agent finding or a build break does). Set HOLDOUT_BLOCKING=true to include
  // it in the gate.
  const HOLDOUT_BLOCKING = (process.env.HOLDOUT_BLOCKING || "").toLowerCase() === "true";
  const overall = (() => {
    const vals = [
      (by["dark-factory/implementation"] || {}).state,
      (HOLDOUT_BLOCKING && by["dark-factory/holdout"] && !/not applicable|n\/a/i.test(by["dark-factory/holdout"].desc || "")) ? by["dark-factory/holdout"].state : undefined,
      secResolved ? secResolved.state : undefined,
      devResolved ? devResolved.state : undefined,
    ].filter((v) => v !== undefined);
    if (vals.includes("failure") || vals.includes("error")) return "failure";
    if (vals.includes("pending")) return "pending";
    return vals.length ? "success" : "pending";
  })();
  const block = [
    MARKER,
    "### 🏭 Dark Factory — verification",
    row("implementation", "Build + unit tests"),
    // Holdout appears ONLY when it actually evaluated something. The scenarios are
    // repo/language-specific (appliesWhen), so a Terraform-only PR has none — in
    // that case the step reports "not applicable"; omit the row entirely (like
    // deploy-test) rather than clutter the board with a not-applicable/​not-run line.
    ...((by["dark-factory/holdout"] && !/not applicable|n\/a/i.test(by["dark-factory/holdout"].desc || ""))
        ? [row("holdout", "Holdout gate")] : []),
    securityRow,
    devopsRow,
    // deploy-test only appears when the PR was deployable; omit the row otherwise.
    ...(by["dark-factory/deploy-test"] ? [row("deploy-test", "Deploy test")] : []),
    "",
    `_Overall: **${overall}**. Autonomously implemented in a hardware-isolated Kata micro-VM; verification ran as independent hub-side steps (see the checks above). Awaiting human review._`,
  ].join("\n");

  let body = pr.body || "";
  if (body.includes(MARKER)) {
    body = body.slice(0, body.indexOf(MARKER)).trimEnd();
    body = (body ? body + "\n\n" : "") + block;
  } else {
    body = body.trimEnd();
    body = (body ? body + "\n\n" : "") + block;
  }
  await api("PATCH", `/repos/${REPO}/pulls/${pr.number}`, { body });
  console.log(`[df-run] PR #${pr.number} body updated — overall=${overall}`);

  // ── Consolidated verdict REVIEW ──────────────────────────────────────────
  // The AWS agent Apps review autonomously and inconsistently (sometimes a formal
  // review that lands in the sidebar, sometimes only an issue comment; and GitHub
  // App bots cannot be added via the requested_reviewers API). So — for a
  // CONSISTENT, always-present reviewer signal — the pipeline posts ONE formal PR
  // review summarizing both agents' verdicts (as the workflow's GitHub identity).
  //
  // Gate: post once the steps the WORKFLOW controls are resolved (implementation +
  // security). We deliberately do NOT wait for `overall` to be non-pending, because
  // the DevOps Agent App reviews ASYNCHRONOUSLY and its check is often still PENDING
  // when sticky-status runs (at workflow end) — and sticky-status runs only ONCE, so
  // gating on it would mean the review never posts. A still-pending DevOps verdict is
  // shown as "in progress" in the review body. Idempotent via a hidden marker.
  const implState = (by["dark-factory/implementation"] || {}).state;
  // Ready once build is done and the Security agent has a verdict (its findings are
  // the strict gate). DevOps may still be async-pending — shown as "in progress".
  const secState = secResolved ? secResolved.state : undefined;
  const readyToReview = implState && implState !== "pending" && (!secState || secState !== "pending");
  if (POST_VERDICT_REVIEW && readyToReview) {
    const RVMARK = "<!-- dark-factory:verdict-review -->";
    // ROUND-AWARENESS: tag each verdict with the head SHA it evaluated. On a fix
    // round the coder force-pushes a NEW commit → new SHA → we post a FRESH verdict
    // (so the PR visibly moves ❌→✅), rather than skipping because a prior-round
    // verdict exists. Idempotent WITHIN a SHA (a re-run on the same commit no-ops).
    const shaTag = `<!-- df-verdict-sha:${pr.head.sha} -->`;
    try {
      const existing = await api("GET", `/repos/${REPO}/pulls/${pr.number}/reviews?per_page=100`);
      const already = (existing || []).some((r) => (r.body || "").includes(shaTag));
      if (already) {
        console.log(`[df-run] verdict review already posted for ${pr.head.sha.slice(0,7)} — skipping`);
      } else {
        const secLine = securityRow.replace(/^- /, "");
        const devLine = devopsRow.replace(/^- /, "");
        const holdoutLine = (by["dark-factory/holdout"] && !/not applicable|n\/a/i.test(by["dark-factory/holdout"].desc || ""))
          ? "\n" + row("holdout", "Holdout gate").replace(/^- /, "") : "";
        // Build a plain-English findings summary from whichever agents flagged issues.
        const flagged = [];
        if (secResolved && secResolved.state === "failure") flagged.push(`Security (${secResolved.desc})`);
        if (devResolved && devResolved.state === "failure") flagged.push(`DevOps (${devResolved.desc})`);
        // When overall is green, note if the advisory holdout is red (it doesn't
        // block the merge, but the wording shouldn't claim "no findings" if a row is ❌).
        const holdoutRed = by["dark-factory/holdout"] && by["dark-factory/holdout"].state === "failure"
          && !/not applicable|n\/a/i.test(by["dark-factory/holdout"].desc || "");
        const verdictLine =
          overall === "failure"
            ? `**Overall: ❌ Changes requested — do NOT merge.** ${flagged.length ? flagged.join(" and ") + " flagged issues" : "One or more checks failed"}. Address the agents' findings (see their inline review comments), push a fix, and the pipeline re-evaluates. This is NOT approved.`
            : overall === "pending"
              ? "**Overall: ⏳ Security cleared; DevOps review still in progress** — final verdict pending the DevOps release-readiness review. Not yet approved."
              : holdoutRed
                ? "**Overall: ✅ Cleared to merge** — the AWS Security & DevOps agents found no blocking issues (build + both agents green). The holdout gate is below threshold but is ADVISORY (a train/test quality signal, not a merge gate) — review it before merging. Human approval still required."
                : "**Overall: ✅ All checks green** — Build, Holdout, Security, and DevOps agents all cleared with no findings. Looks good to merge (human approval still required).";
        const reviewBody = [
          RVMARK,
          shaTag,
          "### 🏭 Dark Factory — consolidated agent verdict",
          "",
          row("implementation", "Build + unit tests").replace(/^- /, ""),
          holdoutLine ? holdoutLine.trim() : null,
          secLine,
          devLine,
          "",
          verdictLine,
          "",
          "_The AWS Security & DevOps agents' own reviews are the source of truth; this consolidated review reads their verdicts (findings block the merge) so both are always visible in one place. Posted by the Dark Factory pipeline as a COMMENT — a human still owns the merge decision._",
        ].filter((x) => x !== null).join("\n");
        // Event: REQUEST_CHANGES when an agent flagged findings (so the PR visibly
        // shows changes-requested, not a bland comment); COMMENT otherwise. Never
        // APPROVE — the human owns merge approval. If REQUEST_CHANGES is rejected
        // (e.g. can't request changes on own PR in some setups), fall back to COMMENT.
        const event = overall === "failure" ? "REQUEST_CHANGES" : "COMMENT";
        try {
          await api("POST", `/repos/${REPO}/pulls/${pr.number}/reviews`, { event, body: reviewBody });
        } catch (e) {
          await api("POST", `/repos/${REPO}/pulls/${pr.number}/reviews`, { event: "COMMENT", body: reviewBody });
        }
        console.log(`[df-run] posted consolidated verdict review (event=${event}, overall=${overall})`);

        // ── AUTO-FIX loop ──────────────────────────────────────────────────────
        // On a ❌ verdict from the real agents, feed their findings straight back to
        // the coder (no human paraphrasing): submit a bounded df-run revision with
        // iterate-note = the collected Security + DevOps findings. The human only
        // approves at the end. Bounded by MAX_ITERATIONS via a df-iterations/<n> label.
        if (overall === "failure" && AUTO_FIX && (secResolved && secResolved.state === "failure" || devResolved && devResolved.state === "failure")) {
          await maybeAutoFix(pr, { secBotReview, secBotInline, devResolved, comments: prComments, reviews });
        }
      }
    } catch (e) {
      console.log(`[df-run] verdict review skipped: ${e.message.slice(0, 140)}`);
    }
  }
}

// Collect the agents' findings into a plain-text fix instruction, enforce the
// iteration cap, and submit a df-run revision via the in-cluster k8s API.
async function maybeAutoFix(pr, ctx) {
  try {
    const ITER = "df-iterations/";
    const issue = await api("GET", `/repos/${REPO}/issues/${pr.number}`).catch(() => ({}));
    const labels = ((issue && issue.labels) || []).map((l) => (typeof l === "string" ? l : l.name));
    const cur = labels.filter((l) => l.startsWith(ITER)).map((l) => parseInt(l.slice(ITER.length), 10)).filter((n) => !isNaN(n));
    const count = cur.length ? Math.max(...cur) : 0;
    if (count >= MAX_ITERATIONS) {
      console.log(`[df-run] auto-fix cap reached (${count}/${MAX_ITERATIONS}) — leaving for a human`);
      await api("POST", `/repos/${REPO}/issues/${pr.number}/comments`, { body: `<!-- dark-factory:autofix -->\n🏭 Dark Factory: auto-fix cap reached (${count}/${MAX_ITERATIONS}). The agents still report findings — a human should resolve or push a fix.` }).catch(() => {});
      return;
    }
    const next = count + 1;

    // Gather the findings text: the Security bot's review summary + both agents'
    // inline review comments (path:line — what is the issue), truncated for the note.
    const findingLines = [];
    const secRv = ctx.secBotReview;
    if (secRv && secRv.body) findingLines.push(`SECURITY AGENT:\n${secRv.body.trim().slice(0, 1500)}`);
    const inlineFor = (pred, label) => {
      const items = (ctx.comments || []).filter((c) => pred((c.user || {}).login));
      if (!items.length) return;
      findingLines.push(`${label} inline findings:`);
      for (const c of items.slice(0, 8)) findingLines.push(`- ${c.path}${c.line ? `:${c.line}` : ""} — ${(c.body || "").replace(/\s+/g, " ").trim().slice(0, 240)}`);
    };
    inlineFor((l) => /aws-security-agent/i.test(l) && /\[bot\]/i.test(l), "Security");
    inlineFor((l) => /aws-devops-agent/i.test(l) && /\[bot\]/i.test(l), "DevOps");
    if (ctx.devResolved && ctx.devResolved.state === "failure") findingLines.push(`DEVOPS AGENT: ${ctx.devResolved.desc}${ctx.devResolved.url ? ` (report: ${ctx.devResolved.url})` : ""}`);
    const note = [
      "The AWS Security/DevOps agents requested changes on your PR. Address ALL of the findings below, then rebuild + re-run tests. Do not introduce new issues.",
      "",
      ...findingLines,
    ].join("\n").slice(0, 8000);

    // Bump the counter label.
    for (const l of cur) await api("DELETE", `/repos/${REPO}/issues/${pr.number}/labels/${encodeURIComponent(ITER + l)}`).catch(() => {});
    await api("POST", `/repos/${REPO}/issues/${pr.number}/labels`, { labels: [`${ITER}${next}`] }).catch(() => {});

    const wf = {
      apiVersion: "argoproj.io/v1alpha1", kind: "Workflow",
      metadata: { name: `df-run-${ISSUE_NUMBER}-fix${next}`, namespace: ARGO_NAMESPACE },
      spec: {
        workflowTemplateRef: { name: "df-run" },
        arguments: { parameters: [
          { name: "issue-id", value: `${ISSUE_NUMBER}` },
          { name: "issue-number", value: `${ISSUE_NUMBER}` },
          { name: "repo", value: REPO },
          { name: "issue-title", value: pr.title || "" },
          { name: "issue-body", value: "" },
          { name: "base-branch", value: BASE_BRANCH || "main" },
          // Pass the findings as BASE64 (iterate-note-b64), NOT raw iterate-note —
          // the findings are multi-line markdown with quotes/braces that break the
          // claim-sandbox manifest YAML when injected raw (observed: df-run-84-fix1
          // 'manifest must be a valid yaml'). The coder decodes it.
          { name: "iterate-note", value: "" },
          { name: "iterate-note-b64", value: Buffer.from(note, "utf8").toString("base64") },
          { name: "trigger-label", value: TRIGGER_LABEL || "dark-factory" },
        ] },
      },
    };
    await submitWorkflow(wf);
    console.log(`[df-run] AUTO-FIX submitted df-run-${ISSUE_NUMBER}-fix${next} (round ${next}/${MAX_ITERATIONS})`);
    await api("POST", `/repos/${REPO}/issues/${pr.number}/comments`, { body: `<!-- dark-factory:autofix -->\n🏭 Dark Factory — **auto-fix round ${next}/${MAX_ITERATIONS}**: the coder is revising \`${BRANCH}\` to address the agents' findings above. A new verdict will be posted when the reviews re-run.` }).catch(() => {});
  } catch (e) {
    if (e && e.statusCode === 409) { console.log("[df-run] auto-fix already in flight (dedup) — no-op"); return; }
    console.log(`[df-run] auto-fix skipped (non-fatal): ${(e && e.message || e).toString().slice(0, 160)}`);
  }
}

// Submit a Workflow to the in-cluster k8s API using the pod SA token (same as iterate.js).
function submitWorkflow(wf) {
  const fs = require("fs");
  const token = fs.readFileSync("/var/run/secrets/kubernetes.io/serviceaccount/token", "utf8");
  const ca = fs.readFileSync("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt");
  const body = JSON.stringify(wf);
  return new Promise((resolve, reject) => {
    const req = https.request({
      host: "kubernetes.default.svc", method: "POST",
      path: `/apis/argoproj.io/v1alpha1/namespaces/${ARGO_NAMESPACE}/workflows`,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      ca,
    }, (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => {
      if (r.statusCode >= 200 && r.statusCode < 300) resolve(JSON.parse(b));
      else reject(Object.assign(new Error(`k8s submit -> ${r.statusCode}: ${b.slice(0, 200)}`), { statusCode: r.statusCode }));
    }); });
    req.on("error", reject); req.write(body); req.end();
  });
}

main().catch((e) => { console.error(`[df-run] status update failed (non-fatal): ${e.message}`); process.exit(0); });
