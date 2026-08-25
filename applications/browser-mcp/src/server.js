/**
 * browser-mcp: fronts Amazon Bedrock AgentCore Browser as an MCP server.
 *
 *   agent --(JWT via agentgateway)--> [this pod] --(SigV4 / CDP wss)--> AgentCore Browser
 *
 * Shape:
 *  - One StreamableHTTP MCP endpoint. Each MCP session gets its own MCP Server
 *    instance and its own BrowserSession, so conversations are isolated.
 *  - tools/list is served from a catalog discovered once at boot WITHOUT a
 *    browser, so agents can see and reason about the browser tools for free.
 *  - The AgentCore browser session is minted on the first real tools/call and
 *    released again when the session goes idle.
 *  - The pod hosts many concurrent sessions: it holds no browser itself, it only
 *    signs and routes. Do not scale pods to add browser sessions.
 */

import { randomUUID } from 'node:crypto';

import express from 'express';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  isInitializeRequest,
} from '@modelcontextprotocol/sdk/types.js';

import { config, validate } from './config.js';
import { log } from './log.js';
import { makeDataPlaneClient, resolveBrowserId, resolveRegion } from './agentcore.js';
import { discoverTools, getCatalog, isAdvertised } from './catalog.js';
import { BrowserSession } from './session.js';

// How long to wait between whole initialisation cycles. Each cycle itself retries
// internally for BROWSER_READY_TIMEOUT_SECONDS, so this is the pause between cycles.
const INIT_RETRY_SECONDS = Number.parseInt(process.env.INIT_RETRY_SECONDS || '15', 10);

/** Caps concurrent LIVE browser sessions (MCP sessions themselves are free). */
function makeLimiter(max) {
  let live = 0;
  return {
    get live() {
      return live;
    },
    acquire() {
      if (live >= max) {
        throw new Error(
          `This pod is at its concurrent browser session limit (${max}). ` +
            'Retry shortly, or raise MAX_BROWSER_SESSIONS.',
        );
      }
      live += 1;
    },
    release() {
      if (live > 0) live -= 1;
    },
  };
}

/** Build the MCP server for one session: catalog for list, routing for call. */
function buildMcpServer(browserSession, initState) {
  const server = new Server(
    { name: 'browser-mcp', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    if (!initState.ready) throw new Error('browser-mcp is still initialising');
    return { tools: getCatalog() };
  });

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    if (!initState.ready) {
      return {
        isError: true,
        content: [{ type: 'text', text: 'browser-mcp is still initialising; retry shortly' }],
      };
    }
    if (!isAdvertised(name)) {
      return {
        isError: true,
        content: [{ type: 'text', text: `Unknown tool: ${name}` }],
      };
    }
    try {
      return await browserSession.callTool(name, args);
    } catch (err) {
      log.error(
        { mcpSessionId: browserSession.mcpSessionId, tool: name, err: err.message },
        'Tool call failed',
      );
      return {
        isError: true,
        content: [{ type: 'text', text: `Browser tool "${name}" failed: ${err.message}` }],
      };
    }
  });

  return server;
}

async function main() {
  validate();

  const region = resolveRegion();
  const limiter = makeLimiter(config.maxSessions);

  // `browserId` is resolved AFTER the HTTP listener is up (see below), so this
  // object is deliberately mutable. BrowserSession reads it at activation time,
  // which is always after initialisation completed.
  const deps = { region, browserId: null, dataPlane: makeDataPlaneClient(region), limiter };

  // Startup is split from listening on purpose. Resolving the browser calls AWS,
  // and on a first deploy that call can fail for minutes while a freshly attached
  // IAM policy propagates. If we did that work before binding the port, the
  // liveness probe would fail from its initialDelay onward and the kubelet would
  // kill the container long before our own retry budget expired. So: listen
  // immediately, report liveness on /healthz, and gate traffic on /readyz until
  // initialisation genuinely completes.
  const initState = { ready: false, lastError: null, cycles: 0, startedAt: Date.now() };

  /** mcpSessionId -> { transport, server, browserSession } */
  const sessions = new Map();

  const app = express();
  app.use(express.json({ limit: '4mb' }));

  // Liveness: the process is up and serving HTTP. Deliberately independent of
  // AWS reachability, so a transient AWS problem cannot cause a restart loop.
  app.get('/healthz', (_req, res) => res.status(200).send('ok'));

  // Readiness: only true once we know the browser id and the tool catalog, i.e.
  // once we can actually answer MCP requests.
  app.get('/readyz', (_req, res) => {
    if (!initState.ready) {
      // Still 'initializing' even after a failed cycle: we never stop retrying, so
      // there is no terminal state to report.
      res.status(503).json({
        status: 'initializing',
        lastError: initState.lastError || undefined,
        failedCycles: initState.cycles || undefined,
        elapsedSeconds: Math.round((Date.now() - initState.startedAt) / 1000),
        region,
      });
      return;
    }
    res.status(200).json({
      status: 'ok',
      browserId: deps.browserId,
      region,
      mcpSessions: sessions.size,
      liveBrowserSessions: limiter.live,
      toolsAdvertised: getCatalog().length,
    });
  });

  const teardown = async (sid, reason) => {
    const entry = sessions.get(sid);
    if (!entry) return;
    sessions.delete(sid);
    await entry.browserSession.close(reason).catch(() => {});
    log.info({ mcpSessionId: sid, reason, remaining: sessions.size }, 'MCP session torn down');
  };

  app.post(config.mcpPath, async (req, res) => {
    try {
      const sid = req.headers['mcp-session-id'];

      if (sid && sessions.has(sid)) {
        const entry = sessions.get(sid);
        entry.lastSeen = Date.now();
        await entry.transport.handleRequest(req, res, req.body);
        return;
      }

      if (!sid && isInitializeRequest(req.body)) {
        const browserSession = new BrowserSession(null, deps);
        const server = buildMcpServer(browserSession, initState);
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          onsessioninitialized: (newId) => {
            browserSession.mcpSessionId = newId;
            sessions.set(newId, { transport, server, browserSession, lastSeen: Date.now() });
            log.info({ mcpSessionId: newId, total: sessions.size }, 'MCP session initialised');
          },
          onsessionclosed: (closedId) => {
            teardown(closedId, 'client-closed').catch(() => {});
          },
        });
        transport.onclose = () => {
          if (transport.sessionId) teardown(transport.sessionId, 'transport-closed').catch(() => {});
        };

        await server.connect(transport);
        await transport.handleRequest(req, res, req.body);
        return;
      }

      res.status(400).json({
        jsonrpc: '2.0',
        error: { code: -32000, message: 'Bad Request: no valid session ID provided' },
        id: null,
      });
    } catch (err) {
      log.error({ err: err.message }, 'Error handling MCP POST');
      if (!res.headersSent) {
        res.status(500).json({
          jsonrpc: '2.0',
          error: { code: -32603, message: 'Internal server error' },
          id: null,
        });
      }
    }
  });

  // SSE stream (server->client notifications) and explicit session termination.
  const bySession = async (req, res) => {
    const sid = req.headers['mcp-session-id'];
    if (!sid || !sessions.has(sid)) {
      res.status(400).send('Invalid or missing session ID');
      return;
    }
    const entry = sessions.get(sid);
    entry.lastSeen = Date.now();
    await entry.transport.handleRequest(req, res);
  };
  app.get(config.mcpPath, bySession);
  app.delete(config.mcpPath, bySession);

  // Clients can vanish without sending DELETE. Reap sessions that have gone
  // quiet so we never hold an MCP session (or its browser) indefinitely.
  const reaper = setInterval(() => {
    if (config.mcpSessionIdleSeconds <= 0) return;
    const cutoff = Date.now() - config.mcpSessionIdleSeconds * 1000;
    for (const [sid, entry] of sessions) {
      if (entry.lastSeen < cutoff) {
        log.info({ mcpSessionId: sid }, 'Reaping abandoned MCP session');
        teardown(sid, 'abandoned').catch(() => {});
      }
    }
  }, Math.max(1, config.reaperIntervalSeconds) * 1000);
  reaper.unref();

  const httpServer = app.listen(config.port, '0.0.0.0', () => {
    log.info(
      {
        port: config.port,
        path: config.mcpPath,
        region,
        maxBrowserSessions: config.maxSessions,
        sessionTimeoutSeconds: config.sessionTimeoutSeconds,
        idleSeconds: config.sessionIdleSeconds,
      },
      'browser-mcp listening; initialising (not ready yet)',
    );
  });

  // Initialise in the background now that the port is bound, and retry FOREVER.
  // Giving up would leave the pod alive but permanently unready, which needs a
  // human to notice and delete it. Since readiness gates traffic and liveness only
  // reports that the process is up, retrying indefinitely is both safe and
  // self-healing: whenever the dependency becomes available, we converge.
  (async function initLoop() {
    for (let cycle = 1; ; cycle += 1) {
      try {
        deps.browserId = await resolveBrowserId(region);
        await discoverTools();
        initState.ready = true;
        initState.lastError = null;
        log.info(
          {
            browserId: deps.browserId,
            tools: getCatalog().length,
            initSeconds: Math.round((Date.now() - initState.startedAt) / 1000),
            cycles: cycle,
          },
          'browser-mcp ready (no browser session started yet)',
        );
        return;
      } catch (err) {
        initState.lastError = err.message;
        initState.cycles = cycle;
        log.error(
          { cycle, err: err.message, retryInSeconds: INIT_RETRY_SECONDS },
          'Initialisation attempt failed; will retry',
        );
        await new Promise((r) => setTimeout(r, INIT_RETRY_SECONDS * 1000));
      }
    }
  })();

  let shuttingDown = false;
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ signal, sessions: sessions.size }, 'Shutting down; releasing browser sessions');
    httpServer.close();
    await Promise.allSettled(
      [...sessions.keys()].map((sid) => teardown(sid, `signal-${signal}`)),
    );
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

main().catch((err) => {
  log.error({ err: err.message, stack: err.stack }, 'Fatal startup error');
  process.exit(1);
});
