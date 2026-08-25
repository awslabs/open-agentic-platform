// Calls EVERY advertised tool with only the arguments the schema marks required.
//
// This exists because of a real defect. The AgentCore service rejects executeCode without
// `language` ("code and language fields are required in argument"), while the schema
// advertised language as optional with a default that nothing implemented. Every earlier
// test passed language explicitly, so the failure only appeared when a model omitted it and
// then answered from its own knowledge while claiming it had used the sandbox.
//
// A tool is only safe to advertise if it works when called the way its schema says it may
// be called. This test asserts exactly that, for all nine.
//
//   BASE_URL=http://localhost:8040 node test/tool-matrix.js
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const BASE = process.env.BASE_URL || 'http://localhost:8031';
const CALL = { timeout: 120000 };

setTimeout(() => { console.log('SELF TIMEOUT'); process.exit(1); }, 300000).unref();

const text = (r) => (r.content || []).map((c) => c.text).join('\n');
const client = new Client({ name: 'tool-matrix', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(`${BASE}/mcp`));
await client.connect(transport);

const advertised = (await client.listTools()).tools;
console.log(`advertised tools: ${advertised.map((t) => t.name).join(', ')}\n`);

const results = [];
const call = async (name, args, label) => {
  try {
    const r = await client.callTool({ name, arguments: args }, undefined, CALL);
    const ok = !r.isError;
    results.push({ name: label || name, ok, detail: ok ? text(r).trim().split('\n')[0].slice(0, 60) : text(r).slice(0, 90) });
    return r;
  } catch (err) {
    results.push({ name: label || name, ok: false, detail: `THREW: ${err.message.slice(0, 80)}` });
    return null;
  }
};

// Minimal arguments only: exactly what each schema marks required.
await call('executeCode', { code: 'print("minimal")' });
await call('executeCommand', { command: 'echo minimal' });
await call('writeFiles', { content: [{ path: 'matrix.txt', text: 'hi' }] });
await call('readFiles', { paths: ['matrix.txt'] });
await call('listFiles', {});                      // directoryPath is NOT required by the schema
await call('removeFiles', { paths: ['matrix.txt'] });

const started = await call('startCommandExecution', { command: 'sleep 2 && echo done' });
const taskId = started?.structuredContent?.taskId;
if (taskId) {
  await call('getTask', { taskId });
  await new Promise((r) => setTimeout(r, 4000));
  await call('stopTask', { taskId }, 'stopTask (on a finished task, error is expected)');
} else {
  results.push({ name: 'getTask', ok: false, detail: 'no taskId returned' });
  results.push({ name: 'stopTask', ok: false, detail: 'no taskId returned' });
}

// Also verify the documented optional parameters actually work when supplied.
await call('executeCode', { code: 'print("py")', language: 'python' }, 'executeCode + language');
await call('executeCode', { code: 'x=1', clearContext: true }, 'executeCode + clearContext');
await call('listFiles', { directoryPath: '' }, 'listFiles + directoryPath');

console.log('results:');
let failures = 0;
for (const r of results) {
  // stopTask on a completed task is expected to report an error, so it is not counted.
  const expected = r.name.startsWith('stopTask (on a finished');
  const mark = r.ok ? 'ok  ' : expected ? 'ok* ' : 'FAIL';
  if (!r.ok && !expected) failures += 1;
  console.log(`  ${mark} ${r.name.padEnd(46)} ${r.detail}`);
}

await transport.terminateSession().catch(() => {});
await client.close().catch(() => {});

const advertisedNotCovered = advertised
  .map((t) => t.name)
  .filter((n) => !results.some((r) => r.name === n || r.name.startsWith(n)));
if (advertisedNotCovered.length) {
  console.log(`\nFAIL: advertised but untested: ${advertisedNotCovered.join(', ')}`);
  process.exit(1);
}

console.log(failures ? `\nTOOL MATRIX FAIL: ${failures} tool(s) broken with minimal arguments` : '\nTOOL MATRIX PASS');
process.exit(failures ? 1 : 0);
