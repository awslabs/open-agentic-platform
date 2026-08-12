/**
 * The AgentCore Code Interpreter layer: resolve the interpreter, start and stop sessions,
 * and invoke tools.
 *
 * Simpler than the browser equivalent by a wide margin. AgentCore Browser exposes only a
 * CDP WebSocket that must be SigV4-signed by hand at the upgrade handshake, which is why
 * that server hand-ports a signer and spawns a chrome-devtools-mcp child per session. The
 * Code Interpreter is an ordinary request/response API, so the SDK is the whole client:
 * no signing code, no child processes.
 */

import {
  BedrockAgentCoreClient,
  StartCodeInterpreterSessionCommand,
  StopCodeInterpreterSessionCommand,
  InvokeCodeInterpreterCommand,
} from '@aws-sdk/client-bedrock-agentcore';
import {
  BedrockAgentCoreControlClient,
  ListCodeInterpretersCommand,
} from '@aws-sdk/client-bedrock-agentcore-control';

import { config } from './config.js';
import { log } from './log.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function makeClients(region) {
  return {
    dataPlane: new BedrockAgentCoreClient({ region }),
    controlPlane: new BedrockAgentCoreControlClient({ region }),
  };
}

/**
 * Resolve a configured interpreter NAME to its id, so the OAM app can refer to a stable
 * name while AWS generates "<name>-<suffix>". With no name configured, use the AWS
 * built-in, which requires no provisioning.
 *
 * Retries until the deadline: on a cold deploy the interpreter may still be provisioning,
 * and a freshly attached IAM policy can take minutes to propagate (measured at over five
 * minutes on the browser component).
 */
export async function resolveInterpreterId(controlPlane, name) {
  if (!name) {
    log.info({ interpreterId: config.builtinInterpreterId }, 'Using the AWS built-in code interpreter');
    return config.builtinInterpreterId;
  }

  const deadline = Date.now() + config.readyTimeoutSeconds * 1000;
  let attempt = 0;
  let lastError = null;

  for (;;) {
    attempt += 1;
    try {
      let nextToken;
      do {
        const resp = await controlPlane.send(
          new ListCodeInterpretersCommand({ type: 'CUSTOM', maxResults: 50, nextToken }),
        );
        for (const s of resp.codeInterpreterSummaries || []) {
          if (s.name !== name) continue;
          if (s.status && s.status !== 'READY') {
            lastError = `status is ${s.status}`;
            log.info({ name, status: s.status }, 'Interpreter found but not READY yet');
          } else {
            log.info(
              { name, interpreterId: s.codeInterpreterId, arn: s.codeInterpreterArn },
              'Resolved code interpreter name to id',
            );
            return s.codeInterpreterId;
          }
        }
        nextToken = resp.nextToken;
      } while (nextToken);
      if (!lastError) lastError = 'not present in ListCodeInterpreters yet';
    } catch (err) {
      lastError = err.message;
      log.warn({ attempt, err: err.message }, 'ListCodeInterpreters failed; retrying');
    }

    if (Date.now() > deadline) {
      // Report the cause, not just the timeout: "not READY" alone is misleading when the
      // real problem was AccessDenied while IAM propagated.
      throw new Error(
        `could not resolve code interpreter "${name}" within ` +
          `${config.readyTimeoutSeconds}s: ${lastError}`,
      );
    }
    await sleep(5000);
  }
}

export async function startSession(dataPlane, interpreterId, name) {
  const resp = await dataPlane.send(
    new StartCodeInterpreterSessionCommand({
      codeInterpreterIdentifier: interpreterId,
      name,
      sessionTimeoutSeconds: config.sessionTimeoutSeconds,
    }),
  );
  return { sessionId: resp.sessionId };
}

export async function stopSession(dataPlane, interpreterId, sessionId) {
  await dataPlane.send(
    new StopCodeInterpreterSessionCommand({
      codeInterpreterIdentifier: interpreterId,
      sessionId,
    }),
  );
}

/**
 * Invoke one tool and drain the response stream into an MCP-shaped result.
 *
 * The stream is a union of exactly one `result` member plus one member per exception type
 * (AccessDenied, Conflict, InternalServer, ResourceNotFound, ServiceQuotaExceeded,
 * Throttling, Validation, $Unknown). Anything that is not `result` must be treated as an
 * error, otherwise we would wait for a result frame that never arrives.
 */
export async function invokeTool(dataPlane, interpreterId, sessionId, name, args) {
  const resp = await dataPlane.send(
    new InvokeCodeInterpreterCommand({
      codeInterpreterIdentifier: interpreterId,
      sessionId,
      name,
      arguments: args || {},
    }),
  );

  for await (const event of resp.stream || []) {
    if (event.result) return event.result;
    const key = Object.keys(event).find((k) => event[k] !== undefined);
    const detail = key ? JSON.stringify(event[key]) : 'unknown stream member';
    throw new Error(`code interpreter returned ${key || 'an unknown error'}: ${detail}`);
  }
  throw new Error('code interpreter stream ended without a result');
}
