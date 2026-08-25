/**
 * Environment-driven configuration. Every knob is an env var so a team can tune it
 * through the mcp-server component's `env` list without rebuilding the image.
 */

const int = (name, fallback) => {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : fallback;
};

const list = (name) =>
  (process.env[name] || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

export const config = {
  port: int('MCP_PORT', 8000),
  mcpPath: process.env.MCP_PATH || '/mcp',

  // Region is injected by the aws-service-identity trait from the cluster's region, so
  // it never appears in a developer's OAM.
  region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || 'us-west-2',

  /**
   * Which code interpreter to use. A NAME is resolved to an id at boot via
   * ListCodeInterpreters, matching how the browser server resolves a browser name, so the
   * OAM app refers to a stable name rather than a generated id.
   *
   * Leave the name empty to use the AWS built-in `aws.codeinterpreter.v1`, which needs no
   * provisioning. Verified working in spikes/agentcore-code-interpreter.
   */
  interpreterName: process.env.AGENTCORE_CODE_INTERPRETER_NAME || '',
  builtinInterpreterId: process.env.AGENTCORE_BUILTIN_ID || 'aws.codeinterpreter.v1',

  /** Budget for one initialisation cycle to resolve the interpreter. */
  readyTimeoutSeconds: int('INTERPRETER_READY_TIMEOUT_SECONDS', 300),
  /** Pause between initialisation cycles. Initialisation retries forever. */
  initRetrySeconds: int('INIT_RETRY_SECONDS', 15),

  /**
   * TTL requested when starting a session. AWS enforces it, so it is the only bound on a
   * leak if this pod dies hard. It also bounds any async task, because a task cannot
   * outlive the session that owns it.
   */
  sessionTimeoutSeconds: int('SESSION_TIMEOUT_SECONDS', 900),

  /**
   * Release an idle session's interpreter; the next call transparently starts a fresh one.
   * Note this discards the session's variables AND its filesystem, so raise it for flows
   * that build up state across long gaps.
   *
   * Eviction is suppressed while a session has an outstanding async task, because
   * stopping the session would take the task with it (see the spike findings).
   */
  sessionIdleSeconds: int('SESSION_IDLE_SECONDS', 300),

  /** Cap on concurrent live interpreter sessions for this pod. */
  maxSessions: int('MAX_INTERPRETER_SESSIONS', 25),

  /** How often to sweep MCP sessions whose client vanished without a DELETE. */
  reaperIntervalSeconds: int('REAPER_INTERVAL_SECONDS', 60),
  /** An MCP session with no requests for this long is torn down entirely. */
  mcpSessionIdleSeconds: int('MCP_SESSION_IDLE_SECONDS', 1800),

  /** Optional allow/deny filtering of the advertised tool surface. */
  toolsAllow: list('TOOLS_ALLOW'),
  toolsDeny: list('TOOLS_DENY'),

  logLevel: process.env.LOG_LEVEL || 'info',
};
