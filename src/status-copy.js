import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const PERSONA_COPY = Object.freeze(require('../assets/persona-copy.zh-CN.json'))
const COPY = Object.freeze(PERSONA_COPY.status)

export const characterName = PERSONA_COPY.characterName
export const personaCopyLibrary = PERSONA_COPY

function seedNumber(seed) {
  const number = Number(seed)
  if (Number.isFinite(number)) return Math.abs(Math.trunc(number))
  return [...String(seed ?? '')].reduce((total, character) => total + character.codePointAt(0), 0)
}

export function statusCopy(group, seed = 0) {
  const variants = COPY[group] ?? COPY.working
  return variants[seedNumber(seed) % variants.length]
}

export function activityCopy(activity, seed = 0) {
  return statusCopy({
    searching: 'searching',
    editing: 'editing',
    testing: 'testing',
    commanding: 'commanding',
  }[activity] ?? 'working', seed)
}

export function activityStage(activity) {
  return {
    searching: '查找阶段',
    editing: '实现阶段',
    testing: '验证阶段',
    commanding: '执行阶段',
  }[activity] ?? '处理阶段'
}

export function taskCopy(task) {
  const value = String(task ?? '').trim().replace(/[。！？.!?]+$/u, '')
  if (!value) return statusCopy('working')
  if (/^(正在|继续)/u.test(value)) {
    return `${value}呢`
  }
  if (/^(准备|检查|验证|修改|修复|测试|构建|整理|分析|梳理|查找|搜索|读取|实现)/u.test(value)) {
    return `正在${value}呢`
  }
  return `正在处理「${value}」呢`
}

export { COPY as statusCopyLibrary }
