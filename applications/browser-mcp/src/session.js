/**
 * One BrowserSession per MCP session.
 *
 * Lazy by design: constructing this object provisions NOTHING. The AgentCore
 * browser session and its dedicated chrome-devtools-mcp child are created on the
 * first actual tool call, and torn down again when the session goes idle. So an
 * agent that merely knows about the browser tools costs nothing, and a pod at
 * rest holds no browser sessions.
 *
 * Each MCP session gets its OWN AgentCore session, so conversations never share
 * cookies or page state. The pod is only a signer/router: the browser itself runs
 * in AgentCore, which is why one pod can host many concurrent sessions.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

import { config } from './config.js';
import { log } from './log.js';
import { signWsHeaders, startSession, stopSession } from './agentcore.js';

export class BrowserSession {
  /**
   * @param {object} deps { region, browserId, dataPlane } shared, resolved once at boot.
   */
  constructor(mcpSessionId, deps) {
    this.mcpSessionId = mcpSessionId;
    this.deps = deps;
    this.client = null;
    this.transport = null;
    this.browserSessionId = null;
    this.idleTimer = null;
    this.ttlTimer = null;
    this.closed = false;
    this.holdsSlot = false;
    // Set while we tear down on purpose, so the child's onclose is not mistaken
    // for an unexpected death and does not recurse back into deactivate().
    this.tearingDown = false;
    // Bounded tail of the child's stderr, reported if it dies unexpectedly.
    this.childStderr = '';
    // Serialises concurrent first-calls so we mint exactly one browser session.
    this.activating = null;
  }

  get active() {
    return this.client !== null;
  }

  /** Mint the AgentCore session and attach a dedicated chrome-devtools-mcp child. */
  async #activate() {
    const { region, browserId, dataPlane, limiter } = this.deps;

    // Cap applies to LIVE browser sessions; idle MCP sessions cost nothing.
    limiter.acquire();
    this.holdsSlot = true;

    let sessionId;
    try {
      ({ sessionId } = await startSession(dataPlane, browserId, 'browser-mcp'));
    } catch (err) {
      this.#releaseSlot();
      throw err;
    }
    this.browserSessionId = sessionId;
    log.info(
      { mcpSessionId: this.mcpSessionId, browserSessionId: sessionId },
      'Started AgentCore browser session',
    );

    try {
      const { wsUrl, headers } = await signWsHeaders(region, browserId, sessionId);

      this.transport = new StdioClientTransport({
        command: config.cdpCommand,
        args: [
          '--headless=true',
          `--wsEndpoint=${wsUrl}`,
          `--wsHeaders=${JSON.stringify(headers)}`,
        ],
        stderr: 'pipe',
      });
      const client = new Client({ name: 'browser-mcp', version: '0.1.0' });
      await client.connect(this.transport);
      this.client = client;

      // Always consume the child's stderr. It is piped, and an unread pipe fills
      // (~64KB) and then blocks the child on write, which would present as tool
      // calls hanging rather than failing. Keep only a tail, to report if it dies.
      if (this.transport.stderr) {
        this.transport.stderr.on('data', (chunk) => {
          this.childStderr = (this.childStderr + chunk.toString()).slice(-2048);
        });
      }

      // Detect a child that dies on its own: crash, OOM, or the CDP websocket
      // closing because AWS ended the browser session early. Without this the
      // session parks in a broken state: `client` stays non-null so `active`
      // reports true, every later tool call fails, and the AgentCore session plus
      // the limiter slot stay held until the idle timer fires minutes later.
      //
      // NOTE: these must be set AFTER connect(). Protocol.connect() overwrites
      // transport.onclose/onerror, so wiring the transport directly is silently
      // discarded; the protocol-level callbacks are the supported hook.
      client.onclose = () => {
        if (this.tearingDown || this.closed) return;
        log.warn(
          { mcpSessionId: this.mcpSessionId, stderrTail: this.childStderr.slice(-400) || undefined },
          'Browser child exited unexpectedly; releasing session so the next call re-mints',
        );
        this.deactivate('child-exited').catch((err) =>
          log.error({ mcpSessionId: this.mcpSessionId, err: err.message }, 'Cleanup after child exit failed'),
        );
      };
      client.onerror = (err) => {
        log.warn({ mcpSessionId: this.mcpSessionId, err: String(err?.message || err) }, 'Browser child error');
      };

      // Each session owns its TTL, so expiry is handled per session rather than
      // by recycling the whole pod.
      this.ttlTimer = setTimeout(
        () => this.deactivate('session-ttl').catch(() => {}),
        Math.max(1, config.sessionTimeoutSeconds - 30) * 1000,
      );

      log.info({ mcpSessionId: this.mcpSessionId }, 'Attached to browser over CDP');
    } catch (err) {
      // Never leak a paid-for AgentCore session if attaching failed.
      await this.#stopBrowserSession();
      throw err;
    }
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
    if (config.sessionIdleSeconds <= 0) return;
    this.idleTimer = setTimeout(
      () => this.deactivate('idle').catch(() => {}),
      config.sessionIdleSeconds * 1000,
    );
  }

  /** Forward a tool call, activating the browser on first use. */
  async callTool(name, args) {
    await this.#ensureActive();
    this.#resetIdleTimer();
    return this.client.callTool({ name, arguments: args || {} });
  }

  #releaseSlot() {
    if (this.holdsSlot) {
      this.holdsSlot = false;
      this.deps.limiter.release();
    }
  }

  async #stopBrowserSession() {
    if (!this.browserSessionId) {
      this.#releaseSlot();
      return;
    }
    const id = this.browserSessionId;
    this.browserSessionId = null;
    try {
      await stopSession(this.deps.dataPlane, this.deps.browserId, id);
      log.info({ mcpSessionId: this.mcpSessionId, browserSessionId: id }, 'Stopped AgentCore browser session');
    } catch (err) {
      log.warn({ browserSessionId: id, err: err.message }, 'Failed to stop browser session');
    } finally {
      this.#releaseSlot();
    }
  }

  /**
   * Release the browser but keep the MCP session usable: tools/list keeps working
   * and the next tool call transparently re-mints a fresh browser session.
   */
  async deactivate(reason) {
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null; }
    if (this.ttlTimer) { clearTimeout(this.ttlTimer); this.ttlTimer = null; }

    const client = this.client;
    this.client = null;
    this.transport = null;

    if (client) {
      log.info({ mcpSessionId: this.mcpSessionId, reason }, 'Releasing browser');
      // Suppress our own onclose: closing the client is what we are doing, not a
      // failure to react to. Closing also kills the child process, and its
      // orphaned watchdog is then reaped by tini as PID 1.
      this.tearingDown = true;
      try {
        await client.close().catch(() => {});
      } finally {
        this.tearingDown = false;
      }
    }
    this.childStderr = '';
    await this.#stopBrowserSession();
  }

  /** Terminal teardown for this MCP session. */
  async close(reason = 'closed') {
    this.closed = true;
    await this.deactivate(reason);
  }
}
