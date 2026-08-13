// Kills the chrome-devtools-mcp child out from under a live session, then verifies
// the server cleans up and the next tool call transparently re-mints a browser.
//
// Before the onclose wiring this parked the session in a broken state: `client`
// stayed non-null so `active` reported true, every later call failed, and the
// AgentCore session plus the limiter slot stayed held until the idle timer fired.
//
// Usage: CONTAINER=<name> BASE_URL=http://localhost:PORT node test/child-death.js
import { execFileSync } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8099';
const CONTAINER = process.env.CONTAINER;
const KUBECTL_POD = process.env.KUBECTL_POD;
const KUBECTL_CTX = process.env.KUBECTL_CTX || 'peeks-hub';

setTimeout(() => {
  console.log('SELF TIMEOUT');
  process.exit(1);
}, 240000).unref();

function inTarget(cmd) {
  if (CONTAINER) {
    return execFileSync('podman', ['exec', CONTAINER, 'sh', '-c', cmd], { encoding: 'utf8' });
  }
  if (KUBECTL_POD) {
    return execFileSync(
      'kubectl',
      ['--context', KUBECTL_CTX, 'exec', KUBECTL_POD, '-n', 'default', '--', 'sh', '-c', cmd],
      { encoding: 'utf8' },
    );
  }
  throw new Error('set CONTAINER or KUBECTL_POD');
}

const health = async () => (await fetch(`${BASE}/readyz`)).json();
const zombies = () => Number.parseInt(inTarget('ps -o stat 2>/dev/null | grep -c Z || true').trim() || '0', 10);
const children = () =>
  Number.parseInt(inTarget('ps -o args 2>/dev/null | grep -c "[c]hrome-devtools-mcp" || true').trim() || '0', 10);

const z0 = zombies();
console.log(`baseline: zombies=${z0} children=${children()}`);

const client = new Client({ name: 'child-death', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
await client.connect(transport);

await client.callTool(
  { name: 'navigate_page', arguments: { url: 'https://example.com' } },
  undefined,
  { timeout: 120000 },
);
const afterFirst = await health();
console.log(`after first call: live=${afterFirst.liveBrowserSessions} children=${children()}`);
if (afterFirst.liveBrowserSessions !== 1) throw new Error('expected exactly 1 live browser session');

console.log('killing the chrome-devtools-mcp child...');
try {
  inTarget('pkill -f "[c]hrome-devtools-mcp" || true');
} catch {}

// Give the server a moment to notice and clean up.
await new Promise((r) => setTimeout(r, 8000));
const afterKill = await health();
console.log(`after kill: live=${afterKill.liveBrowserSessions} children=${children()} zombies=${zombies()}`);
if (afterKill.liveBrowserSessions !== 0) {
  throw new Error(`session leaked: expected 0 live browser sessions, got ${afterKill.liveBrowserSessions}`);
}

console.log('calling a tool again on the SAME MCP session (must transparently re-mint)...');
const res = await client.callTool(
  { name: 'navigate_page', arguments: { url: 'https://example.com' } },
  undefined,
  { timeout: 120000 },
);
if (res.isError) throw new Error(`recovery call failed: ${JSON.stringify(res).slice(0, 200)}`);
const afterRecover = await health();
console.log(`after recovery: live=${afterRecover.liveBrowserSessions} children=${children()}`);
if (afterRecover.liveBrowserSessions !== 1) throw new Error('expected the session to re-mint a browser');

await transport.terminateSession();
await client.close();
await new Promise((r) => setTimeout(r, 4000));
const z1 = zombies();
const final = await health();
console.log(`after teardown: live=${final.liveBrowserSessions} children=${children()} zombies=${z1}`);
if (z1 > z0) throw new Error(`zombies accumulated: ${z0} -> ${z1} (is tini PID 1?)`);

console.log('\nCHILD DEATH PASS');
process.exit(0);
