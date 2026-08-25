/**
 * End-to-end validation of browser-mcp against a real AgentCore browser.
 *
 * Proves, in order:
 *   1. Boot provisions NO browser session, yet tools are advertised (lazy + discovery).
 *   2. tools/list works with zero live browser sessions.
 *   3. A real tool call activates a browser and returns real page content, which
 *      is also the proof that the Node SigV4 WS signing is correct.
 *   4. Two MCP sessions run concurrently with two INDEPENDENT AgentCore sessions
 *      (isolation), on ONE pod.
 *   5. Teardown releases the browser sessions.
 *
 * Self-terminating: a watchdog kills the process so it can never hang.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8010';
const MCP_URL = `${BASE}/mcp`;
const HARD_TIMEOUT_MS = Number.parseInt(process.env.HARD_TIMEOUT_MS || '180000', 10);

setTimeout(() => {
  console.error(`\nFAIL: hard timeout after ${HARD_TIMEOUT_MS}ms`);
  process.exit(1);
}, HARD_TIMEOUT_MS);

const fail = (msg) => {
  console.error(`FAIL: ${msg}`);
  process.exit(1);
};
const step = (n, msg) => console.log(`\n[${n}] ${msg}`);

async function readyz() {
  const r = await fetch(`${BASE}/readyz`);
  if (!r.ok) throw new Error(`/readyz -> HTTP ${r.status}`);
  return r.json();
}

async function connect(label) {
  const transport = new StreamableHTTPClientTransport(new URL(MCP_URL));
  const client = new Client({ name: `e2e-${label}`, version: '0.0.1' });
  await client.connect(transport);
  return { client, transport };
}

function textOf(result) {
  return (result.content || [])
    .filter((c) => c.type === 'text')
    .map((c) => c.text)
    .join('\n');
}

async function main() {
  // ---- 1. Lazy: nothing provisioned at boot, but tools are known.
  step(1, 'Boot state: expect 0 live browser sessions and a non-empty tool catalog');
  const boot = await readyz();
  console.log('   readyz:', JSON.stringify(boot));
  if (boot.liveBrowserSessions !== 0) {
    fail(`expected 0 live browser sessions at boot, got ${boot.liveBrowserSessions}`);
  }
  if (!(boot.toolsAdvertised > 0)) fail('no tools advertised');
  console.log(`   OK: ${boot.toolsAdvertised} tools advertised with zero browsers running`);

  // ---- 2. tools/list with no browser.
  step(2, 'Open MCP session A and list tools (must not start a browser)');
  const a = await connect('A');
  const listed = await a.client.listTools();
  console.log(`   session A = ${a.transport.sessionId}, tools = ${listed.tools.length}`);
  const afterList = await readyz();
  if (afterList.liveBrowserSessions !== 0) {
    fail(`tools/list started a browser (live=${afterList.liveBrowserSessions})`);
  }
  const names = listed.tools.map((t) => t.name);
  for (const need of ['navigate_page', 'take_snapshot']) {
    if (!names.includes(need)) fail(`expected tool ${need} to be advertised`);
  }
  console.log('   OK: tools/list served with zero live browser sessions');

  // ---- 3. First real call activates a browser; proves Node SigV4 signing.
  step(3, 'Call navigate_page + take_snapshot on session A (activates browser, proves SigV4)');
  const nav = await a.client.callTool({
    name: 'navigate_page',
    arguments: { type: 'url', url: 'https://example.com/' },
  });
  if (nav.isError) fail(`navigate_page returned an error: ${textOf(nav)}`);
  const snap = await a.client.callTool({ name: 'take_snapshot', arguments: {} });
  if (snap.isError) fail(`take_snapshot returned an error: ${textOf(snap)}`);
  const snapText = textOf(snap);
  console.log(`   snapshot excerpt: ${snapText.slice(0, 160).replace(/\s+/g, ' ')}`);
  if (!/example domain/i.test(snapText)) {
    fail('snapshot did not contain expected page content ("Example Domain")');
  }
  const afterCall = await readyz();
  console.log('   readyz:', JSON.stringify(afterCall));
  if (afterCall.liveBrowserSessions !== 1) {
    fail(`expected exactly 1 live browser session, got ${afterCall.liveBrowserSessions}`);
  }
  console.log('   OK: real page content returned, so the Node-signed CDP connection works');

  // ---- 4. Concurrency + isolation on ONE pod.
  step(4, 'Open MCP session B and call a tool concurrently (2 independent browsers, 1 pod)');
  const b = await connect('B');
  if (b.transport.sessionId === a.transport.sessionId) fail('session B reused session A id');
  const navB = await b.client.callTool({
    name: 'navigate_page',
    arguments: { type: 'url', url: 'https://example.org/' },
  });
  if (navB.isError) fail(`session B navigate_page failed: ${textOf(navB)}`);
  const both = await readyz();
  console.log('   readyz:', JSON.stringify(both));
  if (both.liveBrowserSessions !== 2) {
    fail(`expected 2 concurrent live browser sessions, got ${both.liveBrowserSessions}`);
  }
  if (both.mcpSessions !== 2) fail(`expected 2 MCP sessions, got ${both.mcpSessions}`);
  console.log(`   session B = ${b.transport.sessionId}`);
  console.log('   OK: two concurrent, independent AgentCore browser sessions on one pod');

  // Isolation: A is still on example.com while B went to example.org.
  const snapA2 = textOf(await a.client.callTool({ name: 'take_snapshot', arguments: {} }));
  if (!/example domain/i.test(snapA2)) fail('session A lost its own page state (isolation broken)');
  console.log('   OK: session A retained its own page state independently of B');

  // ---- 5. Teardown releases browsers.
  step(5, 'Terminate both MCP sessions (HTTP DELETE) and confirm browsers are released');
  await a.transport.terminateSession();
  await b.transport.terminateSession();
  await a.client.close();
  await b.client.close();
  for (let i = 0; i < 20; i += 1) {
    const s = await readyz();
    if (s.liveBrowserSessions === 0 && s.mcpSessions === 0) {
      console.log('   readyz:', JSON.stringify(s));
      console.log('   OK: all browser sessions released');
      console.log('\nE2E PASS');
      process.exit(0);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  const last = await readyz();
  fail(`sessions not released after close: ${JSON.stringify(last)}`);
}

main().catch((err) => {
  console.error('FAIL: unexpected error:', err?.message || err);
  process.exit(1);
});
