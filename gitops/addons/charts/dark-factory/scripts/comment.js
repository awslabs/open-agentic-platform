// comment.js — upsert ONE marker-based PR comment (edit in place, no spam).
// Shared by the review roles, holdout, and deploy-test so their findings land on
// the PR, not just in pod logs. Idempotent: same marker → same comment edited.
//
// Usage:  node comment.js <marker>        (body read from stdin)
// Env:    GH_TOKEN, REPO (owner/name), PR (number)
const https = require("https");
const { GH_TOKEN, REPO, PR } = process.env;
const marker = process.argv[2];
if (!marker || !PR) { console.error("[comment] missing marker or PR — skip"); process.exit(0); }
const H = { "User-Agent": "dark-factory-comment", Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };

function api(method, path, body) {
  const data = body ? JSON.stringify(body) : null;
  return new Promise((resolve, reject) => {
    const req = https.request({ host: "api.github.com", method, path, headers: { ...H, ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}) } },
      (r) => { let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => (r.statusCode < 300 ? resolve(b ? JSON.parse(b) : {}) : reject(new Error(`${method} ${path} ${r.statusCode}: ${b.slice(0, 120)}`)))); });
    req.on("error", reject); if (data) req.write(data); req.end();
  });
}

let body = "";
process.stdin.on("data", (d) => (body += d)).on("end", async () => {
  const full = `<!-- ${marker} -->\n${body.trim()}`;
  try {
    // Find an existing comment carrying this marker (paginate a little).
    let existing = null;
    for (let page = 1; page <= 5 && !existing; page++) {
      const cs = await api("GET", `/repos/${REPO}/issues/${PR}/comments?per_page=100&page=${page}`);
      if (!cs.length) break;
      existing = cs.find((c) => (c.body || "").includes(`<!-- ${marker} -->`));
    }
    if (existing) { await api("PATCH", `/repos/${REPO}/issues/comments/${existing.id}`, { body: full }); console.log(`[comment] updated ${marker} on PR #${PR}`); }
    else { await api("POST", `/repos/${REPO}/issues/${PR}/comments`, { body: full }); console.log(`[comment] created ${marker} on PR #${PR}`); }
  } catch (e) { console.error(`[comment] non-fatal: ${e.message}`); process.exit(0); }
});
