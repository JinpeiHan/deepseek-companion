import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { HelperProcess, defaultCommand, isWsl, shouldUseBundledHelper } from '../src/helper-process.js'
import { CompanionMessageKind, CompanionState, createMessage } from '../src/protocol.js'

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  throw new Error('timed out waiting for helper condition')
}

test('helper process exposes WSL detection helpers without throwing', () => {
  assert.equal(typeof isWsl(), 'boolean')
  assert.equal(typeof shouldUseBundledHelper(), 'boolean')
  assert.equal(typeof defaultCommand(), 'string')
  assert.equal(typeof defaultCommand(true), 'string')
})

test('helper consumes events and exits when the plugin stops', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-dafeiyu-test-'))
  const eventLog = join(directory, 'events.jsonl')
  const logger = { debug() {}, info() {}, warn() {}, error() {} }
  const bridge = new HelperProcess({ headless: true, eventLog }, logger)
  const child = bridge.start()
  const exited = new Promise((resolve, reject) => {
    child.once('exit', (code) => code === 0 ? resolve() : reject(new Error(`helper exited with ${String(code)}`)))
    child.once('error', reject)
  })
  bridge.send(createMessage(CompanionMessageKind.STATE, {
    state: CompanionState.WORKING,
    message: 'running a test',
  }))
  bridge.stop('test-complete')
  await exited

  const messages = (await readFile(eventLog, 'utf8')).trim().split(/\r?\n/).map(JSON.parse)
  assert.equal(messages[0].state, CompanionState.WORKING)
  assert.equal(messages.at(-1).kind, CompanionMessageKind.SHUTDOWN)
  await rm(directory, { recursive: true, force: true })
})

test('helper heartbeat stays healthy and responds without a restart', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-dafeiyu-heartbeat-'))
  const eventLog = join(directory, 'events.jsonl')
  const logger = { debug() {}, info() {}, warn() {}, error() {} }
  const bridge = new HelperProcess({
    headless: true,
    eventLog,
    heartbeatMs: 25,
    heartbeatTimeoutMs: 150,
  }, logger)
  const initialChild = bridge.start()
  bridge.send(createMessage(CompanionMessageKind.STATE, {
    state: CompanionState.THINKING,
    message: 'heartbeat test',
  }))
  await waitFor(async () => {
    try {
      return (await readFile(eventLog, 'utf8')).includes('"kind": "ping"')
    } catch {
      return false
    }
  })
  await new Promise((resolve) => setTimeout(resolve, 220))
  assert.equal(bridge.child, initialChild)
  bridge.stop('heartbeat-test-complete')
  await waitFor(async () => {
    try {
      return (await readFile(eventLog, 'utf8')).includes('"kind": "shutdown"')
    } catch {
      return false
    }
  })
  await rm(directory, { recursive: true, force: true })
})

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const pythonCommand = process.env.DSH_DAFEIYU_PYTHON || (process.platform === 'win32' ? 'py' : 'python3')

function hasPySide6() {
  const probe = spawnSync(pythonCommand, ['-c', 'import PySide6'], { stdio: 'ignore' })
  return probe.status === 0
}

// Build a bundle whose `standard` pack is registered but has no manifest on
// disk: exactly what a user hits today when they flip the proportion setting
// before the art for that pack ships.
async function makeBundleWithBrokenStandardPack(directory) {
  await mkdir(join(directory, 'runtime'), { recursive: true })
  await mkdir(join(directory, 'assets'), { recursive: true })
  for (const name of ['helper.py', 'animation_model.py', 'asset_pack.py', 'frame_renderer.py', 'layout_store.py', 'persona_copy.py']) {
    await cp(join(repoRoot, 'runtime', name), join(directory, 'runtime', name))
  }
  await cp(join(repoRoot, 'assets', 'pet-manifest.json'), join(directory, 'assets', 'pet-manifest.json'))
  await cp(join(repoRoot, 'assets', 'persona-copy.zh-CN.json'), join(directory, 'assets', 'persona-copy.zh-CN.json'))
  // A real copy, not a symlink: load_pack_descriptor resolves the pack root and
  // rejects anything that lands outside the bundle's own assets directory.
  await cp(join(repoRoot, 'assets', 'pet'), join(directory, 'assets', 'pet'), { recursive: true })
  await writeFile(join(directory, 'assets', 'pet-packs.json'), JSON.stringify({
    formatVersion: 1,
    defaultPack: 'chibi',
    packs: {
      chibi: { manifest: 'pet-manifest.json', root: 'pet' },
      standard: { manifest: 'pet-standard-manifest.json', root: 'pet-standard' },
    },
  }), 'utf8')
}

test('helper falls back to chibi when the selected proportion pack is unusable', { skip: hasPySide6() ? false : 'PySide6 is not installed' }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-dafeiyu-pack-'))
  await makeBundleWithBrokenStandardPack(directory)

  const child = spawn(pythonCommand, [join(directory, 'runtime', 'helper.py')], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      QT_QPA_PLATFORM: 'offscreen',
      DSH_DAFEIYU_PROPORTION: 'standard',
      DSH_DAFEIYU_REDUCED_MOTION: '1',
      DSH_DAFEIYU_LAYOUT_PATH: join(directory, 'layout.json'),
    },
  })
  let stdout = ''
  let stderr = ''
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk) => { stdout += chunk })
  child.stderr.on('data', (chunk) => { stderr += chunk })

  const exited = new Promise((resolveExit, reject) => {
    child.once('exit', (code) => resolveExit(code))
    child.once('error', reject)
  })
  child.stdin.write(`${JSON.stringify({ protocolVersion: 1, kind: 'state', state: 'IDLE', message: 'pack fallback' })}\n`)
  await waitFor(async () => stdout.includes('"ready"'), 20000)
  child.stdin.end()
  const code = await exited

  assert.equal(code, 0, `helper exited with ${String(code)}; stderr: ${stderr}`)
  assert.match(stderr, /proportion pack 'standard' unavailable/)
  assert.match(stderr, /falling back to chibi/)
  await rm(directory, { recursive: true, force: true })
})
