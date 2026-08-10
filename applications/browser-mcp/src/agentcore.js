/**
 * AgentCore Browser access: resolve the browser, start/stop sessions, and mint
 * the SigV4-signed CDP WebSocket endpoint.
 *
 * Automation is only exposed by AgentCore as a CDP WebSocket, and it is
 * authorized with SigV4 at the HTTP upgrade handshake. The signing below is a
 * deliberate port of the Python bedrock-agentcore SDK's
 * BrowserClient.generate_ws_headers(), which is the behaviour proven to work
 * against a live AgentCore browser:
 *
 *   host = bedrock-agentcore.<region>.amazonaws.com
 *   path = /browser-streams/<browserId>/sessions/<sessionId>/automation
 *   sign a GET of https://host+path (service "bedrock-agentcore") carrying only
 *   the `host` and `x-amz-date` headers, then send Authorization/X-Amz-Date
 *   (+ X-Amz-Security-Token for temporary credentials) on the ws upgrade.
 *
 * `applyChecksum: false` keeps the signed header set to `host;x-amz-date`, which
 * matches the Python reference (botocore does not add x-amz-content-sha256 here).
 */

import crypto from 'node:crypto';

import {
  BedrockAgentCoreClient,
  StartBrowserSessionCommand,
  StopBrowserSessionCommand,
} from '@aws-sdk/client-bedrock-agentcore';
import {
  BedrockAgentCoreControlClient,
  ListBrowsersCommand,
} from '@aws-sdk/client-bedrock-agentcore-control';
import { fromNodeProviderChain } from '@aws-sdk/credential-providers';
import { Sha256 } from '@aws-crypto/sha256-js';
import { HttpRequest } from '@smithy/protocol-http';
import { SignatureV4 } from '@smithy/signature-v4';

import { config } from './config.js';
import { log } from './log.js';

const SERVICE = 'bedrock-agentcore';

export function resolveRegion() {
  const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION;
  if (!region) {
    throw new Error(
      'No AWS region available. AWS_REGION should be injected by the platform ' +
        '(aws-service-identity trait) or the environment.',
    );
  }
  return region;
}

export function dataPlaneHost(region) {
  return `${SERVICE}.${region}.amazonaws.com`;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Resolve which AgentCore browser id to use.
 *
 * Precedence: explicit AGENTCORE_BROWSER_ID > built-in `aws.*` id used directly >
 * resolve a CUSTOM browser by name, retrying while Crossplane provisions it.
 */
export async function resolveBrowserId(region) {
  if (config.browserId) {
    log.info({ browserId: config.browserId }, 'Using explicit AGENTCORE_BROWSER_ID');
    return config.browserId;
  }

  const name = config.browserName;
  if (name.startsWith('aws.')) {
    log.info({ browserId: name }, 'Using built-in AgentCore browser');
    return name;
  }

  const client = new BedrockAgentCoreControlClient({ region });
  const deadline = Date.now() + config.browserReadyTimeoutSeconds * 1000;
  let attempt = 0;
  let lastError = null;

  for (;;) {
    attempt += 1;
    try {
      let nextToken;
      do {
        const resp = await client.send(
          new ListBrowsersCommand({ type: 'CUSTOM', maxResults: 50, nextToken }),
        );
        for (const b of resp.browserSummaries || []) {
          if (b.name === name) {
            // READY is required before a session can be started.
            if (b.status && b.status !== 'READY') {
              lastError = `browser status is ${b.status}`;
              log.info({ name, status: b.status }, 'Browser found but not READY yet');
            } else {
              log.info({ name, browserId: b.browserId }, 'Resolved browser name to id');
              return b.browserId;
            }
          }
        }
        nextToken = resp.nextToken;
      } while (nextToken);
      if (!lastError) lastError = 'browser not present in ListBrowsers yet';
    } catch (err) {
      lastError = err.message;
      log.warn({ attempt, err: err.message }, 'ListBrowsers failed; retrying');
    }

    if (Date.now() > deadline) {
      // Report the underlying cause, not just the timeout. "not READY" alone is
      // misleading when the real problem was AccessDenied while IAM propagated.
      throw new Error(
        `could not resolve AgentCore browser "${name}" within ` +
          `${config.browserReadyTimeoutSeconds}s: ${lastError}`,
      );
    }
    await sleep(5000);
  }
}

export function makeDataPlaneClient(region) {
  return new BedrockAgentCoreClient({ region });
}

export async function startSession(client, browserId, name) {
  const resp = await client.send(
    new StartBrowserSessionCommand({
      browserIdentifier: browserId,
      name,
      sessionTimeoutSeconds: config.sessionTimeoutSeconds,
    }),
  );
  return { sessionId: resp.sessionId, createdAt: resp.createdAt };
}

export async function stopSession(client, browserId, sessionId) {
  await client.send(
    new StopBrowserSessionCommand({ browserIdentifier: browserId, sessionId }),
  );
}

/**
 * Build the CDP WebSocket URL and the SigV4-signed upgrade headers for a session.
 * Returns { wsUrl, headers } shaped exactly like the Python SDK's proven output.
 */
export async function signWsHeaders(region, browserId, sessionId) {
  const host = dataPlaneHost(region);
  const path = `/browser-streams/${browserId}/sessions/${sessionId}/automation`;
  const wsUrl = `wss://${host}${path}`;

  const signer = new SignatureV4({
    credentials: fromNodeProviderChain(),
    region,
    service: SERVICE,
    sha256: Sha256,
    applyChecksum: false,
  });

  const signed = await signer.sign(
    new HttpRequest({
      method: 'GET',
      protocol: 'https:',
      hostname: host,
      path,
      headers: { host },
    }),
  );

  // Smithy lowercases header names; read them case-insensitively.
  const pick = (wanted) => {
    const hit = Object.entries(signed.headers).find(
      ([k]) => k.toLowerCase() === wanted,
    );
    return hit ? hit[1] : undefined;
  };

  const authorization = pick('authorization');
  const amzDate = pick('x-amz-date');
  const securityToken = pick('x-amz-security-token');

  if (!authorization || !amzDate) {
    throw new Error('SigV4 signing did not produce Authorization/X-Amz-Date headers');
  }

  const headers = {
    Host: host,
    'X-Amz-Date': amzDate,
    Authorization: authorization,
    Upgrade: 'websocket',
    Connection: 'Upgrade',
    'Sec-WebSocket-Version': '13',
    'Sec-WebSocket-Key': crypto.randomBytes(16).toString('base64'),
    'User-Agent': `BrowserSandbox-Client/1.0 (Session: ${sessionId})`,
  };

  // Present for Pod Identity / any temporary credentials; absent for long-term keys.
  if (securityToken) headers['X-Amz-Security-Token'] = securityToken;

  return { wsUrl, headers };
}
