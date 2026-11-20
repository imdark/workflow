import type { Register } from 'claude-code'

// /side runs `wf side` straight from the engine, like the built-in /fork:
// no model turn, no tokens, and `immediate` lets it run while a turn is
// still streaming. hookEnv() hands the child CLAUDE_CODE_SESSION_ID and
// CLAUDE_PID, which `wf side` needs to know which conversation to fork and
// which tab it lives in; the rest of the environment (TMUX, TERM_PROGRAM,
// PATH) is the host's own.
export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'side',
      description: 'Fork this conversation into a new terminal tab and switch to it',
      immediate: true,
    })

    return next(e)
  })

  on('command.run', { command: 'side' }, async $ => {
    const env = await $.process.hookEnv()
    let text: string
    try {
      const { exitCode, stdout, stderr } = await $.process.run(['wf', 'side'], { env })
      const out = (exitCode === 0 ? stdout : stderr || stdout).trim()
      text = out || (exitCode === 0 ? 'Fork opened.' : `wf side exited ${exitCode}`)
      if (exitCode !== 0 && /keystroke|assistive/i.test(out)) {
        text += '\nTerminal needs Accessibility permission: System Settings → Privacy & Security → Accessibility.'
      }
    } catch (err) {
      text = `❌ couldn't run wf side: ${err instanceof Error ? err.message : String(err)}`
    }

    return { text, isTextDisplayOnly: true }
  })
}
