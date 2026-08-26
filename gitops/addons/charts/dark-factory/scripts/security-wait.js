// security-wait.js — WAIT FOR + MIRROR the real AWS Security Agent bot.
//
// The AWS Security Agent GitHub App (aws-security-agent[bot]) auto-reviews every PR
// and is the SOURCE OF TRUTH for security. This step does NOT run a second scan — it
// polls the bot's own review + inline comments, parses its verdict, and posts it as
// the dark-factory/security commit status so the merge gate + consolidated review key
// off the REAL bot (findings -> failure -> merge blocked). Replaces the old headless
// security-agent.sh, which was a redundant second scan that disagreed with the bot
// (it reported "no findings" while the bot flagged real issues) and produced a false
// "cleared/LGTM". One security signal now: the agent itself.
//
// Env: GH_TOKEN, REPO (owner/name), BRANCH (df/issue-N), POLL_TIMEOUT (seconds),
//   BLOCK_LEVEL (none|low|medium|high|critical) — findings at/above this fail the
//   status (default medium; the bot doesn't expose per-severity counts uniformly, so
//   ANY finding fails unless BLOCK_LEVEL=none, in which case findings are advisory).
const https = require("https");
const { GH_TOKEN, REPO, BRANCH } = process.env;
const POLL_TIMEOUT = parseInt(process.env.POLL_TIMEOUT || "900", 10);
const BLOCK_LEVEL = (process.env.BLOCK_LEVEL || "medium").toLowerCase();
const H = { "User-Agent": "dark-factory-security-wait", Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };
const CONTEXT = "dark-factory/security";

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
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const isSecBot = (l) => /aws-security-agent/i.test(l || "") && /\[bot\]/i.test(l || "");

// Classify the bot's review body + inline-comment count into a verdict.
//   { done:true, findings:N, desc }  |  { done:false } (still reviewing / not posted)
function classify(reviewBody, inlineCount, sawAck) {
  const body = reviewBody || "";
  const low = body.toLowerCase();
  const m = low.match(/(\d+)\s+(?:medium|high|low|critical|informational)?[- ]?severity?\s*finding/) || low.match(/\b(\d+)\s+finding/);
  const declared = m ? parseInt(m[1], 10) : null;
  const saysClean = /no (issues identified|findings|security issues)|no issues were|looks good/i.test(body);
  if (declared !== null) return { done: true, findings: declared, desc: declared > 0 ? `${declared} finding(s)` : "no findings" };
  if (saysClean) return { done: true, findings: 0, desc: "no findings" };
  if (reviewBody && inlineCount > 0) return { done: true, findings: inlineCount, desc: `${inlineCount} finding(s)` };
  // A bot that ONLY posted "reviewing…"/ack with no verdict body yet → not done.
  return { done: false };
}

async function postStatus(state, description) {
  const prs = await api("GET", `/repos/${REPO}/pulls?head=${REPO.split("/")[0]}:${BRANCH}&state=open`);
  if (!prs.length) { console.log("[security-wait] no open PR"); return; }
  const sha = prs[0].head.sha;
  await api("POST", `/repos/${REPO}/statuses/${sha}`, { state, context: CONTEXT, description: description.slice(0, 140) });
  console.log(`[security-wait] posted ${CONTEXT}=${state} — ${description}`);
}

async function main() {
  const prs = await api("GET", `/repos/${REPO}/pulls?head=${REPO.split("/")[0]}:${BRANCH}&state=open`);
  if (!prs.length) { console.log("[security-wait] no open PR — nothing to wait for"); return; }
  const pr = prs[0].number;
  // ROUND-AWARENESS: on a fix round the coder force-pushes a NEW head commit; the
  // bot must RE-REVIEW that commit. A prior round's review still exists on the PR,
  // so we must only trust a bot review/comment tied to the CURRENT head SHA — else
  // a stale "1 finding" (or stale "clean") from the previous commit would be mirrored.
  const headSha = prs[0].head.sha;
  const commits = (await api("GET", `/repos/${REPO}/pulls/${pr}/commits?per_page=100`).catch(() => [])) || [];
  const headWhen = (() => { const c = commits.find((x) => x.sha === headSha); return c && (new Date((c.commit.committer || c.commit.author || {}).date)).getTime(); })() || 0;
  const forHead = (item, tsField) => {
    // Reviews carry commit_id; inline comments carry commit_id/original_commit_id.
    if (item.commit_id) return item.commit_id === headSha;
    // Issue comments have no SHA — accept only if posted at/after the head commit time.
    const t = tsField && item[tsField] ? Date.parse(item[tsField]) : 0;
    return headWhen ? t >= headWhen - 5000 : true;
  };
  console.log(`[security-wait] waiting up to ${POLL_TIMEOUT}s for aws-security-agent[bot] on PR #${pr} @ ${headSha.slice(0,7)} (block at ${BLOCK_LEVEL})`);
  const deadline = Date.now() + POLL_TIMEOUT * 1000;
  // The bot's TERMINAL verdict for a commit is a formal REVIEW (summary body) OR an
  // explicit "No issues identified" issue-comment — NOT bare inline comments. Inline
  // comments arrive incrementally while the bot is still analysing and GitHub can
  // re-attribute a prior round's inline comment to the new head SHA, so counting them
  // alone resolved TOO EARLY (observed: reported "1 finding" while a "reviewing…" ack
  // for the same commit was still posting). So we resolve ONLY on a terminal signal,
  // and only AFTER seeing this round's "reviewing…" ack (proves the bot started on THIS
  // commit). Findings count = the terminal review's declared number, else its inline count.
  let sawAck = false, ackAt = 0;
  while (Date.now() < deadline) {
    const reviews = (await api("GET", `/repos/${REPO}/pulls/${pr}/reviews?per_page=100`).catch(() => [])) || [];
    const comments = (await api("GET", `/repos/${REPO}/pulls/${pr}/comments?per_page=100`).catch(() => [])) || [];
    const issueComments = (await api("GET", `/repos/${REPO}/issues/${pr}/comments?per_page=100`).catch(() => [])) || [];

    // This round's "reviewing…" ack (issue comment tied to head by time).
    const ack = issueComments.filter((c) => isSecBot((c.user || {}).login) && /reviewing|will post/i.test(c.body || "") && forHead(c, "created_at")).slice(-1)[0];
    if (ack) { sawAck = true; ackAt = Date.parse(ack.created_at) || 0; }

    // TERMINAL signals for THIS commit, posted AFTER this round's ack:
    //  (a) a formal review with a summary body (commit_id == head), or
    //  (b) an explicit clean issue-comment ("no issues identified").
    const afterAck = (ts) => !ackAt || (Date.parse(ts || 0) >= ackAt - 2000);
    const botReview = reviews.filter((r) => isSecBot((r.user || {}).login) && forHead(r, "submitted_at") && (r.body || "").trim() && afterAck(r.submitted_at)).slice(-1)[0];
    const cleanComment = issueComments.some((c) => isSecBot((c.user || {}).login) && /no (issues identified|findings)/i.test(c.body || "") && forHead(c, "created_at") && afterAck(c.created_at));
    // inline comments on THIS commit posted after the ack (only used to COUNT once terminal).
    const inline = comments.filter((c) => isSecBot((c.user || {}).login) && forHead(c, "created_at") && afterAck(c.created_at)).length;

    let verdict = { done: false };
    if (botReview) verdict = classify(botReview.body, inline, sawAck);
    else if (cleanComment) verdict = { done: true, findings: 0, desc: "no findings" };
    // NOTE: we deliberately do NOT resolve on inline comments alone — wait for (a) or (b).

    if (verdict.done) {
      const blocked = BLOCK_LEVEL !== "none" && verdict.findings > 0;
      await postStatus(blocked ? "failure" : "success", `security: ${verdict.desc}${verdict.findings > 0 && !blocked ? " (advisory)" : ""}`);
      return;
    }
    await sleep(15);
  }
  // Timed out. Prefer NOT to green a PR the bot never cleared — post pending so the
  // consolidated review shows "in progress" rather than a false pass.
  console.log("[security-wait] timed out waiting for the Security Agent bot verdict");
  await postStatus("pending", "security: AWS Security Agent review still in progress (timed out waiting)");
}

main().catch((e) => { console.error(`[security-wait] error (non-fatal): ${e.message}`); process.exit(0); });
