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

test('an approval becomes a card the pet can answer, and a decision takes it down', () => {
  const listeners = new Map()
  const sent = []
  let onAnswer
  let dispose
  const decided = []
  const session = { header: { id: 's1' } }
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on(name, callback) { listeners.set(name, callback) },
    effect(setup) { dispose = setup() },
    // ask_user_question consumes ctx.userInteraction, so this is the seam an
    // answer travels back through.
    userInteraction: { answer: (payload) => decided.push(payload) },
  }

  // Capture what the plugin hands the helper without spawning one.
  const originalSend = HelperProcess.prototype.send
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.send = function (message) { sent.push(message) }
  HelperProcess.prototype.start = function () { onAnswer = this.onAnswer }
  try {
    apply(ctx, { helper: { headless: true } })
    listeners.get('session/event')(session, {
      type: 'approval/asked',
      seq: 1,
      data: { id: 'ap-1', toolName: 'bash', options: [{ value: 'y', label: '允许' }] },
    })
    const ask = sent.find((m) => m.kind === 'ask')
    assert.ok(ask, 'the approval reaches the pet as an ask')
    assert.equal(ask.id, 'ap-1')
    assert.deepEqual(ask.options, [{ value: 'y', label: '允许' }])

    // The pet answers.
    assert.equal(typeof onAnswer, 'function', 'the bridge exposes an answer callback')
    onAnswer({ id: 'ap-1', value: 'y' })
    assert.deepEqual(
      decided,
      [{ id: 'ap-1', selected: ['y'] }],
      'the answer uses the shape ask_user_question expects: selected is an array',
    )

    // A decision made elsewhere clears the card.
    listeners.get('session/event')(session, {
      type: 'approval/decided',
      seq: 2,
      data: { id: 'ap-1' },
    })
    assert.ok(sent.some((m) => m.kind === 'ask-clear'), 'a decision elsewhere takes the card down')
  } finally {
    HelperProcess.prototype.send = originalSend
    HelperProcess.prototype.start = originalStart
    if (typeof dispose === 'function') dispose()
  }
})

test('an ask_user_question tool call becomes a card in the bubble', () => {
  const listeners = new Map()
  const sent = []
  let dispose
  const ctx = {
    logger: { debug() {}, info() {}, warn() {}, error() {} },
    on(name, callback) { listeners.set(name, callback) },
    effect(setup) { dispose = setup() },
  }
  const originalSend = HelperProcess.prototype.send
  const originalStart = HelperProcess.prototype.start
  HelperProcess.prototype.send = function (message) { sent.push(message) }
  HelperProcess.prototype.start = function () {}
  try {
    apply(ctx, { helper: { headless: true } })
    listeners.get('session/event')({ header: { id: 's' } }, {
      type: 'tool/call',
      seq: 1,
      data: {
        callId: 'c1',
        name: 'ask_user_question',
        arguments: {
          questions: [{
            id: 'q1',
            question: '用哪种方案？',
            options: [{ value: 'a', label: '方案 A' }, { value: 'b', label: '方案 B' }],
          }],
        },
      },
    })
    const ask = sent.find((m) => m.kind === 'ask')
    assert.ok(ask, 'the question reaches the pet')
    assert.equal(ask.id, 'q1', 'answered against the question id the tool echoes back')
    assert.equal(ask.options.length, 2)

    listeners.get('session/event')({ header: { id: 's' } }, {
      type: 'tool/result', seq: 2, data: { callId: 'q1' },
    })
    assert.ok(sent.some((m) => m.kind === 'ask-clear'), 'the result takes the card down')
  } finally {
    HelperProcess.prototype.send = originalSend
    HelperProcess.prototype.start = originalStart
    if (typeof dispose === 'function') dispose()
  }
})
