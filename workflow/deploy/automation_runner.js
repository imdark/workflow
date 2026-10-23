// Runs one NotesGraph automation's code for `wf agent serve` (see
// automations.py). Reads {code, input, secrets} as JSON on stdin; writes one
// JSON object per line on stdout: {"log": line} as the code writes to its
// console, then {"done": output-as-JSON-or-null} or {"error", "stack"}.
//
// The code is the body of an async function, given `input`, `secrets`,
// `console` and `fetch`; what it returns is the output. It runs in a `vm`
// context, which keeps this process's globals (process, require) out of its
// scope; it is not a security boundary. The timeout is the caller's: it
// kills this process. Keep in step with tools/agent-runner/src/code-runner.ts
// in notes-graph.
'use strict';

const vm = require('node:vm');
const { inspect } = require('node:util');

const LINE_CHARS = 10000;

const emit = message => process.stdout.write(JSON.stringify(message) + '\n');
const show = value =>
  typeof value === 'string' ? value : inspect(value, { depth: 4, breakLength: 120 });
const logAt = level => (...args) => {
  const line = args.map(show).join(' ').slice(0, LINE_CHARS);
  emit({ log: level ? level + ': ' + line : line });
};

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const job = JSON.parse(raw);

  const context = vm.createContext({
    console: {
      log: logAt(''),
      info: logAt(''),
      debug: logAt(''),
      warn: logAt('warn'),
      error: logAt('error'),
    },
    input: job.input ?? null,
    secrets: Object.freeze({ ...(job.secrets || {}) }),
    fetch,
    Headers,
    Request,
    Response,
    URL,
    URLSearchParams,
    AbortController,
    TextEncoder,
    TextDecoder,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    structuredClone,
    atob,
    btoa,
  });

  const run = vm.runInContext('(async () => {\n' + job.code + '\n})', context, {
    filename: 'automation.js',
    lineOffset: -1,
  });
  const value = await run();
  let output;
  try {
    output = value === undefined ? null : JSON.stringify(value);
  } catch (err) {
    throw new Error('the return value is not JSON: ' + err.message);
  }
  emit({ done: output ?? null });
}

main().then(
  // Timers the code left running must not keep the run alive.
  () => process.exit(0),
  err => {
    emit({
      error: err && err.message ? err.message : String(err),
      stack: err && err.stack ? String(err.stack) : '',
    });
    process.exit(1);
  }
);
