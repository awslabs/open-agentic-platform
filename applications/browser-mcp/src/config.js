/**
 * Configuration, entirely environment-driven.
 *
 * Region and credentials come from the environment (EKS Pod Identity via the
 * aws-service-identity trait). Nothing region- or account-specific is hardcoded,
 * so the same image and the same OAM deploy to any region or environment.
 */

const int = (name, fallback) => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n)) throw new Error(`${name} must be an integer, got: ${raw}`);
  return n;
};

const list = (name) =>
  (process.env[name] || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

export const config = {
  // HTTP surface consumed by agentgateway.
  port: int('MCP_PORT', 8000),
  mcpPath: process.env.MCP_PATH || '/mcp',

  // Which AgentCore browser to use. Either an explicit id, or a name we resolve
  // at runtime (a built-in like `aws.browser.v1` is used as an id directly).
  browserId: process.env.AGENTCORE_BROWSER_ID || '',
  browserName: process.env.AGENTCORE_BROWSER_NAME || '',

  // AgentCore browser session TTL requested at mint time. This is enforced by
  // AWS, not by us, which makes it the ONLY bound on a leak if this pod dies hard
  // (SIGKILL/OOM/node loss kills our in-process timers with it). 900s matches the
  // AgentCore documented default; a longer TTL only widens that worst case.
  sessionTimeoutSeconds: int('SESSION_TIMEOUT_SECONDS', 900),

  // Stop an idle AgentCore session (and its child) after this long with no tool
  // calls. A later call transparently re-mints, so an idle pod costs nothing.
  sessionIdleSeconds: int('SESSION_IDLE_SECONDS', 300),

  // Reap an entire MCP session after this long with no requests at all. Clients
  // can disappear without sending DELETE, and we must not hold their session
  // (or its browser) forever.
  mcpSessionIdleSeconds: int('MCP_SESSION_IDLE_SECONDS', 900),

  // How often the reaper scans for abandoned sessions.
  reaperIntervalSeconds: int('REAPER_INTERVAL_SECONDS', 60),

  // Safety cap on concurrent live browser sessions per pod. Keep under the
  // account's AgentCore browser session quota.
  maxSessions: int('MAX_BROWSER_SESSIONS', 25),

  // How long to wait for a named browser to appear/become READY (Crossplane
  // may still be provisioning it when we start).
  browserReadyTimeoutSeconds: int('BROWSER_READY_TIMEOUT_SECONDS', 300),

  // Tool surface control. Default: expose everything the pinned
  // chrome-devtools-mcp advertises. TOOLS_ALLOW (if set) wins, then TOOLS_DENY.
  toolsAllow: list('TOOLS_ALLOW'),
  toolsDeny: list('TOOLS_DENY'),

  logLevel: (process.env.LOG_LEVEL || 'info').toLowerCase(),

  // The pinned stdio MCP server we front. Installed in the image.
  cdpCommand: process.env.CDP_MCP_COMMAND || 'chrome-devtools-mcp',
};

export function validate() {
  if (!config.browserId && !config.browserName) {
    throw new Error('Set AGENTCORE_BROWSER_NAME (or AGENTCORE_BROWSER_ID).');
  }
}
