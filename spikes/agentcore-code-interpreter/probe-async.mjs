// Spike 2: the async task trio (startCommandExecution / getTask / stopTask).
//
// These are being exposed as MCP tools, so the schemas and the lifecycle must be facts.
// Questions:
//   1. What does startCommandExecution return, and where is the taskId?
//   2. What does getTask return while running vs after completion? Where is the output?
//   3. Does stopTask work on a running task?
//   4. Does a task keep running independently of further calls? (this decides whether
//      our idle-eviction timer would kill it)
import {
  BedrockAgentCoreClient,
  StartCodeInterpreterSessionCommand,
  InvokeCodeInterpreterCommand,
  StopCodeInterpreterSessionCommand,
} from '@aws-sdk/client-bedrock-agentcore';

const REGION = process.env.AWS_REGION || 'us-west-2';
const IDENTIFIER = process.env.CODE_INTERPRETER_ID || 'aws.codeinterpreter.v1';
setTimeout(() => { console.log('SELF TIMEOUT'); process.exit(1); }, 240000).unref();

const dp = new BedrockAgentCoreClient({ region: REGION });

async function invoke(sessionId, name, args) {
  const resp = await dp.send(new InvokeCodeInterpreterCommand({
    codeInterpreterIdentifier: IDENTIFIER, sessionId, name, arguments: args,
  }));
  for await (const ev of resp.stream || []) {
    if (ev.result) return ev.result;
    const key = Object.keys(ev)[0];
    return { __error: key, detail: JSON.stringify(ev[key]).slice(0, 200) };
  }
  return { __error: 'no result frame' };
}

const summarise = (r) => {
  if (r.__error) return `ERROR ${r.__error}: ${r.detail || ''}`;
  const texts = (r.content || []).filter((c) => c.text).map((c) => c.text).join(' | ');
  return JSON.stringify({
    isError: Boolean(r.isError),
    blocks: (r.content || []).map((c) => c.type),
    text: texts.slice(0, 220),
    structured: r.structuredContent || null,
  });
};

const s = await dp.send(new StartCodeInterpreterSessionCommand({
  codeInterpreterIdentifier: IDENTIFIER, name: 'spike-async', sessionTimeoutSeconds: 600,
}));
const sid = s.sessionId;
console.log('session:', sid);

try {
  console.log('\n[1] startCommandExecution: sleep 20 then echo');
  const started = await invoke(sid, 'startCommandExecution', {
    command: 'sleep 20 && echo finished-at-$(date +%S)',
  });
  console.log('  ', summarise(started));
  const taskId =
    started.structuredContent?.taskId ||
    (started.content || []).map((c) => c.text).join(' ').match(/[0-9a-fA-F-]{8,}/)?.[0];
  console.log('   extracted taskId:', taskId);

  if (taskId) {
    console.log('\n[2] getTask immediately (expect running)');
    console.log('  ', summarise(await invoke(sid, 'getTask', { taskId })));

    console.log('\n[3] wait 25s with NO calls, then getTask (does the task survive idle?)');
    await new Promise((r) => setTimeout(r, 25000));
    console.log('  ', summarise(await invoke(sid, 'getTask', { taskId })));

    console.log('\n[4] stopTask on an already-finished task');
    console.log('  ', summarise(await invoke(sid, 'stopTask', { taskId })));

    console.log('\n[5] start another, then stopTask while running');
    const t2 = await invoke(sid, 'startCommandExecution', { command: 'sleep 120' });
    const id2 = t2.structuredContent?.taskId ||
      (t2.content || []).map((c) => c.text).join(' ').match(/[0-9a-fA-F-]{8,}/)?.[0];
    console.log('   taskId2:', id2);
    if (id2) {
      console.log('   stop:', summarise(await invoke(sid, 'stopTask', { taskId: id2 })));
      console.log('   get: ', summarise(await invoke(sid, 'getTask', { taskId: id2 })));
    }
  }
} finally {
  await dp.send(new StopCodeInterpreterSessionCommand({
    codeInterpreterIdentifier: IDENTIFIER, sessionId: sid,
  })).catch(() => {});
  console.log('\nsession stopped');
}
process.exit(0);
