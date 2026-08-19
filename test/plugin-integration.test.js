import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import test from 'node:test'
import { apply, inject } from '../src/index.js'
import { HelperProcess } from '../src/helper-process.js'

test('plugin declares the service dependencies it actually consumes', () => {
  // sessions is required and settings is not, which is what lets the pet mount
  // in the command-line DSH: Cordis holds a plugin unmounted until every
  // *required* service exists, and the CLI has no settings service. mount()
  // already falls back to a local scope when it is absent.
  assert.deepEqual(inject.required, ['sessions'])
  assert.deepEqual(inject.optional, ['settings'])
})

test('the plugin mounts without a settings service, as the CLI has none', () => {
  const listeners = new Map()
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on(name, callback) {
      listeners.set(name, callback)
    },
    effect(setup) {
      dispose = setup()
    },
    // deliberately no ctx.settings: this is the command-line DSH.
  }

  assert.doesNotThrow(
    () => apply(ctx, { helper: { headless: true } }),
    'mounting must not require ctx.settings',
  )
  assert.ok(listeners.has('session/event'), 'the pet still subscribes to session events')
  if (typeof dispose === 'function') dispose()
})

test('package metadata exposes the DSH web client bundle', () => {
  const require = createRequire(import.meta.url)
  const metadata = require('dsh-dafeiyu/package.json')
  assert.equal(metadata.exports['./client'], './lib/client.js')
  assert.equal(metadata.dsh.client.platform, 'web')
})

async function waitFor(predicate, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  throw new Error('timed out waiting for plugin integration condition')
}

test('plugin forwards DSH-shaped session events and owns helper shutdown', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-dafeiyu-plugin-'))
  const eventLog = join(directory, 'events.jsonl')
  const listeners = new Map()
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on(name, callback) {
      listeners.set(name, callback)
    },
    effect(setup) {
      dispose = setup()
    },
  }

  apply(ctx, { helper: { headless: true, eventLog } })
  const session = { header: { id: 'phase0-real-shape' } }
  listeners.get('session/event')(session, { type: 'turn/start', seq: 1, data: { turn: 1 } })
  listeners.get('session/event')(session, {
    type: 'tool/call',
    seq: 2,
    data: { callId: 'call-1', name: 'web_search' },
  })
  listeners.get('session/event')(session, {
    type: 'turn/end',
    seq: 3,
    data: { turn: 1, reason: { kind: 'completed' } },
  })
  dispose()

  await waitFor(async () => {
    try {
      return (await readFile(eventLog, 'utf8')).includes('"kind": "shutdown"')
    } catch {
      return false
    }
  })

  const messages = (await readFile(eventLog, 'utf8')).trim().split(/\r?\n/).map(JSON.parse)
  assert.deepEqual(messages.map((message) => message.kind), [
    'hello',
    'state',
    'state',
    'state',
    'pulse',
    'shutdown',
  ])
  assert.deepEqual(messages.map((message) => message.state).filter(Boolean), [
    'IDLE',
    'IDLE',
    'THINKING',
    'WORKING',
    'SUCCESS',
  ])
  await rm(directory, { recursive: true, force: true })
})

test('live settings keep the active project state without restarting the helper', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-dafeiyu-live-settings-'))
  const eventLog = join(directory, 'events.jsonl')
  const listeners = new Map()
  let dispose
  let settingsListener
  let settingsValue = {
    enabled: true,
    scale: 1,
    bubbleScale: 1,
    activityLevel: 'normal',
    reducedMotion: false,
    includeSubagents: false,
    characterProportion: 'chibi',
  }
  const settings = {
    get: () => ({ ...settingsValue }),
    watch(callback) {
      settingsListener = callback
      return () => { settingsListener = undefined }
    },
  }
  const ctx = {
    settings: { register: () => settings },
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on(name, callback) {
      listeners.set(name, callback)
    },
    effect(setup) {
      dispose = setup()
    },
  }

  let helperOptions
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.start = function (...args) {
    helperOptions = this.options
    return originalStart.apply(this, args)
  }
  try {
    apply(ctx, { helper: { headless: true, eventLog } })
  } finally {
    HelperProcess.prototype.start = originalStart
  }
  const activeSession = { header: { id: 'live-settings', cwd: 'D:\\work\\active-project' } }
  listeners.get('session/event')(activeSession, { type: 'turn/start', seq: 1, data: { turn: 1 } })
  listeners.get('session/event')(activeSession, {
    type: 'todo/write',
    seq: 2,
    data: { todos: [{ content: '继续保留这个任务', status: 'in_progress' }] },
  })
  settingsValue = { ...settingsValue, scale: 0.9, bubbleScale: 0.8, characterProportion: 'standard' }
  settingsListener(settingsValue)
  listeners.get('session/event')(activeSession, {
    type: 'tool/call',
    seq: 3,
    data: { callId: 'edit-after-settings', name: 'apply_patch' },
  })
  dispose()

  await waitFor(async () => {
    try {
      return (await readFile(eventLog, 'utf8')).includes('"kind": "shutdown"')
    } catch {
      return false
    }
  })

  const messages = (await readFile(eventLog, 'utf8')).trim().split(/\r?\n/).map(JSON.parse)
  assert.equal(messages.filter((message) => message.kind === 'hello').length, 1)
  assert.equal(messages.filter((message) => message.kind === 'config').length, 1)
  const configMessage = messages.find((message) => message.kind === 'config')
  assert.equal(configMessage.characterProportion, 'standard')
  const working = messages.findLast((message) => message.state === 'WORKING')
  assert.equal(working.project, 'active-project')
  assert.equal(working.task, '继续保留这个任务')
  assert.equal(helperOptions.env.DSH_DAFEIYU_PROPORTION, 'chibi')
  await rm(directory, { recursive: true, force: true })
})

test('the pet registers as the user-questions provider and answers through it', async () => {
  const sent = []
  let onAnswer
  let provider
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on() {},
    effect(setup) { dispose = setup() },
    // ctx.userQuestions is a pull seam: the tool awaits the provider's ask().
    userQuestions: {
      registerProvider(candidate) {
        provider = candidate
        return () => { provider = undefined }
      },
    },
  }
  const originalSend = HelperProcess.prototype.send
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.send = function (message) { sent.push(message) }
  HelperProcess.prototype.start = function () { onAnswer = this.onAnswer }
  try {
    apply(ctx, { helper: { headless: true } })
    assert.ok(provider, 'the pet registers itself as the provider')

    const answered = provider.ask({
      questions: [{
        id: 'q1',
        question: '用哪种方案？',
        options: [{ label: '方案 A', description: '更快' }, { label: '方案 B' }],
      }],
    })
    const ask = sent.find((m) => m.kind === 'ask')
    assert.ok(ask, 'the question reaches the pet')
    assert.deepEqual(ask.options, [{ label: '方案 A', description: '更快' }, { label: '方案 B' }])

    onAnswer({ id: 'q1', value: '方案 A' })
    // selected carries labels, which is what the host echoes back.
    assert.deepEqual(await answered, { answers: [{ id: 'q1', selected: ['方案 A'] }] })
    assert.ok(sent.some((m) => m.kind === 'ask-clear'), 'the card comes down once answered')
  } finally {
    HelperProcess.prototype.send = originalSend
    HelperProcess.prototype.start = originalStart
    if (typeof dispose === 'function') dispose()
  }
})

test('a question the bubble cannot show is declined rather than swallowed', async () => {
  let provider
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on() {},
    effect(setup) { dispose = setup() },
    userQuestions: { registerProvider(c) { provider = c; return () => {} } },
  }
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.start = function () {}
  try {
    apply(ctx, { helper: { headless: true } })
    // No options means nothing to click; declining lets the host fall back to a
    // UI that can take free text.
    await assert.rejects(provider.ask({ questions: [{ id: 'q', question: 'free text?' }] }))
    // More than one question would need somewhere to queue.
    await assert.rejects(provider.ask({
      questions: [
        { id: 'a', question: 'one', options: [{ label: 'x' }] },
        { id: 'b', question: 'two', options: [{ label: 'y' }] },
      ],
    }))
  } finally {
    HelperProcess.prototype.start = originalStart
    if (typeof dispose === 'function') dispose()
  }
})

test('a duplicate provider is reported, not fatal', () => {
  const warnings = []
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn: (m) => warnings.push(String(m)), error() {} },
    on() {},
    effect(setup) { dispose = setup() },
    userQuestions: {
      registerProvider() {
        throw new Error('a user-questions provider is already registered')
      },
    },
  }
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.start = function () {}
  try {
    // Only one provider may be active, so a richer UI winning is expected and
    // must not stop the pet from mounting and showing states.
    assert.doesNotThrow(() => apply(ctx, { helper: { headless: true } }))
    assert.ok(warnings.some((w) => w.includes('already')), 'the clash is reported')
  } finally {
    HelperProcess.prototype.start = originalStart
    if (typeof dispose === 'function') dispose()
  }
})
