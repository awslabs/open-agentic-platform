// Proves the async-task fix: idle eviction must NOT tear down a session that has a
// background task running, because stopping the session kills the task and getTask needs
// the same sessionId, so there would be no way to recover it.
//
// Run the server with a deliberately short idle window:
//   SESSION_IDLE_SECONDS=15 MCP_PORT=8032 node src/server.js
//   BASE_URL=http://localhost:8032 IDLE=15 node test/task-eviction.js
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8032';
const IDLE = Number.parseInt(process.env.IDLE || '15', 10);
const CALL = { timeout: 120000 };

setTimeout(() => { console.log('SELF TIMEOUT'); process.exit(1); }, 300000).unref();

const health = async () => (await fetch(`${BASE}/readyz`)).json();
const text = (r) => (r.content || []).map((c) => c.text).join('\n');
const fail = (m) => { console.log(`FAIL: ${m}`); process.exit(1); };

const client = new Client({ name: 'task-eviction', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
await client.connect(transport);

console.log(`[1] start a background command that outlives the ${IDLE}s idle window`);
let r = await client.callTool(
  { name: 'startCommandExecution', arguments: { command: `sleep ${IDLE * 2} && echo task-survived` } },
  undefined,
  CALL,
);
if (r.isError) fail(`startCommandExecution failed: ${text(r)}`);
const taskId = r.structuredContent?.taskId;
if (!taskId) fail(`no taskId in structuredContent: ${JSON.stringify(r.structuredContent)}`);
console.log(`   taskId=${taskId}`);

let h = await health();
console.log(`   live=${h.liveInterpreterSessions} openAsyncTasks=${h.openAsyncTasks}`);
if (h.openAsyncTasks !== 1) fail('server did not record the open task');

const waitFor = IDLE * 2 + 10;
console.log(`\n[2] make NO calls for ${waitFor}s, well past the idle window`);
console.log('    without the fix the session would be evicted and the task lost');
for (let elapsed = 0; elapsed < waitFor; elapsed += 10) {
  await new Promise((res) => setTimeout(res, 10000));
  h = await health();
  console.log(`    t+${elapsed + 10}s live=${h.liveInterpreterSessions} openAsyncTasks=${h.openAsyncTasks}`);
  if (h.liveInterpreterSessions === 0) fail('session was evicted while a task was running');
}

console.log('\n[3] the task and its output are still retrievable');
r = await client.callTool({ name: 'getTask', arguments: { taskId } }, undefined, CALL);
if (r.isError) fail(`getTask failed after the idle window: ${text(r)}`);
const status = r.structuredContent?.taskStatus;
console.log(`   taskStatus=${status} output=${text(r).trim().slice(0, 60)}`);
if (!text(r).includes('task-survived')) fail(`task output missing, status=${status}`);

console.log('\n[4] once terminal, the session becomes evictable again');
h = await health();
console.log(`   openAsyncTasks=${h.openAsyncTasks}`);
if (h.openAsyncTasks !== 0) fail('completed task was not cleared');
const deadline = Date.now() + (IDLE + 20) * 1000;
while (Date.now() < deadline) {
  await new Promise((res) => setTimeout(res, 5000));
  h = await health();
  if (h.liveInterpreterSessions === 0) break;
}
console.log(`   live=${h.liveInterpreterSessions} after the idle window`);
if (h.liveInterpreterSessions !== 0) fail('session never became evictable after the task finished');

await transport.terminateSession().catch(() => {});
await client.close().catch(() => {});
console.log('\nTASK EVICTION PASS');
process.exit(0);
