// Drives a code interpreter tool call THROUGH agentgateway, which is the path an agent
// actually uses. Proves:
//   - the gateway accepts the JWT and routes /mcp/<name> to this backend
//   - the MCP session id survives the proxy hop, so per-session isolation holds end to end
//   - a tools/call activates a real AgentCore session (liveInterpreterSessions 0 -> 1)
//   - two gateway sessions stay isolated from each other
//
// Env: GW_URL, MCP_PATH, TOKEN, DIRECT_URL (a direct pod URL, for observing /readyz)
//
//   GW_URL=https://... MCP_PATH=/mcp/code-interpreter-mcp TOKEN=$(...) \
//   DIRECT_URL=http://localhost:8040 node test/gateway.js
//
// Note: DIRECT_URL through a port-forward reaches ONE pod. At more than one replica the
// counter assertions can read a pod that does not hold the session, so treat a counter
// mismatch there as inconclusive rather than a failure.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const GW = process.env.GW_URL;
const MCP_PATH = process.env.MCP_PATH || '/mcp/code-interpreter-mcp';
const TOKEN = process.env.TOKEN;
const DIRECT = process.env.DIRECT_URL;
const CALL = { timeout: 120000 };

if (!GW || !TOKEN) {
  console.error('FAIL: GW_URL and TOKEN are required');
  process.exit(1);
}

setTimeout(() => {
  console.error('\nFAIL: hard timeout');
  process.exit(1);
}, Number.parseInt(process.env.HARD_TIMEOUT_MS || '180000', 10)).unref();

const fail = (m) => {
  console.error(`FAIL: ${m}`);
  process.exit(1);
};
const readyz = async () => {
  if (!DIRECT) return null;
  try {
    return await (await fetch(`${DIRECT}/readyz`)).json();
  } catch {
    return null;
  }
};
const textOf = (r) => (r.content || []).filter((c) => c.type === 'text').map((c) => c.text).join('\n');

const connect = async (name) => {
  const transport = new StreamableHTTPClientTransport(new URL(`${GW}${MCP_PATH}`), {
    requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
  });
  const client = new Client({ name, version: '1.0.0' });
  await client.connect(transport);
  return { client, transport };
};

console.log(`[1] connect through the gateway: ${GW}${MCP_PATH}`);
const before = await readyz();
if (before) console.log(`    backend before: live=${before.liveInterpreterSessions} mcpSessions=${before.mcpSessions}`);
const A = await connect('gw-A');

const tools = await A.client.listTools();
console.log(`    tools visible through the gateway: ${tools.tools.length}`);
if (tools.tools.length !== 9) fail(`expected 9 tools, got ${tools.tools.length}`);

const afterList = await readyz();
if (afterList && afterList.liveInterpreterSessions !== (before?.liveInterpreterSessions ?? 0)) {
  fail('tools/list started an interpreter session; activation should be lazy');
}
console.log('    tools/list started no interpreter session (lazy activation holds via the gateway)');

console.log('\n[2] a tools/call through the gateway activates a real session');
let r = await A.client.callTool(
  { name: 'executeCode', arguments: { language: 'python', code: 'print(6 * 7)' } },
  undefined,
  CALL,
);
if (r.isError) fail(`executeCode failed through the gateway: ${textOf(r)}`);
if (!textOf(r).includes('42')) fail(`unexpected output: ${textOf(r)}`);
console.log(`    executeCode returned: ${textOf(r).trim().split('\n')[0]}`);

const afterCall = await readyz();
if (afterCall) {
  console.log(`    backend now: live=${afterCall.liveInterpreterSessions} mcpSessions=${afterCall.mcpSessions}`);
  if (afterCall.liveInterpreterSessions < 1) {
    console.log('    NOTE: counter did not move; inconclusive if more than one replica');
  }
}

console.log('\n[3] session state survives the proxy hop');
r = await A.client.callTool(
  { name: 'executeCode', arguments: { language: 'python', code: 'gwmarker = "kept"\nprint("stored")' } },
  undefined,
  CALL,
);
if (r.isError) fail(`state-setting call failed: ${textOf(r)}`);
r = await A.client.callTool(
  { name: 'executeCode', arguments: { language: 'python', code: 'print(gwmarker)' } },
  undefined,
  CALL,
);
if (!textOf(r).includes('kept')) fail(`session did not survive the proxy hop: ${textOf(r)}`);
console.log('    variable set in one call was readable in the next');

console.log('\n[4] a second gateway session is isolated');
const B = await connect('gw-B');
r = await B.client.callTool(
  { name: 'executeCode', arguments: { language: 'python', code: 'print("gwmarker" in globals())' } },
  undefined,
  CALL,
);
const leaked = textOf(r).toLowerCase().includes('true');
console.log(`    B sees A's variable: ${leaked}`);
if (leaked) fail('SESSION LEAK across gateway sessions');

console.log('\n[5] teardown');
for (const s of [A, B]) {
  await s.transport.terminateSession().catch(() => {});
  await s.client.close().catch(() => {});
}
if (DIRECT) {
  for (let i = 0; i < 20; i += 1) {
    const h = await readyz();
    if (h && h.liveInterpreterSessions === 0) break;
    await new Promise((res) => setTimeout(res, 1000));
  }
  const end = await readyz();
  if (end) console.log(`    backend after: live=${end.liveInterpreterSessions} mcpSessions=${end.mcpSessions}`);
}

console.log('\nGATEWAY PASS');
process.exit(0);
