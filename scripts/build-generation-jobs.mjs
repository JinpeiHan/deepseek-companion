#!/usr/bin/env node
// Builds the 100 image generation jobs (50 standard + 50 slender) from the two
// proportion manifests. Every job carries the current-proportion turnaround as
// the dominant reference plus the matching chibi runtime frame, which pins the
// exact pose, camera and expression of that frame without letting the chibi
// two-head proportion leak into the result.

import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const PACKS = {
  standard: {
    manifest: 'assets/pet-standard-manifest.json',
    root: 'assets/pet-standard',
    proportion:
      '约四头身的矮身少女比例：全身总高约等于 4 个头高，头部明显偏大、脖子短、躯干和四肢短而圆润，是介于二头身 Q 版和写实之间的“小体型”比例',
    silhouette: '短款深藏青女仆裙、白色荷叶边围裙、白色短袜与深蓝玛丽珍鞋',
  },
  slender: {
    manifest: 'assets/pet-slender-manifest.json',
    root: 'assets/pet-slender',
    proportion: '约六至七头身的修长少女比例：全身总高约等于 6.5 个头高，四肢修长',
    silhouette: '长及小腿的深藏青女仆长裙、白色荷叶边长围裙、白色长袜与深蓝玛丽珍鞋',
  },
}

const CHIBI_ROOT = 'assets/pet'

const ACTIONS = {
  idle: { view: '正面', action: '安静站立待机，双手自然垂在身侧，胸口因呼吸而极轻微起伏' },
  blink: { view: '正面', action: '站立不动地完成一次眨眼' },
  glance: { view: '正面', action: '站立不动地左右张望一次，只有头部和视线转动' },
  thinking: { view: '正面', action: '思考：一只手抬到下巴附近，视线略微上移，眉头轻挑' },
  working: { view: '正面', action: '双手握着扫帚打扫，身体微微前倾' },
  working_search: { view: '朝向画面左侧的侧面', action: '向左行走的循环' },
  working_command: { view: '朝向画面右侧的侧面', action: '向右行走的循环' },
  walk_start_left: { view: '朝向画面左侧的侧面', action: '从站立起步向左走' },
  walk_stop_left: { view: '朝向画面左侧的侧面', action: '从向左走收步回到站立' },
  walk_start_right: { view: '朝向画面右侧的侧面', action: '从站立起步向右走' },
  walk_stop_right: { view: '朝向画面右侧的侧面', action: '从向右走收步回到站立' },
  waiting: { view: '正面', action: '说话等待：一只手抬起做出示意手势，嘴微张' },
  success: { view: '正面', action: '开心：双手轻轻举起，眼睛弯成笑眼，身体轻快上扬' },
  error: { view: '正面', action: '生气：双手叉腰，鼓起脸颊，眉毛下压' },
  error_dizzy: { view: '正面', action: '眩晕：身体轻微歪斜，眼睛变成打转的漩涡状' },
  dragging: { view: '正面', action: '被从空中拎起拖动：双手上举，头发与裙摆向下垂坠，双脚离地' },
  head_pat: { view: '正面', action: '被摸头：肩膀微缩、眯眼享受，画面中不出现手或角色以外的任何物体' },
  poke: { view: '正面', action: '被戳到时的反应：身体一缩，露出惊讶表情' },
  tail: { view: '正面', action: '尾巴被碰到时的反应：上半身回头看向尾巴，鲸尾明显摆动' },
}

const phase = (index, total) => (total === 1 ? '完整姿势' : `第 ${index + 1}/${total} 阶段`)

const buildPrompt = (pack, clip, frameIndex, frameCount) => {
  const meta = PACKS[pack]
  const action = ACTIONS[clip]
  return [
    `第一张参考图是本帧唯一的比例权威：${meta.proportion}。请严格照抄它的头身比，不要改成更成熟或更修长的体型。`,
    '保持前四张参考图确立的脸型、蓝色渐变长发、白色荷叶边女仆头饰、头两侧的鲸鱼耳鳍、',
    `围裙上的白色小鲸鱼图案、裙摆金色纹样、后腰白色蝴蝶结、${meta.silhouette}，以及身后固定的分叉鲸尾。`,
    `本帧视角为${action.view}，动作是${action.action}的${phase(frameIndex, frameCount)}。`,
    '角色完整入画，不裁切头顶、双手、裙摆、鲸尾和双脚。',
  ].join('')
}

const readJson = async (relative) => JSON.parse(await readFile(resolve(ROOT, relative), 'utf8'))

const main = async () => {
  const chibi = await readJson('assets/pet-manifest.json')
  const jobs = []

  for (const [pack, meta] of Object.entries(PACKS)) {
    const manifest = await readJson(meta.manifest)
    // The first production pass funds a keyframe subset, so the manifest holds
    // a subset of the chibi action matrix rather than all 19 clips.
    const clipNames = Object.keys(manifest.clips)
    const unknown = clipNames.filter((clip) => !chibi.clips[clip])
    if (unknown.length > 0) {
      throw new Error(`${pack} manifest declares clips outside the chibi action matrix: ${unknown.join(', ')}`)
    }
    for (const clip of clipNames) {
      const frames = manifest.clips[clip].frames
      const chibiFrames = chibi.clips[clip].frames
      if (frames.length !== chibiFrames.length) {
        throw new Error(`${pack}/${clip} frame count differs from the chibi pack`)
      }
      if (!ACTIONS[clip]) throw new Error(`missing action description for clip: ${clip}`)
      frames.forEach((frame, frameIndex) => {
        jobs.push({
          pack,
          clip,
          frameIndex,
          output: `${meta.root}/${frame}`,
          references: [
            `art-references/${pack}/turnaround/front.jpg`,
            `art-references/${pack}/turnaround/side.jpg`,
            `art-references/${pack}/turnaround/back.jpg`,
            `art-references/${pack}/details/details.jpg`,
            `${CHIBI_ROOT}/${chibiFrames[frameIndex]}`,
          ],
          previousFrame: frameIndex === 0 ? null : `${meta.root}/${frames[frameIndex - 1]}`,
          prompt: buildPrompt(pack, clip, frameIndex, frames.length),
        })
      })
    }
  }

  const outputs = new Set(jobs.map((job) => job.output))
  if (outputs.size !== jobs.length) throw new Error('duplicate job output detected')
  const counts = Object.keys(PACKS).map((pack) => jobs.filter((job) => job.pack === pack).length)
  if (new Set(counts).size !== 1) throw new Error(`packs are asymmetric: ${counts.join(' vs ')}`)

  await writeFile(
    resolve(ROOT, 'art-references/generation-jobs.json'),
    `${JSON.stringify({ formatVersion: 1, jobs }, null, 2)}\n`,
    'utf8',
  )
  const standard = jobs.filter((job) => job.pack === 'standard').length
  const slender = jobs.filter((job) => job.pack === 'slender').length
  console.log(`generated ${jobs.length} image jobs (${standard} standard, ${slender} slender)`)
}

await main()
