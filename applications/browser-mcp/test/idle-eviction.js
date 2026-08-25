/**
 * Verifies the idle lifecycle:
 *   - after SESSION_IDLE_SECONDS with no tool calls, the AgentCore browser
 *     session is released while the MCP session stays alive and usable;
 *   - a later tool call transparently re-mints a fresh browser session.
 *
 * Run the server with a short SESSION_IDLE_SECONDS (e.g. 5).
 * Self-terminating; never hangs.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8010';
const IDLE_WAIT_MS = Number.parseInt(process.env.IDLE_WAIT_MS || '20000', 10);

setTimeout(() => {
  console.error('\nFAIL: hard timeout');
  process.exit(1);
}, Number.parseInt(process.env.HARD_TIMEOUT_MS || '150000', 10));

const fail = (m) => {
  console.error(`FAIL: ${m}`);
  process.exit(1);
};
const readyz = async () => (await fetch(`${BASE}/readyz`)).json();
const textOf = (r) => (r.content || []).filter((c) => c.type === 'text').map((c) => c.text).join('\n');

const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
const client = new Client({ name: 'idle-test', version: '0.0.1' });

try {
  await client.connect(transport);

  console.log('[1] Activate the browser with a tool call');
  const nav = await client.callTool({
    name: 'navigate_page',
    arguments: { type: 'url', url: 'https://example.com/' },
  });
  if (nav.isError) fail(`navigate failed: ${textOf(nav)}`);
  let s = await readyz();
  console.log('   readyz:', JSON.stringify(s));
  if (s.liveBrowserSessions !== 1) fail(`expected 1 live browser, got ${s.liveBrowserSessions}`);

  console.log(`[2] Idle for ${IDLE_WAIT_MS}ms; browser should be released, MCP session kept`);
  await new Promise((r) => setTimeout(r, IDLE_WAIT_MS));
  s = await readyz();
  console.log('   readyz:', JSON.stringify(s));
  if (s.liveBrowserSessions !== 0) fail(`browser not released while idle (live=${s.liveBrowserSessions})`);
  if (s.mcpSessions !== 1) fail(`MCP session should survive idle eviction, got ${s.mcpSessions}`);
  console.log('   OK: browser released, MCP session still alive');

  console.log('[3] tools/list must still work with no browser');
  const listed = await client.listTools();
  if (!(listed.tools.length > 0)) fail('tools/list empty after eviction');
  console.log(`   OK: ${listed.tools.length} tools still advertised`);

  console.log('[4] Next tool call should transparently re-mint a browser');
  const again = await client.callTool({
    name: 'navigate_page',
    arguments: { type: 'url', url: 'https://example.com/' },
  });
  if (again.isError) fail(`re-activation failed: ${textOf(again)}`);
  const snap = textOf(await client.callTool({ name: 'take_snapshot', arguments: {} }));
  if (!/example domain/i.test(snap)) fail('re-minted session did not return real page content');
  s = await readyz();
  console.log('   readyz:', JSON.stringify(s));
  if (s.liveBrowserSessions !== 1) fail(`expected 1 live browser after re-mint, got ${s.liveBrowserSessions}`);
  console.log('   OK: browser transparently re-minted and serving');

  await transport.terminateSession();
  await client.close();
  console.log('\nIDLE PASS');
  process.exit(0);
} catch (err) {
  fail(`unexpected error: ${err?.message || err}`);
}
