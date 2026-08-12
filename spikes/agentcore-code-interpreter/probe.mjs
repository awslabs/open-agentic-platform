// Spike: validate the AgentCore Code Interpreter API surface before designing an MCP
// server around it. Answers questions that would otherwise be guesses:
//
//   1. What resource type does a code interpreter ARN use? (the browser turned out to be
//      "browser-custom", not "browser", and following the docs' "browser/*" example
//      produced an IAM policy that denied every call)
//   2. Does the AWS built-in system code interpreter work without provisioning one?
//   3. What does InvokeCodeInterpreter actually return, and how does the stream frame?
//   4. How closely does CodeInterpreterResult map onto an MCP tool result?
//   5. Does session state persist across calls (does a variable survive)?
//
// Run with the SDK already vendored in browser-mcp, so this needs no npm install:
//   NODE_PATH=../../applications/browser-mcp/node_modules node probe.mjs
import {
  BedrockAgentCoreClient,
  StartCodeInterpreterSessionCommand,
  InvokeCodeInterpreterCommand,
  StopCodeInterpreterSessionCommand,
} from '@aws-sdk/client-bedrock-agentcore';
import {
  BedrockAgentCoreControlClient,
  ListCodeInterpretersCommand,
} from '@aws-sdk/client-bedrock-agentcore-control';

const REGION = process.env.AWS_REGION || 'us-west-2';
// The documented built-in, analogous to aws.browser.v1 for the browser.
const IDENTIFIER = process.env.CODE_INTERPRETER_ID || 'aws.codeinterpreter.v1';

setTimeout(() => {
  console.log('SELF TIMEOUT');
  process.exit(1);
}, 180000).unref();

const dp = new BedrockAgentCoreClient({ region: REGION });
const cp = new BedrockAgentCoreControlClient({ region: REGION });

/** Drain the response stream into the same shape an MCP tool result uses. */
async function invoke(sessionId, name, args) {
  const resp = await dp.send(
    new InvokeCodeInterpreterCommand({
      codeInterpreterIdentifier: IDENTIFIER,
      sessionId,
      name,
      arguments: args,
    }),
  );
  const frames = [];
  for await (const ev of resp.stream || []) {
    const key = Object.keys(ev)[0];
    frames.push(key);
    if (ev.result) {
      const texts = (ev.result.content || [])
        .filter((c) => c.text)
        .map((c) => c.text);
      return {
        frames,
        isError: Boolean(ev.result.isError),
        blockTypes: (ev.result.content || []).map((c) => c.type),
        text: texts.join('\n').slice(0, 300),
        structured: ev.result.structuredContent ? Object.keys(ev.result.structuredContent) : null,
      };
    }
    // Any non-result member is an exception member: surface it rather than hanging.
    if (key !== 'result') return { frames, error: JSON.stringify(ev[key]).slice(0, 300) };
  }
  return { frames, error: 'stream ended with no result frame' };
}

console.log('[1] what code interpreters exist, and what ARN shape do they use?');
try {
  for (const type of ['CUSTOM', 'SYSTEM']) {
    const r = await cp.send(new ListCodeInterpretersCommand({ type, maxResults: 20 }));
    for (const s of r.codeInterpreterSummaries || []) {
      console.log(`   ${type}: ${s.name} -> ${s.codeInterpreterArn || s.codeInterpreterId}`);
    }
  }
} catch (err) {
  console.log('   ListCodeInterpreters failed:', err.message.slice(0, 200));
}

console.log(`\n[2] start a session on ${IDENTIFIER}`);
let sessionId;
try {
  const s = await dp.send(
    new StartCodeInterpreterSessionCommand({
      codeInterpreterIdentifier: IDENTIFIER,
      name: 'spike-probe',
      sessionTimeoutSeconds: 300,
    }),
  );
  sessionId = s.sessionId;
  console.log('   sessionId:', sessionId);
} catch (err) {
  console.log('   FAILED:', err.name, err.message.slice(0, 300));
  process.exit(1);
}

try {
  console.log('\n[3] executeCode (python)');
  console.log('  ', JSON.stringify(await invoke(sessionId, 'executeCode', {
    language: 'python',
    code: 'x = 6 * 7\nprint(f"answer={x}")',
  })));

  console.log('\n[4] does session state persist across calls?');
  console.log('  ', JSON.stringify(await invoke(sessionId, 'executeCode', {
    language: 'python',
    code: 'print(f"x is still {x}")',
  })));

  console.log('\n[5] executeCommand (shell)');
  console.log('  ', JSON.stringify(await invoke(sessionId, 'executeCommand', {
    command: 'echo hello && python3 --version',
  })));

  console.log('\n[6] writeFiles then listFiles');
  console.log('   write:', JSON.stringify(await invoke(sessionId, 'writeFiles', {
    content: [{ path: 'spike.txt', text: 'written by the spike' }],
  })));
  console.log('   list: ', JSON.stringify(await invoke(sessionId, 'listFiles', { directoryPath: '' })));
  console.log('   read: ', JSON.stringify(await invoke(sessionId, 'readFiles', { paths: ['spike.txt'] })));

  console.log('\n[7] error shape: deliberately broken code');
  console.log('  ', JSON.stringify(await invoke(sessionId, 'executeCode', {
    language: 'python',
    code: 'raise ValueError("boom")',
  })));
} finally {
  console.log('\n[8] stop session');
  try {
    await dp.send(
      new StopCodeInterpreterSessionCommand({
        codeInterpreterIdentifier: IDENTIFIER,
        sessionId,
      }),
    );
    console.log('   stopped');
  } catch (err) {
    console.log('   stop failed:', err.message.slice(0, 200));
  }
}
process.exit(0);
