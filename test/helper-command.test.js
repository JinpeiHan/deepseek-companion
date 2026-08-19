import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveHelperLaunch } from '../src/helper-process.js'

const bundledPath = '/package/runtime/bin/win32-x64/dsh-dafeiyu-helper.exe'
const helperPath = '/package/runtime/helper.py'

function resolve(overrides = {}) {
  return resolveHelperLaunch({
    platform: 'linux',
    isWslEnv: false,
    bundledPath,
    helperPath,
    fileExists: () => true,
    windowsPath: () => 'C:\\package\\runtime\\bin\\win32-x64\\dsh-dafeiyu-helper.exe',
    ...overrides,
  })
}

test('native Windows launches the bundled x64 helper directly', () => {
  assert.deepEqual(resolve({ platform: 'win32' }), { command: bundledPath, args: [] })
})

test('WSL visual mode uses cmd.exe so npm file modes cannot block the helper', () => {
  assert.deepEqual(resolve({ isWslEnv: true }), {
    command: 'cmd.exe',
    args: ['/d', '/c', 'C:\\package\\runtime\\bin\\win32-x64\\dsh-dafeiyu-helper.exe'],
  })
})

test('WSL headless mode stays on Linux Python for Linux event-log paths', () => {
  assert.deepEqual(resolve({ isWslEnv: true, headless: true }), {
    command: 'python3',
    args: [helperPath],
  })
})

test('ordinary Linux does not attempt Windows interop', () => {
  assert.deepEqual(resolve(), { command: 'python3', args: [helperPath] })
})

test('missing bundled helper falls back to the configured Python', () => {
  assert.deepEqual(resolve({
    isWslEnv: true,
    fileExists: () => false,
    pythonEnv: '/opt/dsh/python',
  }), {
    command: '/opt/dsh/python',
    args: [helperPath],
  })
})

test('macOS runs its own frozen helper, never the Windows one', () => {
  const nativePath = '/package/runtime/bin/darwin-arm64/dsh-dafeiyu-helper'
  const launch = resolve({
    platform: 'darwin',
    nativePath,
    // Only the macOS binary is present; the Windows path must not be reached.
    fileExists: (path) => path === nativePath,
  })
  assert.deepEqual(launch, { command: nativePath, args: [] })
})

test('macOS without a frozen helper falls back to python3, not to interop', () => {
  const launch = resolve({
    platform: 'darwin',
    nativePath: '/package/runtime/bin/darwin-arm64/dsh-dafeiyu-helper',
    fileExists: () => false,
  })
  // `py` is the Windows launcher and does not exist on macOS.
  assert.equal(launch.command, 'python3')
  assert.deepEqual(launch.args, [helperPath])
})

test('a macOS host never takes the WSL path', () => {
  // isWsl() is linux-only by construction, but the launch resolver takes the
  // flag as an argument, so assert the branch is guarded by platform too.
  const launch = resolve({
    platform: 'darwin',
    isWslEnv: true,
    nativePath: undefined,
    fileExists: () => true,
  })
  assert.notEqual(launch.command, 'cmd.exe', 'macOS must never shell out to cmd.exe')
})
