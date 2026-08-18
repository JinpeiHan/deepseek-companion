import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  activityCopy,
  activityStage,
  characterName,
  personaCopyLibrary,
  statusCopy,
  statusCopyLibrary,
  taskCopy,
} from '../src/status-copy.js'

test('status copy stays varied, friendly, and deterministic', () => {
  assert.ok(Object.values(statusCopyLibrary).every((variants) => variants.length >= 2))
  assert.equal(statusCopy('success', 1), statusCopy('success', 1))
  assert.notEqual(statusCopy('success', 1), statusCopy('success', 2))
  assert.match(statusCopy('waiting', 0), /你|确认/u)
})

test('activity copy hides technical tool names behind human stages', () => {
  assert.equal(activityStage('testing'), '验证阶段')
  assert.match(activityCopy('searching', 0), /找|查看/u)
  assert.doesNotMatch(activityCopy('commanding', 1), /shell_command/u)
})

test('task copy adds restrained conversational particles', () => {
  assert.equal(taskCopy('修改登录模块'), '正在修改登录模块呢')
  assert.equal(taskCopy('正在运行测试'), '正在运行测试呢')
  assert.equal(taskCopy('Ship the release'), '正在处理「Ship the release」呢')
})

test('shared persona copy names the visible character and covers every state group', async () => {
  const source = JSON.parse(await readFile(new URL('../assets/persona-copy.zh-CN.json', import.meta.url), 'utf8'))
  assert.equal(characterName, '小鲸鱼')
  assert.equal(personaCopyLibrary.characterName, source.characterName)
  assert.deepEqual(statusCopyLibrary, source.status)
  assert.deepEqual(Object.keys(source.status).sort(), [
    'approval', 'commanding', 'editing', 'error', 'idle', 'limit', 'preparing',
    'result', 'searching', 'stopped', 'success', 'testing', 'thinking',
    'toolError', 'waiting', 'working',
  ])
})

test('persona copy is restrained and keeps task facts verbatim', () => {
  assert.equal(taskCopy('修改登录模块'), '正在修改登录模块呢')
  assert.match(statusCopy('waiting', 0), /主人|确认/u)
  assert.doesNotMatch(statusCopy('editing', 0), /主人/u)
  assert.doesNotMatch(Object.values(statusCopyLibrary).flat().join('\n'), /大肥鱼/u)
  assert.match(taskCopy('Ship the release'), /Ship the release/u)
})
