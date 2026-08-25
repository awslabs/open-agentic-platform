// Opens N concurrent MCP sessions, activates a real browser on each, then holds them
// open so pod memory can be measured. Self-timeouts so it can never hang the shell.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8099';
const N = Number.parseInt(process.env.N || '5', 10);
const HOLD_MS = Number.parseInt(process.env.HOLD_MS || '90000', 10);
const STAGGER_MS = Number.parseInt(process.env.STAGGER_MS || '0', 10);
const CALL_TIMEOUT = Number.parseInt(process.env.CALL_TIMEOUT || '180000', 10);

setTimeout(() => {
  console.log('SELF TIMEOUT');
  process.exit(1);
}, HOLD_MS + 180000).unref();

async function openSession(i) {
  if (STAGGER_MS) await new Promise((r) => setTimeout(r, STAGGER_MS * (i - 1)));
  const t = Date.now();
  const client = new Client({ name: `load-${i}`, version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
  await client.connect(transport);
  // Activating requires a real tool call: this is what mints the AgentCore session
  // and spawns the child process.
  await client.callTool(
    { name: 'navigate_page', arguments: { url: 'https://example.com' } },
    undefined,
    { timeout: CALL_TIMEOUT },
  );
  console.log(`  session ${i} active after ${((Date.now() - t) / 1000).toFixed(1)}s`);
  return { client, transport };
}

const t0 = Date.now();
const results = await Promise.allSettled(
  Array.from({ length: N }, (_, i) => openSession(i + 1)),
);
const ok = results.filter((r) => r.status === 'fulfilled');
const failed = results.filter((r) => r.status === 'rejected');
console.log(`OPENED ${ok.length}/${N} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
for (const f of failed) console.log('  FAILED:', String(f.reason).slice(0, 160));

const health = await (await fetch(`${BASE}/readyz`)).json();
console.log('BACKEND', JSON.stringify(health));
console.log(`HOLDING ${ok.length} sessions for ${HOLD_MS / 1000}s — measure now`);
await new Promise((r) => setTimeout(r, HOLD_MS));

for (const r of ok) {
  try {
    await r.value.transport.terminateSession();
    await r.value.client.close();
  } catch {}
}
console.log('CLOSED');
const after = await (await fetch(`${BASE}/readyz`)).json();
console.log('BACKEND AFTER', JSON.stringify(after));
process.exit(0);
