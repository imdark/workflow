// Runs one NotesGraph automation's code for `wf agent serve` (see
// automations.py). Reads {code, input, secrets, shell} as JSON on stdin;
// writes one JSON object per line on stdout: {"log": line} as the code
// writes to its console, then {"done": output-as-JSON-or-null} or
// {"error", "stack"}.
//
// The code is the body of an async function, given `input`, `secrets`,
// `console` and `fetch`; what it returns is the output. A shell automation
// (`shell: true`, runtime 'shell' on the server) is given `sh` too, to run
// commands on this device; see makeSh. It runs in a `vm` context, which
// keeps this process's globals (process, require) out of its scope; it is
// not a security boundary. The timeout is the caller's: it kills this
// process's group, commands included. Keep in step with
// tools/agent-runner/src/code-runner.ts in notes-graph.
'use strict';

const vm = require('node:vm');
const { spawn } = require('node:child_process');
const { inspect } = require('node:util');

const LINE_CHARS = 10000;
// Most of a command's output `sh` hands back; past it the head is dropped.
const SH_OUTPUT_CHARS = 100000;

const emit = message => process.stdout.write(JSON.stringify(message) + '\n');
const show = value =>
  typeof value === 'string' ? value : inspect(value, { depth: 4, breakLength: 120 });
const logAt = level => (...args) => {
  const line = args.map(show).join(' ').slice(0, LINE_CHARS);
  emit({ log: level ? level + ': ' + line : line });
};

/**
 * `sh(command, {cwd, env, check})`: run `command` with `sh -c` in `cwd`
 * (default: the folder wf serves from), its output streamed to the log as
 * it comes. Resolves to {code, output}; with `check` (the default) a
 * non-zero exit rejects instead, with the output's tail in the message.
 */
const makeSh = log => (command, opts = {}) =>
  new Promise((resolve, reject) => {
    const child = spawn('sh', ['-c', String(command)], {
      cwd: opts.cwd || process.cwd(),
      env: { ...process.env, ...(opts.env || {}) },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    let partial = '';
    const take = chunk => {
      const text = chunk.toString('utf8');
      output += text;
      if (output.length > SH_OUTPUT_CHARS * 2) output = output.slice(-SH_OUTPUT_CHARS);
      const lines = (partial + text).split('\n');
      partial = lines.pop();
      for (const line of lines) log(line);
    };
    child.stdout.on('data', take);
    child.stderr.on('data', take);
    child.on('error', reject);
    child.on('close', code => {
      if (partial) log(partial);
      output = output.slice(-SH_OUTPUT_CHARS);
      if (code !== 0 && opts.check !== false) {
        const tail = output.trimEnd().slice(-500);
        reject(new Error(`\`${command}\` exited ${code}${tail ? `: ${tail}` : ''}`));
        return;
      }
      resolve({ code, output });
    });
  });

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const job = JSON.parse(raw);

  const shell = job.shell ? { sh: makeSh(logAt('')) } : {};
  const context = vm.createContext({
    ...shell,
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
