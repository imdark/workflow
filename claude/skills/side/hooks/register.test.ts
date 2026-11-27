import { expect, test } from 'claude-code/testing'

const ENV = { CLAUDE_CODE_SESSION_ID: 'abc12345', CLAUDE_PID: '42' }

test('/side registers as an immediate command', async ($, on) => {
  const registered: unknown[] = []
  on('command.register', (_, spec) => {
    registered.push(spec)
    return { value: { command: 'side' } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  expect(registered).toEqual([expect.objectContaining({ name: 'side', immediate: true })])
})

test('/side runs wf side with the session env and shows its output to the person only', async ($, on) => {
  const runs: { argv: readonly string[]; env?: Record<string, string> }[] = []
  on('process.hookEnv', () => ({ value: ENV }))
  on('process.run', (_, e) => {
    runs.push({ argv: e.argv, env: e.init?.env })
    return { value: { exitCode: 0, stdout: '🍴 Forked abc12345 into a new tmux tab\n', stderr: '',
                      isStdoutTruncated: false, isStderrTruncated: false } }
  })

  const out = await $.command.run({ command: 'side' })

  expect(runs).toEqual([{ argv: ['wf', 'side'], env: ENV }])
  expect(out.text).toBe('🍴 Forked abc12345 into a new tmux tab')
  expect(out.isTextDisplayOnly).toBe(true)
})

test('a failing wf side shows its error and the Accessibility hint', async ($, on) => {
  on('process.hookEnv', () => ({ value: ENV }))
  on('process.run', () => ({
    value: { exitCode: 1, stdout: '', stderr: '❌ System Events got an error: not allowed to send keystrokes',
             isStdoutTruncated: false, isStderrTruncated: false },
  }))

  const out = await $.command.run({ command: 'side' })

  expect(out.text).toContain('not allowed to send keystrokes')
  expect(out.text).toContain('Accessibility')
})
