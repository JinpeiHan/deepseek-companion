#!/usr/bin/env node
// Freeze the PySide6 helper into a standalone binary, on whichever platform is
// running this.
//
// The build was PowerShell-only and emitted into runtime/bin/win32-x64, so a
// Linux or macOS user had no bundled helper at all and fell back to whatever
// `python3` happened to be on PATH -- which works only if PySide6 is installed
// system-wide. Nothing in the helper is Windows-specific: runtime/ contains no
// sys.platform branch anywhere, so the only thing standing between it and a
// native Linux build was the build script.
//
// PyInstaller cannot cross-compile. Each platform's binary has to be produced
// on that platform, which is why the output directory is named after the host
// rather than passed in: a build on Debian populates linux-x64, a build on
// Windows populates win32-x64, and neither can pretend to be the other.

import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const ARCH = { x64: 'x64', arm64: 'arm64' }[process.arch] ?? process.arch
export const platformDir = `${process.platform}-${ARCH}`
const EXE = process.platform === 'win32' ? 'dsh-dafeiyu-helper.exe' : 'dsh-dafeiyu-helper'

// PyInstaller's --add-data separator is ';' on Windows and ':' everywhere else.
// Getting this wrong does not fail the build: it produces a binary whose assets
// are silently missing, which only surfaces when the pet tries to draw.
const DATA_SEP = process.platform === 'win32' ? ';' : ':'

const resolvePython = () =>
  process.env.DSH_DAFEIYU_BUILD_PYTHON ||
  process.env.DSH_DAFEIYU_PYTHON ||
  (process.platform === 'win32' ? 'py' : 'python3')

const run = (command, args, options = {}) =>
  spawnSync(command, args, { cwd: ROOT, stdio: 'inherit', ...options })

const main = () => {
  const python = resolvePython()
  const output = join(ROOT, 'runtime', 'bin', platformDir)
  const work = join(ROOT, '.build', 'helper')
  mkdirSync(output, { recursive: true })
  mkdirSync(work, { recursive: true })

  const probe = spawnSync(
    python,
    ['-c', 'import PyInstaller, PySide6; print(f"PyInstaller {PyInstaller.__version__}; PySide6 {PySide6.__version__}")'],
    { cwd: ROOT, encoding: 'utf8' },
  )
  if (probe.status !== 0) {
    console.error(
      `'${python}' cannot import both PyInstaller and PySide6. Install them into that interpreter, ` +
        'or point DSH_DAFEIYU_BUILD_PYTHON at one that has them.',
    )
    if (probe.stderr) console.error(probe.stderr.trim())
    process.exit(1)
  }
  console.log(`${platformDir}: ${probe.stdout.trim()}`)

  const build = run(python, [
    '-m', 'PyInstaller',
    '--noconfirm',
    '--clean',
    '--onefile',
    '--console',
    '--name', 'dsh-dafeiyu-helper',
    '--distpath', output,
    '--workpath', work,
    '--specpath', work,
    '--add-data', `${join(ROOT, 'assets')}${DATA_SEP}assets`,
    '--paths', join(ROOT, 'runtime'),
    join(ROOT, 'runtime', 'helper.py'),
  ])
  if (build.status !== 0) {
    console.error(`PyInstaller failed with exit code ${build.status}`)
    process.exit(build.status ?? 1)
  }

  const executable = join(output, EXE)
  if (!existsSync(executable)) {
    console.error(`PyInstaller reported success but ${executable} does not exist`)
    process.exit(1)
  }

  const smoke = run('node', [join(ROOT, 'scripts', 'test-packaged-helper.mjs'), '--executable', executable])
  if (smoke.status !== 0) {
    console.error(`Packaged helper smoke test failed with exit code ${smoke.status}`)
    process.exit(smoke.status ?? 1)
  }
  console.log(executable)
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  main()
}
