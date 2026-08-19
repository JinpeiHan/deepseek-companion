#!/usr/bin/env node
// Run a Python command with whatever interpreter this machine actually has.
//
// The npm scripts hardcoded `py -3`, which is the Windows Python launcher and
// does not exist on Linux or macOS, so `pnpm run test:python` and
// `pnpm run helper:visual` were Windows-only even though the helper itself has
// no platform-specific code in it at all.
//
// Resolution matches src/helper-process.js deliberately: DSH_DAFEIYU_PYTHON
// first, then the platform default. If a developer points that variable at the
// interpreter their PySide6 lives in, the helper and these scripts agree about
// which Python that is, instead of the tests passing against one interpreter
// and the pet running under another.

import { spawn } from 'node:child_process'

const argv = process.argv.slice(2)
if (argv.length === 0) {
  console.error('usage: node scripts/python.mjs <args...>')
  process.exit(2)
}

const isWindows = process.platform === 'win32'
const configured = process.env.DSH_DAFEIYU_PYTHON

// `py -3` is a launcher, not an interpreter: it needs the version selector, and
// only on Windows.
const command = configured || (isWindows ? 'py' : 'python3')
const args = configured || !isWindows ? argv : ['-3', ...argv]

const child = spawn(command, args, { stdio: 'inherit', shell: false })

child.on('error', (error) => {
  if (error.code === 'ENOENT') {
    console.error(
      `Could not run '${command}'. Set DSH_DAFEIYU_PYTHON to the interpreter that has PySide6 ` +
        `installed, e.g. DSH_DAFEIYU_PYTHON=/usr/bin/python3.`,
    )
    process.exit(127)
  }
  console.error(error.message)
  process.exit(1)
})

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 1)
})
