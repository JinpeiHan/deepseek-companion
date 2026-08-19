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
  // One in-flight question at a time, resolved when the pet is clicked.
  // ctx.userQuestions is a pull seam: the tool awaits the provider's ask(), so
  // the answer has to come back as a resolved promise rather than as an event
  // fired at the host.
  let pending

  const settlePending = (outcome, error) => {
    if (!pending) return
    const { resolve, reject, timer } = pending
    pending = undefined
    if (timer) clearTimeout(timer)
    bridge?.send(createMessage(CompanionMessageKind.ASK_CLEAR, {}))
    if (error) reject(error)
    else resolve(outcome)
  }

  const onPetAnswer = ({ id, value }) => {
    if (!pending || pending.id !== id) {
      logger.warn?.(`dsh-dafeiyu: an answer arrived for ${id}, which is no longer being asked`)
      return
    }
    // selected carries option *labels*: that is what the host echoes back.
    settlePending({ answers: [{ id, selected: [value] }] })
  }

  const askProvider = {
    ask(request) {
      const question = request?.questions?.[0]
      if (!question) return Promise.reject(new Error('ask_user_question requires a question'))
      // Only the first question is shown. A second would need somewhere to
      // queue, and a bubble that silently drops one is worse than declining.
      if (request.questions.length > 1) {
        return Promise.reject(new Error('the pet shows one question at a time; use the web UI'))
      }
      let ask
      try {
        ask = createAsk({
          id: question.id,
          question: question.question,
          detail: question.detail ?? question.header ?? '',
          options: question.options,
        })
      } catch (error) {
        // No options means nothing to click. Declining lets the host fall back
        // to a UI that can take free text, instead of the pet showing a card
        // with no way out of it.
        return Promise.reject(error)
      }
      if (!bridge || pending) {
        return Promise.reject(new Error('the pet is not available to ask right now'))
      }
      return new Promise((resolve, reject) => {
        pending = { id: ask.id, resolve, reject, timer: undefined }
        const onAbort = () => settlePending(undefined, new Error('ask_user_question was aborted'))
        if (request.signal) {
          if (request.signal.aborted) return onAbort()
          request.signal.addEventListener('abort', onAbort, { once: true })
        }
        bridge.send(createMessage(CompanionMessageKind.ASK, ask))
      })
    },
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
      onAnswer: onPetAnswer,
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
  // Only one provider may be active, so a duplicate is expected whenever a
  // richer UI is already attached -- report it and carry on showing states
  // rather than failing to mount.
  let offProvider
  try {
    offProvider = ctx.userQuestions?.registerProvider?.(askProvider)
    if (offProvider) logger.info?.('dsh-dafeiyu: answering questions in the pet bubble')
  } catch (error) {
    logger.warn?.(`dsh-dafeiyu: another user-questions provider is active; questions stay in that UI (${error?.message ?? error})`)
  }

  const offEvent = eventCtx.on('session/event', (session, event) => {
    if (!bridge || !reducer) return
    try {
      for (const message of reducer.handle(session, event)) bridge.send(message)
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
    offProvider?.()
    // A question still on screen has a tool awaiting its promise. Rejecting it
    // lets the agent fail and retry; leaving it pending hangs that tool for the
    // rest of the run.
    settlePending(undefined, new Error('the pet stopped while a question was open'))
    unwatch()
    stopRuntime('dsh-host-stop')
  })
}

export function apply(ctx, config = {}) {
  // ctx.inject waits for every listed service, so listing settings here kept
  // the pet unmounted on the command line just as surely as declaring it in
  // `inject` did -- mount() has handled its absence all along. Only wait for it
  // when the host actually has it.
  if (typeof ctx.inject === 'function' && ctx.settings !== undefined) {
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
