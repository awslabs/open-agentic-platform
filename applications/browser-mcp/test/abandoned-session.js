/**
 * Verifies that an ABANDONED MCP session releases resources.
 *
 * A client can vanish without sending DELETE (pod killed, network gone, agent
 * crash). This test deliberately uses raw HTTP instead of the MCP SDK so that no
 * client-side cleanup runs: it initialises a session, activates a browser, then
 * simply stops talking. The server's reaper must release the AgentCore browser
 * session and drop the MCP session on its own.
 *
 * Run the server with short values, e.g.
 *   MCP_SESSION_IDLE_SECONDS=5 REAPER_INTERVAL_SECONDS=5
 *
 * Self-terminating; never hangs.
 */

const BASE = process.env.BASE_URL || 'http://localhost:8012';
const MCP = `${BASE}/mcp`;

setTimeout(() => {
  console.error('\nFAIL: hard timeout');
  process.exit(1);
}, Number.parseInt(process.env.HARD_TIMEOUT_MS || '150000', 10));

const fail = (m) => {
  console.error(`FAIL: ${m}`);
  process.exit(1);
};
const readyz = async () => (await fetch(`${BASE}/readyz`)).json();

const HEADERS = {
  'content-type': 'application/json',
  accept: 'application/json, text/event-stream',
};

/** Parse either a plain JSON body or an SSE frame carrying one JSON message. */
async function parseBody(res) {
  const raw = await res.text();
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('text/event-stream')) {
    const line = raw.split('\n').find((l) => l.startsWith('data:'));
    return line ? JSON.parse(line.slice(5).trim()) : null;
  }
  return raw ? JSON.parse(raw) : null;
}

async function rpc(body, sessionId) {
  const res = await fetch(MCP, {
    method: 'POST',
    headers: sessionId ? { ...HEADERS, 'mcp-session-id': sessionId } : HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return { sid: res.headers.get('mcp-session-id'), msg: await parseBody(res) };
}

try {
  console.log('[1] Initialise a session over raw HTTP');
  const init = await rpc({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'abandon-test', version: '0.0.1' },
    },
  });
  const sid = init.sid;
  if (!sid) fail('server did not return an mcp-session-id');
  console.log(`   session = ${sid}`);

  await fetch(MCP, {
    method: 'POST',
    headers: { ...HEADERS, 'mcp-session-id': sid },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
  });

  console.log('[2] Activate a browser with a real tool call');
  const call = await rpc(
    {
      jsonrpc: '2.0',
      id: 2,
      method: 'tools/call',
      params: { name: 'navigate_page', arguments: { type: 'url', url: 'https://example.com/' } },
    },
    sid,
  );
  if (call.msg?.result?.isError) fail(`navigate failed: ${JSON.stringify(call.msg.result)}`);
  let s = await readyz();
  console.log('   readyz:', JSON.stringify(s));
  if (s.liveBrowserSessions !== 1) fail(`expected 1 live browser, got ${s.liveBrowserSessions}`);

  console.log('[3] Abandon it: no DELETE, no further requests. Waiting for the reaper.');
  const deadline = Date.now() + 90_000;
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    s = await readyz();
    if (s.liveBrowserSessions === 0 && s.mcpSessions === 0) {
      console.log('   readyz:', JSON.stringify(s));
      console.log('   OK: reaper released the browser and dropped the abandoned session');
      console.log('\nABANDON PASS');
      process.exit(0);
    }
    if (Date.now() > deadline) {
      fail(`abandoned session never reaped: ${JSON.stringify(s)}`);
    }
  }
} catch (err) {
  fail(`unexpected error: ${err?.message || err}`);
}
