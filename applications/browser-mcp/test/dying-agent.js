/**
 * Simulates the AGENT POD DYING abruptly (SIGKILL, no graceful shutdown, no
 * DELETE, no chance to close anything).
 *
 * Connects, activates a browser, then hard-kills itself. The shell that runs this
 * then polls /readyz to see whether the server notices the dead peer immediately
 * or only later via its idle/reaper timers.
 *
 * Run the server with LONG idle values so timers cannot mask the result:
 *   SESSION_IDLE_SECONDS=600 MCP_SESSION_IDLE_SECONDS=900 REAPER_INTERVAL_SECONDS=60
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8015';

const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
const client = new Client({ name: 'dying-agent', version: '0.0.1' });

await client.connect(transport);
await client.callTool({
  name: 'navigate_page',
  arguments: { type: 'url', url: 'https://example.com/' },
});

const s = await (await fetch(`${BASE}/readyz`)).json();
console.log(`   activated: mcpSessions=${s.mcpSessions} liveBrowserSessions=${s.liveBrowserSessions}`);
console.log(`   mcp session id = ${transport.sessionId}`);
console.log('   now SIGKILLing self (agent pod death, no cleanup)');

// Hard kill: no DELETE, no close(), no exit handlers.
process.kill(process.pid, 'SIGKILL');
