/**
 * One InterpreterSession per MCP session.
 *
 * Lazy by design: constructing this provisions NOTHING. The AgentCore session is started
 * on the first real tool call and released when the session goes idle, so an agent that
 * merely knows about these tools costs nothing and a pod at rest holds no sessions.
 *
 * Each MCP session gets its OWN AgentCore session, because a session is a live interpreter:
 * variables persist between calls and so does its filesystem (both verified against the live
 * service; see DESIGN.md, "What the service actually is"). Two conversations must never
 * share that.
 *
 * No child process, unlike the browser server: the SDK is the whole client.
 */

import { config } from './config.js';
import { log } from './log.js';
import { startSession, stopSession, invokeTool } from './agentcore.js';
import { TASK_STARTING_TOOL, TASK_POLLING_TOOLS, normalizeArgs } from './tools.js';

/** Terminal states reported by getTask; anything else means the task is still live. */
const TERMINAL_TASK_STATES = new Set(['completed', 'canceled', 'failed']);

export class InterpreterSession {
  constructor(mcpSessionId, deps) {
    this.mcpSessionId = mcpSessionId;
    this.deps = deps;
    this.sessionId = null;
    this.idleTimer = null;
    this.ttlTimer = null;
    this.closed = false;
    this.holdsSlot = false;
    this.activating = null;
    /**
     * Async tasks handed out by startCommandExecution that have not reached a terminal
     * state. While this is non-empty, idle eviction is suppressed: the task runs inside
     * this AgentCore session, so stopping the session would kill it, and getTask needs the
     * same sessionId, so there would be no way to recover it.
     */
    this.openTasks = new Set();
  }

  get active() {
    return this.sessionId !== null;
  }

  async #activate() {
    const { interpreterId, dataPlane, limiter } = this.deps;

    limiter.acquire();
    this.holdsSlot = true;

    let sessionId;
    try {
      ({ sessionId } = await startSession(dataPlane, interpreterId, 'code-interpreter-mcp'));
    } catch (err) {
      this.#releaseSlot();
      throw err;
    }
    this.sessionId = sessionId;

    // Release slightly before the AWS-enforced TTL so teardown is ours rather than a
    // surprise mid-call failure.
    this.ttlTimer = setTimeout(
      () => this.deactivate('session-ttl').catch(() => {}),
      Math.max(1, config.sessionTimeoutSeconds - 30) * 1000,
    );

    log.info(
      { mcpSessionId: this.mcpSessionId, interpreterSessionId: sessionId },
      'Started AgentCore code interpreter session',
    );
  }

  async #ensureActive() {
    if (this.closed) throw new Error('MCP session is closed');
    if (this.active) return;
    if (!this.activating) {
      this.activating = this.#activate().finally(() => {
        this.activating = null;
      });
    }
    await this.activating;
  }

  #resetIdleTimer() {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = null;
    if (config.sessionIdleSeconds <= 0) return;
    if (this.openTasks.size > 0) {
      // A background task is still running. Do not arm the timer at all; it is re-armed
      // when the last task reaches a terminal state.
      return;
    }
    this.idleTimer = setTimeout(
      () => this.deactivate('idle').catch(() => {}),
      config.sessionIdleSeconds * 1000,
    );
  }

  /** Track async task lifecycle so eviction cannot kill a running task. */
  #trackTasks(toolName, args, result) {
    if (toolName === TASK_STARTING_TOOL) {
      const taskId = result?.structuredContent?.taskId;
      if (taskId) {
        this.openTasks.add(taskId);
        log.info(
          { mcpSessionId: this.mcpSessionId, taskId, openTasks: this.openTasks.size },
          'Async task started; idle eviction suppressed for this session',
        );
      }
      return;
    }
    if (!TASK_POLLING_TOOLS.has(toolName)) return;

    const taskId = args?.taskId;
    if (!taskId || !this.openTasks.has(taskId)) return;
    const status = result?.structuredContent?.taskStatus;
    // stopTask on a live task ends it even though it reports no structured status.
    const finished =
      (status && TERMINAL_TASK_STATES.has(String(status).toLowerCase())) ||
      (toolName === 'stopTask' && !result?.isError);
    if (finished) {
      this.openTasks.delete(taskId);
      log.info(
        { mcpSessionId: this.mcpSessionId, taskId, status: status || 'stopped', openTasks: this.openTasks.size },
        'Async task finished',
      );
    }
  }

  /** Forward a tool call, starting the interpreter session on first use. */
  async callTool(name, rawArgs) {
    await this.#ensureActive();
    // Apply schema-promised defaults before the service sees the call.
    const args = normalizeArgs(name, rawArgs);
    try {
      const result = await invokeTool(
        this.deps.dataPlane,
        this.deps.interpreterId,
        this.sessionId,
        name,
        args,
      );
      this.#trackTasks(name, args, result);
      return result;
    } finally {
      // Re-arm after every call, including failures, so a broken session still ages out.
      this.#resetIdleTimer();
    }
  }

  #releaseSlot() {
    if (this.holdsSlot) {
      this.holdsSlot = false;
      this.deps.limiter.release();
    }
  }

  async #stopInterpreterSession() {
    if (!this.sessionId) {
      this.#releaseSlot();
      return;
    }
    const id = this.sessionId;
    this.sessionId = null;
    try {
      await stopSession(this.deps.dataPlane, this.deps.interpreterId, id);
      log.info(
        { mcpSessionId: this.mcpSessionId, interpreterSessionId: id },
        'Stopped AgentCore code interpreter session',
      );
    } catch (err) {
      log.warn({ interpreterSessionId: id, err: err.message }, 'Failed to stop interpreter session');
    } finally {
      this.#releaseSlot();
    }
  }

  /**
   * Release the interpreter but keep the MCP session usable: tools/list keeps working and
   * the next call starts a fresh interpreter. Note that discards variables AND files, so
   * anything the caller wanted to keep is gone.
   */
  async deactivate(reason) {
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null; }
    if (this.ttlTimer) { clearTimeout(this.ttlTimer); this.ttlTimer = null; }

    if (this.openTasks.size > 0 && reason === 'idle') {
      // Defensive: the timer should not have been armed at all in this state.
      log.warn(
        { mcpSessionId: this.mcpSessionId, openTasks: this.openTasks.size },
        'Idle eviction skipped: async tasks still running',
      );
      this.#resetIdleTimer();
      return;
    }
    if (this.openTasks.size > 0) {
      log.warn(
        { mcpSessionId: this.mcpSessionId, reason, openTasks: this.openTasks.size },
        'Releasing session with async tasks still running; those tasks are lost',
      );
    }
    this.openTasks.clear();

    if (this.sessionId) log.info({ mcpSessionId: this.mcpSessionId, reason }, 'Releasing interpreter');
    await this.#stopInterpreterSession();
  }

  /** Terminal teardown for this MCP session. */
  async close(reason = 'closed') {
    this.closed = true;
    await this.deactivate(reason);
  }
}
