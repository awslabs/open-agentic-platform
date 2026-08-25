/**
 * Tool catalog: what this server advertises to agents.
 *
 * Discovered ONCE at boot by running the pinned chrome-devtools-mcp with no
 * browser attached (it starts and answers tools/list without any Chrome and
 * without any AgentCore session), then cached. That keeps the advertised schema
 * self-maintaining against the pinned version, with no idle child process and
 * no AgentCore cost.
 *
 * This is what makes lazy activation possible: an agent can discover and reason
 * about the browser tools, and only pay for a browser when it actually calls one.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

import { config } from './config.js';
import { log } from './log.js';

let cached = null;

function applyFilter(tools) {
  let out = tools;
  if (config.toolsAllow.length > 0) {
    const allow = new Set(config.toolsAllow);
    out = out.filter((t) => allow.has(t.name));
    const missing = config.toolsAllow.filter((n) => !tools.some((t) => t.name === n));
    if (missing.length) log.warn({ missing }, 'TOOLS_ALLOW names not present upstream');
  }
  if (config.toolsDeny.length > 0) {
    const deny = new Set(config.toolsDeny);
    out = out.filter((t) => !deny.has(t.name));
  }
  return out;
}

/** Discover the upstream tool list with no browser and no AgentCore session. */
export async function discoverTools() {
  if (cached) return cached;

  const transport = new StdioClientTransport({
    command: config.cdpCommand,
    // No --wsEndpoint on purpose: discovery must not touch a browser.
    args: ['--headless=true'],
    stderr: 'pipe',
  });
  const client = new Client({ name: 'browser-mcp-discovery', version: '0.1.0' });

  try {
    await client.connect(transport);
    const { tools } = await client.listTools();
    const filtered = applyFilter(tools);
    cached = filtered;
    log.info(
      { upstream: tools.length, advertised: filtered.length },
      'Discovered tool catalog (browser-less)',
    );
    return cached;
  } finally {
    // Discovery child is disposable; nothing stays running at rest.
    await client.close().catch(() => {});
  }
}

export function getCatalog() {
  if (!cached) throw new Error('Tool catalog not initialised');
  return cached;
}

export function isAdvertised(name) {
  return !!cached && cached.some((t) => t.name === name);
}
