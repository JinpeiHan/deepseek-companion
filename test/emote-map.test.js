import assert from 'node:assert/strict'
import test from 'node:test'

import {
  EMOTE_COOLDOWN_MS,
  IDLE_RELAX_MS,
  IDLE_SLACK_MS,
  emoteFor,
  idleEmoteFor,
  updateEmoteMemory,
} from '../src/emote-map.js'

test('the four turn/end reasons are told apart', () => {
  // DSH closes a turn with one of exactly four reasons, and they mean
  // different things: finishing is not being stopped, and being stopped is not
  // failing.
  const memory = { stepsThisTurn: 4 }
  assert.equal(emoteFor({ type: 'turn/end', data: { reason: { kind: 'completed' } } }, memory), 'salute_roger')
  assert.equal(emoteFor({ type: 'turn/end', data: { reason: { kind: 'error' } } }, memory), 'cry_wail')
  assert.equal(emoteFor({ type: 'turn/end', data: { reason: { kind: 'aborted' } } }, memory), 'sulk_pout')
  assert.equal(emoteFor({ type: 'turn/end', data: { reason: { kind: 'blocked' } } }, memory), 'confused_question')
})

test('answering in one step is worth showing off about', () => {
  const done = (memory) => emoteFor({ type: 'turn/end', data: { reason: { kind: 'completed' } } }, memory)
  assert.equal(done({ stepsThisTurn: 1 }), 'smug_zako')
  // Zero means no step was observed, not that the turn was quick: a turn whose
  // steps went unrecorded still did the work.
  assert.equal(done({ stepsThisTurn: 0 }), 'salute_roger')
  assert.equal(done({}), 'salute_roger')
})

test('repeated provider retries escalate from puzzled to wrung out', () => {
  const memory = {}
  const retry = { type: 'llm/retry' }
  updateEmoteMemory(memory, retry)
  assert.equal(emoteFor(retry, memory), 'confused_question')
  updateEmoteMemory(memory, retry)
  updateEmoteMemory(memory, retry)
  assert.equal(emoteFor(retry, memory), 'soul_leaving', 'the third retry in a turn is a bad day, not a hiccup')
})

test('a new turn forgets the last one', () => {
  const memory = {}
  for (let i = 0; i < 5; i += 1) updateEmoteMemory(memory, { type: 'llm/retry' })
  updateEmoteMemory(memory, { type: 'turn/start' })
  assert.equal(emoteFor({ type: 'llm/retry' }, memory), 'confused_question')
})

test('compaction is the character eating its own tokens', () => {
  assert.equal(emoteFor({ type: 'compaction/start' }, {}), 'eat_token')
})

test('a denied approval sulks and an approved one does not', () => {
  assert.equal(emoteFor({ type: 'approval/decided', data: { approved: false } }, {}), 'sulk_pout')
  assert.equal(emoteFor({ type: 'approval/decided', data: { approved: true } }, {}), null)
})

test('a returning user is greeted only after a real absence', () => {
  assert.equal(emoteFor({ type: 'user/message' }, { quietForMs: 1000 }), null, 'every prompt would be exhausting')
  assert.equal(emoteFor({ type: 'user/message' }, { quietForMs: IDLE_SLACK_MS }), 'love_heart_hands')
})

test('silence escalates, and a fresh session is not idle', () => {
  assert.equal(idleEmoteFor(0), null)
  assert.equal(idleEmoteFor(IDLE_SLACK_MS), 'slack_off')
  assert.equal(idleEmoteFor(IDLE_RELAX_MS), 'relax_armchair')
})

test('unmapped events stay unmapped', () => {
  // Reacting to everything is noise; the reaction stops meaning anything.
  for (const type of ['assistant/chunk', 'todo/write', 'request/header', 'session/title', 'hook/invoked']) {
    assert.equal(emoteFor({ type }, {}), null, `${type} should not earn a reaction`)
  }
})

test('every mapped clip exists in the shipped rig packs', async () => {
  const { readFile } = await import('node:fs/promises')
  const manifest = JSON.parse(await readFile(new URL('../assets/pet-standard-rig.json', import.meta.url), 'utf8'))
  const clips = new Set(Object.keys(manifest.clips))
  const mapped = new Set()
  const events = [
    { type: 'turn/end', data: { reason: { kind: 'completed' } } },
    { type: 'turn/end', data: { reason: { kind: 'error' } } },
    { type: 'turn/end', data: { reason: { kind: 'aborted' } } },
    { type: 'turn/end', data: { reason: { kind: 'blocked' } } },
    { type: 'compaction/start' },
    { type: 'llm/retry' },
    { type: 'approval/asked' },
    { type: 'approval/decided', data: { approved: false } },
    { type: 'plan/mode' },
    { type: 'goal/change' },
    { type: 'subagent/descriptor' },
    { type: 'user/message' },
  ]
  for (const event of events) {
    for (const memory of [{ stepsThisTurn: 1 }, { stepsThisTurn: 5, retriesThisTurn: 9, quietForMs: IDLE_SLACK_MS }]) {
      const clip = emoteFor(event, memory)
      if (clip) mapped.add(clip)
    }
  }
  mapped.add(idleEmoteFor(IDLE_SLACK_MS))
  mapped.add(idleEmoteFor(IDLE_RELAX_MS))
  for (const clip of mapped) {
    assert.ok(clips.has(clip), `${clip} is mapped but no such clip ships`)
  }
  assert.ok(mapped.size >= 10, `expected a broad mapping, got ${mapped.size}`)
})

test('the cooldown is long enough to see a clip finish', () => {
  // Clips run 1.5-2s; a shorter cooldown would cut one off with the next.
  assert.ok(EMOTE_COOLDOWN_MS >= 2000)
})
