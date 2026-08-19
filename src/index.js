import { createRequire } from 'node:module'
import Schema from '@deepseek-ai/schemastery'
import { CompanionReducer } from './companion-reducer.js'
import { HelperProcess } from './helper-process.js'
import {
  CompanionMessageKind,
  CompanionState,
  createAsk,
  createMessage,
} from './protocol.js'
import { characterName, statusCopy } from './status-copy.js'

const require = createRequire(import.meta.url)
const pkg = require('../package.json')

export const name = 'dsh-dafeiyu'
// Sessions is the only hard dependency: the pet's whole feature is reacting to
// session events. Settings is optional, and listing it as required is what kept
// the pet out of the command-line DSH -- Cordis holds a plugin unmounted until
// every injected service exists, so the CLI, which has no settings service,
// never mounted it and never reached the localSettingsScope fallback that was
// written for exactly that case.
export const inject = { required: ['sessions'], optional: ['settings'] }
export const CONFIG_ENDPOINT = '/plugins/dsh-dafeiyu/config'
export const Config = Schema.object({
  enabled: Schema.boolean().default(true).description('启用桌面小鲸鱼'),
  characterProportion: Schema.union([
    Schema.const('chibi').description('Q版'),
    Schema.const('standard').description('标准'),
    Schema.const('slender').description('修长'),
  ]).default('chibi').description('角色形态'),
  scale: Schema.number().min(0.7).max(1.4).step(0.05).default(1).role('slider').description('角色大小'),
  bubbleScale: Schema.number().min(0.8).max(1.2).step(0.05).default(1).role('slider').description('气泡大小'),
  activityLevel: Schema.union([
    Schema.const('quiet').description('安静'),
    Schema.const('normal').description('标准'),
    Schema.const('lively').description('活泼'),
  ]).default('normal').description('空闲微动作频率'),
  reducedMotion: Schema.boolean().default(false).description('减少走动、循环帧和程序化晃动'),
  bubbleMode: Schema.union([
    Schema.const('always').description('常驻显示'),
    Schema.const('hidden').description('完全隐藏'),
    Schema.const('custom').description('自定义显示状态'),
  ]).default('always').description('气泡显示模式'),
  bubbleStates: Schema.array(Schema.string()).default(['SUCCESS', 'ERROR', 'WAITING']).description('自定义模式下显示气泡的状态'),
  includeSubagents: Schema.boolean().default(false).description('允许子 Agent 抢占宠物状态'),
}).description('由 DeepSeek Harness 状态驱动的桌面小鲸鱼伴侣')

const defaults = Object.freeze({
  enabled: true,
  characterProportion: 'chibi',
  scale: 1,
  bubbleScale: 1,
  activityLevel: 'normal',
  reducedMotion: false,
  bubbleMode: 'always',
  bubbleStates: ['SUCCESS', 'ERROR', 'WAITING'],
  includeSubagents: false,
})

function publicConfig(config = {}) {
  return {
    enabled: config.enabled ?? defaults.enabled,
    characterProportion: config.characterProportion ?? defaults.characterProportion,
    scale: config.scale ?? defaults.scale,
    bubbleScale: config.bubbleScale ?? defaults.bubbleScale,
    activityLevel: config.activityLevel ?? defaults.activityLevel,
    reducedMotion: config.reducedMotion ?? defaults.reducedMotion,
    bubbleMode: config.bubbleMode ?? defaults.bubbleMode,
    bubbleStates: Array.isArray(config.bubbleStates) ? config.bubbleStates : defaults.bubbleStates,
    includeSubagents: config.includeSubagents ?? defaults.includeSubagents,
  }
}

function localSettingsScope(value) {
  return {
    get: () => value,
    watch: () => () => {},
  }
}

function jsonResponse(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(payload),
  })
  res.end(payload)
}

function isLoopback(address) {
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1'
}

async function readPatch(req) {
  const chunks = []
  let bytes = 0
  for await (const chunk of req) {
    bytes += chunk.length
    if (bytes > 8192) throw new Error('request body is too large')
    chunks.push(chunk)
  }
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('patch must be an object')
  const allowed = new Set(Object.keys(defaults))
  if (Object.keys(value).some((key) => !allowed.has(key))) throw new Error('patch contains an unknown setting')
  return value
}

export function createConfigHandler(settings) {
  return async (req, res) => {
    if (!isLoopback(req.socket?.remoteAddress)) {
      jsonResponse(res, 403, { error: 'local access only' })
      return
    }
    const origin = req.headers?.origin
    if (origin) {
      let originHost
      try { originHost = new URL(origin).host } catch {}
      if (!originHost || originHost !== req.headers.host) {
        jsonResponse(res, 403, { error: 'origin mismatch' })
        return
      }
    }
    if (req.method === 'GET') {
      jsonResponse(res, 200, settings.get())
      return
    }
    if (req.method !== 'PATCH') {
      jsonResponse(res, 405, { error: 'method not allowed' })
      return
    }
    try {
      await settings.update(await readPatch(req))
      jsonResponse(res, 200, settings.get())
    } catch (error) {
      jsonResponse(res, 400, { error: error instanceof Error ? error.message : String(error) })
    }
  }
}


// DSH names an approval's choices differently depending on the tool that asked,
// so the shapes are tried in order rather than assuming one. A tool that offers
// nothing selectable falls through to `null` and the pet just shows the waiting
// state, leaving the web UI as the place to answer -- which is the honest
// outcome, because a card with no usable choice would hide the bubble's normal
// content and give the user nothing to click.
/**
 * Pull the questions out of an `ask_user_question` tool call.
 *
 * This is the tool the agent uses for "confirm this / pick one", and it is the
 * thing worth answering from the bubble. It is a *tool call*, not an approval:
 * approval/asked is the separate permission gate for running a tool at all.
 * The argument key differs between DSH builds, so the known spellings are tried
 * rather than assumed.
 */
export function askFromQuestionTool(event) {
  const data = event?.data ?? {}
  const args =
    data.arguments ?? data.args ?? data.input ?? data.parameters ?? data.message?.arguments ?? null
  const questions = Array.isArray(args?.questions) ? args.questions : null
  if (!questions || questions.length === 0) return null
  // One card at a time: the bubble shows a single question, and a second one
  // would need somewhere to queue.
  const first = questions[0]
  const id = String(first?.id ?? data.callId ?? data.id ?? '')
  if (!id) return null
  const options = Array.isArray(first?.options) ? first.options : null
  if (!options || options.length === 0) return null
  try {
    return createAsk({
      id,
      question: String(first.question ?? ''),
      detail: String(first.header ?? ''),
      options,
    })
  } catch {
    return null
  }
}

export function askFromApproval(event) {
  const data = event?.data ?? {}
  const id = String(data.id ?? '')
  if (!id) return null
  const raw =
    (Array.isArray(data.options) && data.options)
    || (Array.isArray(data.choices) && data.choices)
    || (Array.isArray(data.actions) && data.actions)
    || null
  const options = raw
    ? raw
    : [
        { value: 'approve', label: '允许' },
        { value: 'deny', label: '拒绝' },
      ]
  const question =
    String(data.question ?? data.prompt ?? data.title ?? '').trim()
    || `是否允许 ${String(data.toolName ?? 'this tool')}？`
  try {
    return createAsk({ id, question, options, detail: String(data.detail ?? data.toolName ?? '') })
  } catch {
    return null
  }
}

function mount(ctx, config = {}, eventCtx = ctx) {
  const logger = ctx.logger ?? console
  const base = publicConfig(config)
  const settings = ctx.settings?.register?.('dsh-dafeiyu', Config, {
    base,
    applies: 'live',
  }) ?? localSettingsScope(base)

  let bridge
  let reducer
  let restartTimer
  // Which session each pending approval belongs to, so an answer goes back to
  // the session that asked rather than to whichever one is current.
  const pendingApprovals = new Map()

  const submitAnswer = ({ id, value }) => {
    const session = pendingApprovals.get(id)
    pendingApprovals.delete(id)
    if (!session) {
      logger.warn?.(`dsh-dafeiyu: answer for unknown approval ${id}`)
      return
    }
    // The host decides approvals; the shape of that call is DSH's, not this
    // plugin's, so the known spellings are tried and anything else is reported
    // rather than silently swallowed. A dropped answer leaves the agent
    // blocked, which is worse than a loud log.
    // ask_user_question is a consumer of the ctx.userInteraction seam, and the
    // answer it expects is { id, selected: string[] } -- selected is an array
    // because the tool supports multi-select. The provider side of that seam is
    // whatever UI is attached; this tries the shapes a provider plausibly
    // exposes and reports rather than swallowing, because a dropped answer
    // leaves the agent blocked on a question nobody can see any more.
    const answer = { id, selected: [value] }
    const submit =
      ctx.userInteraction?.answer?.bind(ctx.userInteraction)
      ?? ctx.userInteraction?.resolve?.bind(ctx.userInteraction)
      ?? session.userInteraction?.answer?.bind(session.userInteraction)
      ?? session.answerQuestion?.bind(session)
      ?? session.decideApproval?.bind(session)
    if (typeof submit !== 'function') {
      logger.error?.(
        'dsh-dafeiyu: the pet answered a question but this DSH build exposes no provider seam to '
        + 'submit it through; answer it in the web UI instead. Expected ctx.userInteraction.',
      )
      return
    }
    try {
      submit(answer)
    } catch (error) {
      logger.error?.('dsh-dafeiyu failed to submit an answer', error)
    }
  }

  const stopRuntime = (reason = 'settings-change') => {
    bridge?.stop(reason)
    bridge = undefined
    reducer = undefined
  }

  const restartRuntime = (next) => {
    stopRuntime('settings-change')
    startRuntime(next)
  }

  const applyLiveSettings = (next) => {
    for (const message of reducer.setIncludeSubagents(next.includeSubagents === true)) bridge.send(message)
    bridge.send(createMessage(CompanionMessageKind.CONFIG, {
      characterProportion: next.characterProportion ?? defaults.characterProportion,
      scale: next.scale ?? defaults.scale,
      bubbleScale: next.bubbleScale ?? defaults.bubbleScale,
      activityLevel: next.activityLevel ?? defaults.activityLevel,
      reducedMotion: next.reducedMotion === true,
      bubbleMode: next.bubbleMode ?? defaults.bubbleMode,
      bubbleStates: Array.isArray(next.bubbleStates) ? next.bubbleStates : defaults.bubbleStates,
    }))
  }

  const scheduleRestart = (next) => {
    if (restartTimer) clearTimeout(restartTimer)
    restartTimer = setTimeout(() => {
      restartTimer = undefined
      restartRuntime(next)
    }, 400)
    restartTimer.unref?.()
  }

  const startRuntime = (resolved) => {
    if (resolved.enabled === false) {
      logger.info?.('dsh-dafeiyu is disabled')
      return
    }
    const helperConfig = config.helper ?? {}
    bridge = new HelperProcess({
      ...helperConfig,
      onAnswer: submitAnswer,
      env: {
        ...helperConfig.env,
        DSH_DAFEIYU_PROPORTION: String(resolved.characterProportion ?? defaults.characterProportion),
        DSH_DAFEIYU_SCALE: String(resolved.scale ?? defaults.scale),
        DSH_DAFEIYU_BUBBLE_SCALE: String(resolved.bubbleScale ?? defaults.bubbleScale),
        DSH_DAFEIYU_ACTIVITY_LEVEL: String(resolved.activityLevel ?? defaults.activityLevel),
        DSH_DAFEIYU_REDUCED_MOTION: resolved.reducedMotion === true ? '1' : '0',
        DSH_DAFEIYU_BUBBLE_MODE: String(resolved.bubbleMode ?? defaults.bubbleMode),
        DSH_DAFEIYU_BUBBLE_STATES: (Array.isArray(resolved.bubbleStates) ? resolved.bubbleStates : defaults.bubbleStates).join(','),
        DSH_DAFEIYU_WEBUI_URL: String(config.webuiUrl ?? process.env.DSH_DAFEIYU_WEBUI_URL ?? 'http://127.0.0.1:3080/'),
      },
    }, logger)
    reducer = new CompanionReducer({ includeSubagents: resolved.includeSubagents === true })
    bridge.start()
    bridge.send(createMessage(CompanionMessageKind.HELLO, {
      state: CompanionState.IDLE,
      host: 'deepseek-harness',
      pluginVersion: pkg.version,
      message: `${characterName} connected to DSH`,
    }))
    bridge.send(createMessage(CompanionMessageKind.STATE, {
      state: CompanionState.IDLE,
      phase: 'plugin-start',
      stage: '等待任务',
      message: statusCopy('idle', 0),
      detail: 'DSH · 等待下一次任务',
    }))
    logger.info?.('dsh-dafeiyu companion bridge started')
  }

  startRuntime(settings.get())

  // The companion intentionally observes every DSH session. Loader entries may
  // live inside a scoped composition, so use the unscoped root bus and dispose
  // the registrations explicitly with this plugin's lifecycle.
  // Never let an exception from this optional companion escape into the shared
  // session bus: a throw here could stop every other subscriber from seeing
  // the event, which would look exactly like "installing the pet broke other
  // plugins".
  const offEvent = eventCtx.on('session/event', (session, event) => {
    if (!bridge || !reducer) return
    try {
      for (const message of reducer.handle(session, event)) bridge.send(message)
      if (event?.type === 'tool/call') {
        const ask = askFromQuestionTool(event)
        if (ask) {
          pendingApprovals.set(ask.id, session)
          bridge.send(createMessage(CompanionMessageKind.ASK, ask))
        }
      } else if (event?.type === 'tool/result') {
        // Answered somewhere else, or the tool finished. Either way the
        // question is no longer live.
        const id = String(event.data?.callId ?? event.data?.id ?? '')
        if (id && pendingApprovals.delete(id)) {
          bridge.send(createMessage(CompanionMessageKind.ASK_CLEAR, { id }))
        }
      } else if (event?.type === 'approval/asked') {
        const ask = askFromApproval(event)
        if (ask) {
          pendingApprovals.set(ask.id, session)
          bridge.send(createMessage(CompanionMessageKind.ASK, ask))
        }
      } else if (event?.type === 'approval/decided') {
        // Decided elsewhere -- the web UI, the CLI, a timeout. Take the card
        // down rather than leaving a question the agent has already moved past.
        const id = String(event.data?.id ?? '')
        if (id) pendingApprovals.delete(id)
        bridge.send(createMessage(CompanionMessageKind.ASK_CLEAR, { id }))
      }
    } catch (error) {
      logger.error?.('dsh-dafeiyu failed to handle session event', error)
    }
  }, { global: true })
  const offDisposed = eventCtx.on('session/disposed', (session) => {
    if (!bridge || !reducer) return
    try {
      for (const message of reducer.disposeSession(session)) bridge.send(message)
    } catch (error) {
      logger.error?.('dsh-dafeiyu failed to dispose session', error)
    }
  }, { global: true })

  const unwatch = settings.watch((next) => {
    // Disabling is the only path that tears the helper down.  Every other
    // setting is applied live through a CONFIG message, so sliders never
    // restart the pet.  Starting a previously-disabled runtime is debounced
    // to avoid spawning repeatedly while settings settle.
    if (next.enabled === false) {
      if (restartTimer) {
        clearTimeout(restartTimer)
        restartTimer = undefined
      }
      stopRuntime('settings-change')
      return
    }
    if (!bridge) {
      scheduleRestart(next)
      return
    }
    if (restartTimer) {
      clearTimeout(restartTimer)
      restartTimer = undefined
    }
    applyLiveSettings(next)
  })
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (httpCtx) => {
      httpCtx.effect(
        () => httpCtx.webServer.register({ kind: 'exact', path: CONFIG_ENDPOINT, handler: createConfigHandler(settings) }),
        'dsh-dafeiyu: local settings endpoint',
      )
    })
  }
  ctx.effect(() => () => {
    if (restartTimer) clearTimeout(restartTimer)
    restartTimer = undefined
    offEvent?.()
    offDisposed?.()
    unwatch()
    stopRuntime('dsh-host-stop')
  })
}

export function apply(ctx, config = {}) {
  if (typeof ctx.inject === 'function') {
    ctx.inject(['settings'], (settingsCtx) => mount(settingsCtx, config, ctx))
    return
  }
  mount(ctx, config)
}

export {
  CompanionMessageKind,
  CompanionReducer,
  CompanionState,
  HelperProcess,
}
