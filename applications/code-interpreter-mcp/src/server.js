/**
 * StreamableHTTP MCP server fronting AgentCore Code Interpreter.
 *
 * Carries over the behaviour that browser-mcp had to learn the hard way:
 *
 *  - Bind the listener BEFORE slow AWS initialisation. Doing it the other way round means
 *    nothing answers /healthz and the liveness probe kills the pod during a cold start.
 *  - /healthz is liveness (200 as soon as the process serves HTTP), /readyz is readiness
 *    (503 until the interpreter is resolved). Separate paths, separate meanings.
 *  - Initialisation retries FOREVER. Giving up leaves the pod alive but permanently
 *    unready, which needs a human; a freshly attached IAM policy can take over five
 *    minutes to propagate.
 *  - Four teardown paths release the AgentCore session: client DELETE, idle eviction,
 *    a reaper for clients that vanish without DELETE, and SIGTERM.
 */

import http from 'node:http';
import express from 'express';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { randomUUID } from 'node:crypto';

import { config } from './config.js';
import { log } from './log.js';
import { makeClients, resolveInterpreterId } from './agentcore.js';
import { InterpreterSession } from './session.js';
import { getCatalog, isKnownTool } from './tools.js';

const INIT_RETRY_MS = config.initRetrySeconds * 1000;

/** Caps concurrent LIVE interpreter sessions. Idle MCP sessions are free. */
function makeLimiter(max) {
  let inUse = 0;
  return {
    acquire() {
      if (inUse >= max) {
        throw new Error(
          `this pod is at its limit of ${max} concurrent interpreter sessions. ` +
            'Retry shortly, or raise MAX_INTERPRETER_SESSIONS.',
        );
      }
      inUse += 1;
    },
    release() {
      inUse = Math.max(0, inUse - 1);
    },
    get live() {
      return inUse;
    },
  };
}

/** Map an AgentCore result onto an MCP tool result. The shapes already align closely. */
function toMcpResult(result) {
  const content = [];
  for (const block of result.content || []) {
    if (block.text !== undefined && block.type !== 'resource' && block.type !== 'resource_link') {
      content.push({ type: 'text', text: block.text });
      continue;
    }
    if (block.type === 'resource_link') {
      content.push({
        type: 'text',
        text: `resource: ${block.uri || block.name || '(unnamed)'}${
          block.size !== undefined ? ` (${block.size} bytes)` : ''
        }`,
      });
      continue;
    }
    if (block.type === 'resource') {
      const inner = block.resource || {};
      const text = inner.text !== undefined ? inner.text : block.text;
      content.push({
        type: 'text',
        text:
          text !== undefined
            ? `${inner.uri || block.uri || 'resource'}:\n${text}`
            : `resource ${inner.uri || block.uri || ''} (${inner.mimeType || block.mimeType || 'binary'})`,
      });
      continue;
    }
    // Anything else: keep it visible rather than silently dropping it.
    content.push({ type: 'text', text: JSON.stringify(block) });
  }
  if (!content.length) content.push({ type: 'text', text: '(no output)' });

  const out = { content, isError: Boolean(result.isError) };
  if (result.structuredContent) out.structuredContent = result.structuredContent;
  return out;
}

function buildMcpServer(interpreterSession, initState) {
  const server = new Server(
    { name: 'code-interpreter-mcp', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    if (!initState.ready) throw new Error('server is still initialising');
    return { tools: getCatalog() };
  });

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    if (!initState.ready) throw new Error('server is still initialising');
    const { name, arguments: args } = req.params;
    if (!isKnownTool(name)) throw new Error(`unknown tool: ${name}`);
    try {
      const result = await interpreterSession.callTool(name, args);
      return toMcpResult(result);
    } catch (err) {
      // Surface as a tool error so the agent can react, rather than killing the session.
      log.warn(
        { mcpSessionId: interpreterSession.mcpSessionId, tool: name, err: err.message },
        'Tool call failed',
      );
      return {
        content: [{ type: 'text', text: `Tool "${name}" failed: ${err.message}` }],
        isError: true,
      };
    }
  });

  return server;
}

async function main() {
  const region = config.region;
  const { dataPlane, controlPlane } = makeClients(region);
  const limiter = makeLimiter(config.maxSessions);
  const deps = { region, dataPlane, interpreterId: null, limiter };
  const initState = { ready: false, lastError: null, cycles: 0, startedAt: Date.now() };

  /** mcpSessionId -> { transport, server, session, lastSeen } */
  const sessions = new Map();

  const app = express();
  app.use(express.json({ limit: '8mb' }));

  app.get('/healthz', (_req, res) => res.status(200).send('ok'));

  app.get('/readyz', (_req, res) => {
    if (!initState.ready) {
      res.status(503).json({
        status: 'initializing',
        lastError: initState.lastError || undefined,
        failedCycles: initState.cycles || undefined,
        elapsedSeconds: Math.round((Date.now() - initState.startedAt) / 1000),
        region,
      });
      return;
    }
    let openTasks = 0;
    for (const entry of sessions.values()) openTasks += entry.session.openTasks.size;
    res.status(200).json({
      status: 'ok',
      interpreterId: deps.interpreterId,
      region,
      mcpSessions: sessions.size,
      liveInterpreterSessions: limiter.live,
      openAsyncTasks: openTasks,
      toolsAdvertised: getCatalog().length,
    });
  });

  const handle = async (req, res) => {
    const sid = req.headers['mcp-session-id'];
    let entry = sid ? sessions.get(sid) : undefined;

    if (!entry) {
      // A request carrying an unknown session id cannot be served: state is pod-local.
      if (sid) {
        res.status(400).json({
          jsonrpc: '2.0',
          error: { code: -32000, message: 'Bad Request: no valid session ID provided' },
          id: null,
        });
        return;
      }
      const newId = randomUUID();
      const session = new InterpreterSession(newId, deps);
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => newId,
        onsessionclosed: (closedId) => {
          const e = sessions.get(closedId);
          if (!e) return;
          sessions.delete(closedId);
          e.session.close('client-closed').catch(() => {});
          log.info({ mcpSessionId: closedId, total: sessions.size }, 'MCP session closed');
        },
      });
      const server = buildMcpServer(session, initState);
      await server.connect(transport);
      entry = { transport, server, session, lastSeen: Date.now() };
      sessions.set(newId, entry);
      log.info({ mcpSessionId: newId, total: sessions.size }, 'MCP session initialised');
    }

    entry.lastSeen = Date.now();
    await entry.transport.handleRequest(req, res, req.body);
  };

  app.post(config.mcpPath, handle);
  app.get(config.mcpPath, handle);
  app.delete(config.mcpPath, handle);

  const httpServer = http.createServer(app);
  await new Promise((resolve) => httpServer.listen(config.port, '0.0.0.0', resolve));
  log.info(
    {
      port: config.port,
      path: config.mcpPath,
      region,
      maxInterpreterSessions: config.maxSessions,
      sessionTimeoutSeconds: config.sessionTimeoutSeconds,
      idleSeconds: config.sessionIdleSeconds,
    },
    'code-interpreter-mcp listening; initialising (not ready yet)',
  );

  // Initialise in the background, retrying forever. Readiness gates traffic and liveness
  // only reports that the process is up, so retrying indefinitely is safe and converges
  // whenever the dependency appears.
  (async function initLoop() {
    for (let cycle = 1; ; cycle += 1) {
      try {
        deps.interpreterId = await resolveInterpreterId(controlPlane, config.interpreterName);
        initState.ready = true;
        initState.lastError = null;
        log.info(
          {
            interpreterId: deps.interpreterId,
            tools: getCatalog().length,
            initSeconds: Math.round((Date.now() - initState.startedAt) / 1000),
            cycles: cycle,
          },
          'code-interpreter-mcp ready (no interpreter session started yet)',
        );
        return;
      } catch (err) {
        initState.lastError = err.message;
        initState.cycles = cycle;
        log.error(
          { cycle, err: err.message, retryInSeconds: config.initRetrySeconds },
          'Initialisation attempt failed; will retry',
        );
        await new Promise((r) => setTimeout(r, INIT_RETRY_MS));
      }
    }
  })();

  // Reap MCP sessions whose client vanished without sending DELETE.
  const reaper = setInterval(() => {
    const cutoff = Date.now() - config.mcpSessionIdleSeconds * 1000;
    for (const [id, entry] of sessions) {
      if (entry.lastSeen > cutoff) continue;
      sessions.delete(id);
      log.info({ mcpSessionId: id, total: sessions.size }, 'Reaping abandoned MCP session');
      entry.session.close('abandoned').catch(() => {});
      entry.transport.close?.();
    }
  }, config.reaperIntervalSeconds * 1000);
  reaper.unref();

  let shuttingDown = false;
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ signal, mcpSessions: sessions.size }, 'Shutting down; releasing sessions');
    clearInterval(reaper);
    await Promise.allSettled(
      [...sessions.values()].map((e) => e.session.close(`shutdown-${signal}`)),
    );
    httpServer.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 10000).unref();
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

main().catch((err) => {
  log.error({ err: err.message, stack: err.stack }, 'Fatal startup error');
  process.exit(1);
});
