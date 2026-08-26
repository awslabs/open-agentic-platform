// iterate.js — route a human PR comment back to the coder as a revision request.
// Runs in the df-iterate workflow (fired by an issue_comment on a Dark Factory PR).
//
// The issue_comment payload gives us the PR number + comment text, but not the
// coder branch or the original df issue number. So we: (1) look up the PR to get
// head.ref = df/issue-<n> → issue number; (2) enforce the iteration cap via a
// label on the PR; (3) submit a df-run Workflow (same pipeline) with iterate-note
// = the comment, which df-run injects as DF_ITERATE_NOTE so the coder revises the
// existing branch. Submits via the in-cluster k8s API using the pod SA token
// (the df-iterate workflow runs as dark-factory-sensor, which can create Workflows).
//
// Env: GH_TOKEN, REPO, PR, COMMENT_BODY, MAX_ITERATIONS, ARGO_NAMESPACE,
//      BIFROST_URL, CODER_PROFILE.
const fs = require("fs");
const https = require("https");

const { GH_TOKEN, REPO, PR, COMMENT_BODY, COMMENT_AUTHOR, ARGO_NAMESPACE } = process.env;
const MAX_ITERATIONS = parseInt(process.env.MAX_ITERATIONS || "3", 10);
const GH = { "User-Agent": "dark-factory-iterate", Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };
const ITER_LABEL_PREFIX = "df-iterations/";

function gh(method, path, body) {
  const data = body ? JSON.stringify(body) : null;
  return new Promise((resolve, reject) => {
    const req = https.request({ host: "api.github.com", method, path, headers: { ...GH, ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}) } },
      (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => {
        if (r.statusCode >= 200 && r.statusCode < 300) resolve(b ? JSON.parse(b) : {});
        else reject(Object.assign(new Error(`gh ${method} ${path} -> ${r.statusCode}: ${b.slice(0, 160)}`), { statusCode: r.statusCode }));
      }); });
    req.on("error", reject); if (data) req.write(data); req.end();
  });
}

// Submit a Workflow to the in-cluster k8s API using the pod SA token.
function submitWorkflow(wf) {
  const token = fs.readFileSync("/var/run/secrets/kubernetes.io/serviceaccount/token", "utf8");
  const ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
  const body = JSON.stringify(wf);
  return new Promise((resolve, reject) => {
    const req = https.request({
      host: "kubernetes.default.svc", method: "POST",
      path: `/apis/argoproj.io/v1alpha1/namespaces/${ARGO_NAMESPACE}/workflows`,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      ca: fs.readFileSync(ca),
    }, (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => {
      if (r.statusCode >= 200 && r.statusCode < 300) resolve(JSON.parse(b));
      else reject(Object.assign(new Error(`k8s submit -> ${r.statusCode}: ${b.slice(0, 200)}`), { statusCode: r.statusCode }));
    }); });
    req.on("error", reject); req.write(body); req.end();
  });
}

async function main() {
  // SELF-TRIGGER GUARD (critical): the factory posts its OWN comments to the PR
  // (sticky status, review findings, iteration notices) using a real user's PAT —
  // so GitHub reports comment.user.type="User", and the Sensor's "exclude Bot"
  // filter does NOT exclude them. Without this guard, EVERY factory comment fires
  // df-iterate → new commit → more comments → runaway loop (observed: 3 runs +
  // 3 commits + split statuses on one issue). All factory-authored comments carry a
  // "dark-factory:" HTML marker; skip any comment that has one.
  if (COMMENT_BODY && /<!--\s*dark-factory:/.test(COMMENT_BODY)) {
    console.log("[df-iterate] comment carries a Dark Factory marker — skipping (no self-trigger)");
    return;
  }
  // BOT guard (the leak that caused the runaway): the AWS Security/DevOps AGENT BOTS
  // post findings comments that have NO dark-factory marker and are NOT the factory
  // identity — so without this they fired df-iterate → coder pushes → the bots re-review
  // and comment again → df-iterate again → runaway (observed: ~12 df-iterate runs +
  // overlapping commits on one PR). A real human reviewer never has a "[bot]" login (or
  // user.type=="Bot"). Skip any bot-authored comment; iteration is HUMAN-driven only.
  if (COMMENT_AUTHOR && /\[bot\]$/i.test(COMMENT_AUTHOR)) {
    console.log(`[df-iterate] comment author (${COMMENT_AUTHOR}) is a bot — skipping (iteration is human-driven only)`);
    return;
  }
  if ((process.env.COMMENT_AUTHOR_TYPE || "").toLowerCase() === "bot") {
    console.log(`[df-iterate] comment author type is Bot — skipping (no self-trigger)`);
    return;
  }
  // IDENTITY-based guard: the factory posts comments (verdict review, auto-fix notices,
  // relayed findings) as the token owner. If the comment author IS that identity, it's
  // the factory talking to itself → skip. Catches any factory comment even if unmarkered.
  try {
    const me = await gh("GET", "/user");
    if (COMMENT_AUTHOR && me.login && COMMENT_AUTHOR === me.login) {
      console.log(`[df-iterate] comment author (${COMMENT_AUTHOR}) is the factory identity — skipping (no self-trigger)`);
      return;
    }
  } catch { /* if /user fails, fall back to the marker + bot guards above */ }
  const pr = await gh("GET", `/repos/${REPO}/pulls/${PR}`);
  if (pr.state !== "open") { console.log(`[df-iterate] PR #${PR} is ${pr.state} — skipping`); return; }
  const ref = pr.head.ref;                       // df/issue-<n>
  const m = ref.match(/^df\/issue-(\d+)$/);
  if (!m) { console.log(`[df-iterate] PR head ${ref} is not a df/issue branch — skipping`); return; }
  const issueNumber = m[1];

  // Iteration cap: count via a df-iterations/<n> label on the PR (issue API).
  const issue = await gh("GET", `/repos/${REPO}/issues/${PR}`);
  const labels = (issue.labels || []).map((l) => (typeof l === "string" ? l : l.name));
  // Substrate routing: the fix round must run on the SAME substrate the PR came from,
  // so it lands on the right warm pool (Lambda MicroVM vs Kata). The ORIGINATING ISSUE
  // (not the PR — the coder doesn't copy the label onto the PR) carries the label that
  // fired it. Read the issue's labels; default to plain dark-factory (Kata) on any miss.
  let triggerLabel = "dark-factory";
  try {
    const origIssue = await gh("GET", `/repos/${REPO}/issues/${issueNumber}`);
    const il = (origIssue.labels || []).map((l) => (typeof l === "string" ? l : l.name));
    if (il.includes("darkfactory-lambda")) triggerLabel = "darkfactory-lambda";
  } catch (e) { console.log(`[df-iterate] could not read issue #${issueNumber} labels (${e.message}) — defaulting Kata`); }
  console.log(`[df-iterate] substrate trigger-label=${triggerLabel}`);
  const cur = labels.filter((l) => l.startsWith(ITER_LABEL_PREFIX)).map((l) => parseInt(l.slice(ITER_LABEL_PREFIX.length), 10)).filter((n) => !isNaN(n));
  const count = cur.length ? Math.max(...cur) : 0;
  if (count >= MAX_ITERATIONS) {
    console.log(`[df-iterate] PR #${PR} hit the iteration cap (${count}/${MAX_ITERATIONS}) — a human must break the tie`);
    await gh("POST", `/repos/${REPO}/issues/${PR}/comments`, { body: `🏭 Dark Factory: iteration cap reached (${count}/${MAX_ITERATIONS}). Please resolve manually or push a commit.` }).catch(() => {});
    return;
  }
  const next = count + 1;
  // Bump the counter label (remove old, add new).
  for (const l of cur) await gh("DELETE", `/repos/${REPO}/issues/${PR}/labels/${encodeURIComponent(ITER_LABEL_PREFIX + l)}`).catch(() => {});
  await gh("POST", `/repos/${REPO}/issues/${PR}/labels`, { labels: [`${ITER_LABEL_PREFIX}${next}`] }).catch(() => {});

  console.log(`[df-iterate] revision ${next}/${MAX_ITERATIONS} for issue #${issueNumber} (PR #${PR})`);
  // Substrate-routed template: Lambda fix rounds run the MicroVM-native df-run-lambda
  // (resumes the SAME suspended VM); Kata fix rounds run the certified df-run. Keyed on
  // the originating issue's label (resolved above as triggerLabel).
  const isLambda = triggerLabel === "darkfactory-lambda";
  const wfTemplate = isLambda ? "df-run-lambda" : "df-run";
  const wfName = isLambda ? `df-run-lambda-${issueNumber}-i${next}` : `df-run-${issueNumber}-i${next}`;
  console.log(`[df-iterate] substrate=${triggerLabel} → template=${wfTemplate}`);
  const wf = {
    apiVersion: "argoproj.io/v1alpha1", kind: "Workflow",
    // Dedup per issue+round so a duplicate comment webhook is a no-op.
    metadata: { name: wfName, namespace: ARGO_NAMESPACE },
    spec: {
      workflowTemplateRef: { name: wfTemplate },
      arguments: { parameters: [
        { name: "issue-id", value: `${issueNumber}` },        // no id in this payload; number is unique enough for the mutex/claim
        { name: "issue-number", value: `${issueNumber}` },
        { name: "repo", value: REPO },
        { name: "issue-title", value: pr.title },
        { name: "issue-body", value: "" },
        { name: "base-branch", value: pr.base.ref },
        // Route the fix round to the SAME substrate the PR came from (Lambda vs Kata),
        // so claim-sandbox picks the right warm pool. df-run branches its warm-pool on this.
        { name: "trigger-label", value: triggerLabel },
        // base64 the comment (may be multi-line markdown) so it can't break the
        // claim-sandbox manifest YAML; the coder decodes it. Leave plain empty.
        { name: "iterate-note", value: "" },
        { name: "iterate-note-b64", value: Buffer.from(COMMENT_BODY || "", "utf8").toString("base64") },
      ] },
    },
  };
  try {
    const created = await submitWorkflow(wf);
    console.log(`[df-iterate] submitted ${created.metadata.name} (revision ${next})`);
  } catch (e) {
    if (e.statusCode === 409) console.log(`[df-iterate] revision ${next} already in flight (dedup) — no-op`);
    else throw e;
  }
}

main().catch((e) => { console.error(`[df-iterate] failed: ${e.message}`); process.exit(1); });
