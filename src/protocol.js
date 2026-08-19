export const PROTOCOL_VERSION = 1

export const CompanionState = Object.freeze({
  IDLE: 'IDLE',
  THINKING: 'THINKING',
  WORKING: 'WORKING',
  WAITING: 'WAITING',
  SUCCESS: 'SUCCESS',
  ERROR: 'ERROR',
  DISCONNECTED: 'DISCONNECTED',
})

export const CompanionMessageKind = Object.freeze({
  READY: 'ready',
  HELLO: 'hello',
  STATE: 'state',
  PULSE: 'pulse',
  TASK: 'task',
  TASKS: 'tasks',
  CONFIG: 'config',
  PING: 'ping',
  PONG: 'pong',
  CLOSED: 'closed',
  SHUTDOWN: 'shutdown',
  // An approval the agent is blocked on, shown in the pet's bubble so it can be
  // answered without going to the web UI. ASK carries the choices; the helper
  // replies with an `answer` naming the one that was clicked.
  ASK: 'ask',
  ASK_CLEAR: 'ask-clear',
})

export const MAX_ASK_OPTIONS = 4

/** Normalise an approval into the shape the helper draws. */
export function createAsk({ id, question, options, detail }) {
  const text = String(question ?? '').trim()
  if (!id) throw new TypeError('ask needs an id to answer against')
  if (!text) throw new TypeError('ask needs a question')
  const list = (Array.isArray(options) ? options : [])
    .map((option) =>
      typeof option === 'string'
        ? { value: option, label: option }
        : { value: String(option?.value ?? option?.label ?? ''), label: String(option?.label ?? option?.value ?? '') },
    )
    .filter((option) => option.value && option.label)
    // The bubble is a desktop-pet speech bubble, not a dialog: past a handful of
    // choices it stops being readable, and the web UI remains the place for a
    // long list.
    .slice(0, MAX_ASK_OPTIONS)
  if (list.length === 0) throw new TypeError('ask needs at least one option')
  return { id: String(id), question: text, detail: detail ? String(detail) : '', options: list }
}

const states = new Set(Object.values(CompanionState))
const kinds = new Set(Object.values(CompanionMessageKind))

export function createMessage(kind, payload = {}) {
  if (!kinds.has(kind)) throw new TypeError(`Unknown companion message kind: ${kind}`)
  return {
    protocolVersion: PROTOCOL_VERSION,
    kind,
    timestamp: Date.now(),
    ...payload,
  }
}

export function assertCompanionMessage(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('Companion message must be an object')
  }
  if (value.protocolVersion !== PROTOCOL_VERSION) {
    throw new TypeError(`Unsupported protocol version: ${String(value.protocolVersion)}`)
  }
  if (!kinds.has(value.kind)) throw new TypeError(`Unknown companion message kind: ${String(value.kind)}`)
  if ((value.kind === CompanionMessageKind.STATE || value.kind === CompanionMessageKind.PULSE)
    && !states.has(value.state)) {
    throw new TypeError(`Unknown companion state: ${String(value.state)}`)
  }
  return value
}

export function encodeMessage(message) {
  assertCompanionMessage(message)
  return `${JSON.stringify(message)}\n`
}
