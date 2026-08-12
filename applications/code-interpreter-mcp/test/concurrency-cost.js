// Opens N sessions, activates each one, holds them open, then reports.
// Used to measure per-session memory cost so resource limits are sized from data.
//
//   BASE_URL=http://localhost:8033 N=5 HOLD_SECONDS=60 node test/concurrency-cost.js
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8033';
const N = Number.parseInt(process.env.N || '5', 10);
const HOLD = Number.parseInt(process.env.HOLD_SECONDS || '60', 10);
const STAGGER = Number.parseInt(process.env.STAGGER_MS || '0', 10);

setTimeout(() => { console.log('SELF TIMEOUT'); process.exit(1); }, (HOLD + 300) * 1000).unref();

const health = async () => (await fetch(`${BASE}/readyz`)).json();
const open = async (i) => {
  const client = new Client({ name: `cost-${i}`, version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
  await client.connect(transport);
  return { client, transport };
};

console.log(`baseline: ${JSON.stringify(await health())}`);

// PARALLEL=1 fires every activation at once, which is the case that exposed a cold-start
// cliff in browser-mcp. The default is sequential, which measures steady-state cost.
const PARALLEL = process.env.PARALLEL === '1';

const activate = async (i) => {
  const t0 = Date.now();
  const s = await open(i);
  // One real call, because activation is what actually starts the AgentCore session.
  let isError = false;
  let failed = null;
  try {
    const r = await s.client.callTool(
      { name: 'executeCode', arguments: { language: 'python', code: `print(${i})` } },
      undefined,
      { timeout: 120000 },
    );
    isError = Boolean(r.isError);
  } catch (err) {
    failed = err.message;
  }
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`  session ${i}: ${failed ? `FAILED after ${dt}s (${failed})` : `activated in ${dt}s isError=${isError}`}`);
  return { s, dt, failed };
};

console.log(`\nactivating ${N} sessions ${PARALLEL ? 'SIMULTANEOUSLY' : 'sequentially'}`);
const sessions = [];
const timings = [];
if (PARALLEL) {
  const results = await Promise.all(Array.from({ length: N }, (_, i) => activate(i)));
  for (const r of results) {
    timings.push(r.dt);
    if (!r.failed) sessions.push(r.s);
  }
} else {
  for (let i = 0; i < N; i += 1) {
    if (STAGGER && i) await new Promise((r) => setTimeout(r, STAGGER));
    const r = await activate(i);
    timings.push(r.dt);
    if (!r.failed) sessions.push(r.s);
  }
}

console.log(`\nactivation times (s): ${timings.join(', ')}`);
console.log(`all ${N} sessions live: ${JSON.stringify(await health())}`);
console.log(`\nholding ${HOLD}s — sample memory now`);
await new Promise((r) => setTimeout(r, HOLD * 1000));

console.log(`after hold: ${JSON.stringify(await health())}`);
for (const s of sessions) {
  await s.transport.terminateSession().catch(() => {});
  await s.client.close().catch(() => {});
}
for (let i = 0; i < 30; i += 1) {
  const h = await health();
  if (h.liveInterpreterSessions === 0) break;
  await new Promise((r) => setTimeout(r, 1000));
}
console.log(`after teardown: ${JSON.stringify(await health())}`);
process.exit(0);
