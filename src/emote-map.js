/**
 * Which expressive clip a DSH session event earns.
 *
 * Every entry is keyed on an event type that actually exists in
 * `KNOWN_SESSION_EVENT_TYPES`; nothing here is speculative. Clips with no
 * honest trigger are deliberately left unmapped rather than fired on a loose
 * excuse — a pet that reacts to everything reads as noise, and the reaction
 * stops meaning anything.
 *
 * These are *reactions to a moment*, not states. The DSH state the reducer
 * computes is unchanged underneath and resumes when the clip ends, which is why
 * they travel as EMOTE rather than as a STATE change.
 */

/** Minimum gap between two reactions, so a burst of events does not twitch. */
export const EMOTE_COOLDOWN_MS = 4000

/** Retries within one turn before the pet stops being merely puzzled. */
const RETRY_EXHAUSTION = 3

/** How long a session must be quiet before the pet visibly gives up waiting. */
export const IDLE_SLACK_MS = 5 * 60 * 1000
export const IDLE_RELAX_MS = 15 * 60 * 1000

/**
 * Decide the clip for one event, or null.
 *
 * @param event Session event as delivered to `session/event`.
 * @param memory Per-session scratch the caller owns and passes back.
 */
export function emoteFor(event, memory = {}) {
  const type = String(event?.type ?? '')
  const data = event?.data ?? {}

  switch (type) {
    case 'turn/end': {
      // The four reasons are a closed set in DSH, and they mean genuinely
      // different things to a companion: finishing is not the same as being
      // stopped, and being stopped is not the same as failing.
      const kind = String(data.reason?.kind ?? '')
      if (kind === 'completed') {
        // Exactly one step means it answered in a single model call, which is
        // worth a small showing-off. Zero is not "quick", it is "no step was
        // observed" -- a turn whose steps went unrecorded still did the work,
        // so it gets the ordinary acknowledgement.
        return memory.stepsThisTurn === 1 ? 'smug_zako' : 'salute_roger'
      }
      if (kind === 'error') return 'cry_wail'
      if (kind === 'aborted') return 'sulk_pout'
      if (kind === 'blocked') return 'confused_question'
      return null
    }

    // Compaction is the harness eating its own context to make room. The
    // character's own joke about living on tokens is exactly this moment.
    case 'compaction/start':
      return 'eat_token'

    case 'llm/retry':
    case 'llm/retry-started':
      // Puzzled the first time, wrung out by the third: repeated provider
      // failure in one turn is the difference between a hiccup and a bad day.
      return (memory.retriesThisTurn ?? 0) >= RETRY_EXHAUSTION ? 'soul_leaving' : 'confused_question'

    // Being asked for permission is the pet asking on the agent's behalf.
    case 'approval/asked':
      return 'plead_kneel'

    case 'approval/decided': {
      const approved = data.approved === true || data.decision === 'approve' || data.allowed === true
      return approved ? null : 'sulk_pout'
    }

    // A plan is being formed, or a new goal handed down.
    case 'plan/mode':
      return data.enabled === false ? null : 'idea_lightbulb'
    case 'goal/change':
      return 'salute_roger'

    // Work is being handed to a subagent or a workflow: off it runs.
    case 'subagent/descriptor':
    case 'tool-workflow/run-start':
      return 'dash_run'

    case 'user/message':
      // Only when the human has been away long enough for coming back to be an
      // event. Greeting every prompt would be exhausting.
      return memory.quietForMs >= IDLE_SLACK_MS ? 'love_heart_hands' : null

    default:
      return null
  }
}

/**
 * The clip for a stretch of silence, or null while the session is still fresh.
 *
 * Separate from `emoteFor` because it is driven by a clock rather than an
 * event: nothing arrives to react to, which is the whole point.
 */
export function idleEmoteFor(quietForMs) {
  if (quietForMs >= IDLE_RELAX_MS) return 'relax_armchair'
  if (quietForMs >= IDLE_SLACK_MS) return 'slack_off'
  return null
}

/** Track the per-turn counters `emoteFor` reads. */
export function updateEmoteMemory(memory, event) {
  const type = String(event?.type ?? '')
  if (type === 'turn/start') {
    memory.retriesThisTurn = 0
    memory.stepsThisTurn = 0
  } else if (type === 'step/start') {
    memory.stepsThisTurn = (memory.stepsThisTurn ?? 0) + 1
  } else if (type === 'llm/retry' || type === 'llm/retry-started') {
    memory.retriesThisTurn = (memory.retriesThisTurn ?? 0) + 1
  }
  return memory
}
