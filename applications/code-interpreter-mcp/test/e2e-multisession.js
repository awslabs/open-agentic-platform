// End-to-end: lazy activation, per-session isolation, result mapping, teardown.
//
// Requires a running server (locally or via port-forward):
//   BASE_URL=http://localhost:8031 node test/e2e-multisession.js
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8031';
const CALL = { timeout: 120000 };

setTimeout(() => {
  console.log('SELF TIMEOUT');
  process.exit(1);
}, 300000).unref();

const health = async () => (await fetch(`${BASE}/readyz`)).json();
const text = (r) => (r.content || []).map((c) => c.text).join('\n');
const fail = (m) => {
  console.log(`FAIL: ${m}`);
  process.exit(1);
};

async function open(name) {
  const client = new Client({ name, version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
  await client.connect(transport);
  return { client, transport };
}

console.log('[1] server ready, nothing provisioned');
let h = await health();
console.log(`   tools=${h.toolsAdvertised} live=${h.liveInterpreterSessions} mcpSessions=${h.mcpSessions}`);
if (h.toolsAdvertised !== 9) fail(`expected 9 tools, got ${h.toolsAdvertised}`);
if (h.liveInterpreterSessions !== 0) fail('expected zero live interpreter sessions at rest');

console.log('\n[2] open session A and list tools (must NOT start an interpreter)');
const A = await open('e2e-A');
const tools = await A.client.listTools();
h = await health();
console.log(`   tools listed=${tools.tools.length} live=${h.liveInterpreterSessions}`);
if (h.liveInterpreterSessions !== 0) fail('tools/list started an interpreter session');

console.log('\n[3] executeCode on A (activates), and state persists across calls');
let r = await A.client.callTool({ name: 'executeCode', arguments: { language: 'python', code: 'secret = 4242\nprint("set")' } }, undefined, CALL);
if (r.isError) fail(`executeCode failed: ${text(r)}`);
r = await A.client.callTool({ name: 'executeCode', arguments: { language: 'python', code: 'print(f"secret={secret}")' } }, undefined, CALL);
if (!text(r).includes('secret=4242')) fail(`state did not persist: ${text(r)}`);
console.log(`   ${text(r).trim()}`);
h = await health();
console.log(`   live=${h.liveInterpreterSessions}`);
if (h.liveInterpreterSessions !== 1) fail('expected exactly one live interpreter session');

console.log('\n[4] session B must NOT see A state (isolation)');
const B = await open('e2e-B');
r = await B.client.callTool({ name: 'executeCode', arguments: { language: 'python', code: 'print("secret" in dir() or "secret" in globals())' } }, undefined, CALL);
const leaked = text(r).toLowerCase().includes('true');
h = await health();
console.log(`   B sees A's variable: ${leaked} | live=${h.liveInterpreterSessions}`);
if (leaked) fail('SESSION LEAK: B can see A state');
if (h.liveInterpreterSessions !== 2) fail(`expected 2 live sessions, got ${h.liveInterpreterSessions}`);

console.log('\n[5] files: write, list, read');
r = await A.client.callTool({ name: 'writeFiles', arguments: { content: [{ path: 'e2e.txt', text: 'hello from e2e' }] } }, undefined, CALL);
if (r.isError) fail(`writeFiles failed: ${text(r)}`);
r = await A.client.callTool({ name: 'readFiles', arguments: { paths: ['e2e.txt'] } }, undefined, CALL);
if (!text(r).includes('hello from e2e')) fail(`readFiles did not return content: ${text(r).slice(0, 200)}`);
console.log(`   read back: ${text(r).replace(/\n/g, ' ').slice(0, 80)}`);

console.log('\n[6] executeCommand (shell)');
r = await A.client.callTool({ name: 'executeCommand', arguments: { command: 'echo shell-ok' } }, undefined, CALL);
if (!text(r).includes('shell-ok')) fail(`executeCommand failed: ${text(r)}`);
console.log(`   ${text(r).trim()}`);

console.log('\n[7] an exception is a tool error, not a transport failure');
r = await A.client.callTool({ name: 'executeCode', arguments: { language: 'python', code: 'raise ValueError("boom")' } }, undefined, CALL);
if (!r.isError) fail('expected isError=true for a raised exception');
if (!text(r).includes('boom')) fail('expected the traceback to reach the caller');
console.log(`   isError=${r.isError}, traceback carried through`);

console.log('\n[8] teardown releases both interpreter sessions');
await A.transport.terminateSession();
await A.client.close();
await B.transport.terminateSession();
await B.client.close();
for (let i = 0; i < 20; i += 1) {
  h = await health();
  if (h.liveInterpreterSessions === 0 && h.mcpSessions === 0) break;
  await new Promise((r2) => setTimeout(r2, 1000));
}
console.log(`   live=${h.liveInterpreterSessions} mcpSessions=${h.mcpSessions}`);
if (h.liveInterpreterSessions !== 0) fail('interpreter sessions leaked after teardown');

console.log('\nE2E PASS');
process.exit(0);
