/** Minimal structured logger (JSON lines to stdout), no dependency needed. */

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = LEVELS[(process.env.LOG_LEVEL || 'info').toLowerCase()] ?? LEVELS.info;

function emit(level, fields, msg) {
  if (LEVELS[level] < threshold) return;
  // Allow log.info('message') as well as log.info({...}, 'message').
  if (typeof fields === 'string') {
    msg = fields;
    fields = {};
  }
  const line = { ts: new Date().toISOString(), level, msg, ...fields };
  const out = level === 'error' || level === 'warn' ? process.stderr : process.stdout;
  out.write(`${JSON.stringify(line)}\n`);
}

export const log = {
  debug: (f, m) => emit('debug', f, m),
  info: (f, m) => emit('info', f, m),
  warn: (f, m) => emit('warn', f, m),
  error: (f, m) => emit('error', f, m),
};
