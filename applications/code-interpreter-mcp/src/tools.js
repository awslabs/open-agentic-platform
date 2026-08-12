/**
 * The tool catalog.
 *
 * Hand-authored, unlike browser-mcp which discovers its 29 tools from the
 * chrome-devtools-mcp child at boot. The shapes come from the SDK's ToolArguments type and
 * were confirmed against the live service in spikes/agentcore-code-interpreter.
 *
 * Descriptions carry more weight here than tool count does. Two pairs overlap, and the
 * descriptions are where that gets resolved:
 *
 *   executeCommand vs startCommandExecution  same input, sync vs async
 *   executeCode    vs the file tools         code can read and write files itself
 *
 * So each description says when NOT to use the tool, and dependent tools state their
 * precondition. `taskStatus` values observed from the service: submitted, working,
 * completed, canceled.
 */

import { config } from './config.js';

/** Tools that hand out or consume an async task id, used for eviction suppression. */
export const TASK_STARTING_TOOL = 'startCommandExecution';
export const TASK_POLLING_TOOLS = new Set(['getTask', 'stopTask']);

const LANGUAGES = ['python', 'javascript', 'typescript'];
const RUNTIMES = ['python', 'nodejs', 'deno'];

const ALL_TOOLS = [
  {
    name: 'executeCode',
    description:
      'Run code in the sandbox and return its output. The session keeps state between ' +
      'calls, so variables and imports from earlier calls are still available, and files ' +
      'written earlier are still on disk. Use this for anything computational. Returns ' +
      'stdout, stderr, exit code and execution time; a raised exception comes back as an ' +
      'error with the traceback rather than as a failure of the call.',
    inputSchema: {
      type: 'object',
      properties: {
        code: { type: 'string', description: 'Source to execute.' },
        language: {
          type: 'string',
          enum: LANGUAGES,
          description: 'Language of `code`. Defaults to python.',
        },
        clearContext: {
          type: 'boolean',
          description:
            'Discard variables and imports from previous calls before running. Files on ' +
            'disk are unaffected.',
        },
      },
      required: ['code'],
    },
  },
  {
    name: 'executeCommand',
    description:
      'Run a shell command in the sandbox and wait for it to finish. This is the right ' +
      'choice for almost every command. Use startCommandExecution instead only when the ' +
      'command is expected to run for minutes, since this call blocks until completion.',
    inputSchema: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Shell command to run.' },
      },
      required: ['command'],
    },
  },
  {
    name: 'startCommandExecution',
    description:
      'Start a shell command in the background and return a taskId immediately, without ' +
      'waiting. Only use this for long-running work such as a build or a large download; ' +
      'prefer executeCommand otherwise. Poll with getTask, and cancel with stopTask. The ' +
      'task cannot outlive its session, which is bounded by the session timeout.',
    inputSchema: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Shell command to run in the background.' },
      },
      required: ['command'],
    },
  },
  {
    name: 'getTask',
    description:
      'Check a background command started by startCommandExecution. Requires a taskId ' +
      'returned by that tool. Reports taskStatus as submitted, working, completed or ' +
      'canceled, and once finished includes stdout, stderr and the exit code.',
    inputSchema: {
      type: 'object',
      properties: { taskId: { type: 'string', description: 'Task id from startCommandExecution.' } },
      required: ['taskId'],
    },
  },
  {
    name: 'stopTask',
    description:
      'Cancel a background command started by startCommandExecution. Requires its taskId. ' +
      'Cancelling a task that has already finished returns an error, which is expected ' +
      'rather than a fault.',
    inputSchema: {
      type: 'object',
      properties: { taskId: { type: 'string', description: 'Task id from startCommandExecution.' } },
      required: ['taskId'],
    },
  },
  {
    name: 'writeFiles',
    description:
      'Write files into the sandbox. Useful for supplying input data or a script before ' +
      'running it. Paths are relative to the session working directory.',
    inputSchema: {
      type: 'object',
      properties: {
        content: {
          type: 'array',
          description: 'Files to write.',
          items: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Destination path.' },
              text: { type: 'string', description: 'File contents.' },
            },
            required: ['path', 'text'],
          },
        },
      },
      required: ['content'],
    },
  },
  {
    name: 'readFiles',
    description:
      'Read files from the sandbox, for example an artifact produced by executeCode. ' +
      'Returns the contents as resource blocks.',
    inputSchema: {
      type: 'object',
      properties: {
        paths: {
          type: 'array',
          items: { type: 'string' },
          description: 'Paths to read.',
        },
      },
      required: ['paths'],
    },
  },
  {
    name: 'listFiles',
    description:
      'List files in a sandbox directory. Use an empty directoryPath for the session ' +
      'working directory.',
    inputSchema: {
      type: 'object',
      properties: {
        directoryPath: {
          type: 'string',
          description: 'Directory to list. Empty string means the working directory.',
        },
      },
    },
  },
  {
    name: 'removeFiles',
    description: 'Delete files from the sandbox.',
    inputSchema: {
      type: 'object',
      properties: {
        paths: {
          type: 'array',
          items: { type: 'string' },
          description: 'Paths to delete.',
        },
      },
      required: ['paths'],
    },
  },
];

/** Runtimes are accepted by the service but not surfaced as a tool parameter today. */
export const SUPPORTED_RUNTIMES = RUNTIMES;

let cached = null;

/**
 * The advertised catalog, after allow/deny filtering. Filtering saves agent context; the
 * gateway's authPolicy is what actually enforces which tools a caller may invoke.
 */
export function getCatalog() {
  if (cached) return cached;
  let tools = ALL_TOOLS;
  if (config.toolsAllow.length) {
    const allow = new Set(config.toolsAllow);
    tools = tools.filter((t) => allow.has(t.name));
  }
  if (config.toolsDeny.length) {
    const deny = new Set(config.toolsDeny);
    tools = tools.filter((t) => !deny.has(t.name));
  }
  cached = tools;
  return cached;
}

export const isKnownTool = (name) => ALL_TOOLS.some((t) => t.name === name);
