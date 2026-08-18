#!/usr/bin/env node
// Drives gpt-image-2 over the 100 proportion-pack generation jobs.
//
//   node scripts/generate-openai-assets.mjs --samples --dry-run
//   node scripts/generate-openai-assets.mjs --samples
//   node scripts/generate-openai-assets.mjs --packs standard [--clips idle,blink] [--limit N] [--resume]
//
// Batch runs refuse to start until all four samples are explicitly approved in
// art-references/openai-sample-approval.json. Requests are issued one at a time
// in clip order so each frame can see the previous frame of its own clip.

import { access, mkdir, readFile, rename, writeFile, appendFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadOpenAiImageConfig, defaultDshHome, IMAGE_MODEL } from './openai-image-config.mjs'
import { createOpenAiImageClient } from './openai-image-client.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const SAMPLE_KEYS = ['standard/idle/0', 'standard/head_pat/0', 'slender/idle/0', 'slender/head_pat/0']

const JOBS_FILE = 'art-references/generation-jobs.json'
const APPROVAL_FILE = 'art-references/openai-sample-approval.json'
const RECORDS_FILE = 'art-references/openai-generation-records.jsonl'
const RAW_DIR = 'art-references/openai-raw'
const SAMPLE_DIR = 'art-references/openai-samples'

const SAMPLE_OUTPUTS = {
  'standard/idle/0': `${SAMPLE_DIR}/standard-idle.png`,
  'standard/head_pat/0': `${SAMPLE_DIR}/standard-head-pat.png`,
  'slender/idle/0': `${SAMPLE_DIR}/slender-idle.png`,
  'slender/head_pat/0': `${SAMPLE_DIR}/slender-head-pat.png`,
}

// The endpoint rejects `background=transparent` for gpt-image-2, so the model is
// asked for a flat pure-white plate that u2netp can matte away cleanly, and the
// alpha channel is cut by scripts/remove-image-background.py afterwards.
const PROMPT_SUFFIX = [
  'Q版参考只用于动作、视角和表情，不得继承二头身比例。',
  '严格保持当前比例母图的脸型、发型、服装、围裙鲸鱼图案、鲸耳、鞋袜和分叉鲸尾。',
  '只改变本帧动作需要变化的局部；无文字、无边框、无家具、无房间、无地面阴影。',
  '画面必须是 1:1 正方形构图，角色居中，四周留出空白边距。',
  '输出完整角色，背景为纯白色 #FFFFFF 单色平涂，角色轮廓与背景边界清晰可分离。',
].join('\n')

export const jobKey = (job) => `${job.pack}/${job.clip}/${job.frameIndex}`

export function selectJobs(jobs, { samples = false, packs = null, clips = null, limit = null } = {}) {
  let selected = jobs
  if (samples) {
    selected = SAMPLE_KEYS.map((key) => {
      const job = jobs.find((candidate) => jobKey(candidate) === key)
      if (!job) throw new Error(`sample job is missing from the job list: ${key}`)
      return job
    })
  }
  if (packs) selected = selected.filter((job) => packs.includes(job.pack))
  if (clips) selected = selected.filter((job) => clips.includes(job.clip))
  if (limit !== null) selected = selected.slice(0, limit)
  return selected
}

export async function readSampleApproval(file) {
  const reject = (reason) => new Error(`four OpenAI samples must be approved before batch generation (${reason})`)
  let parsed
  try {
    parsed = JSON.parse(await readFile(file, 'utf8'))
  } catch {
    throw reject(`cannot read ${file}`)
  }
  if (parsed?.model !== IMAGE_MODEL) throw reject(`approval model is not ${IMAGE_MODEL}`)
  const samples = parsed?.samples ?? {}
  for (const key of SAMPLE_KEYS) {
    if (samples[key] !== 'approved') throw reject(`${key} is ${samples[key] ?? 'missing'}`)
  }
  return samples
}

export async function buildEditRequest(job, root = ROOT) {
  const images = []
  const sources = [...job.references]
  if (job.previousFrame) {
    try {
      await access(resolve(root, job.previousFrame))
      sources.push(job.previousFrame)
    } catch {
      // The previous frame only exists once its own job has run; skipping it is
      // expected on a partial or resumed batch.
    }
  }
  for (const source of sources) {
    const filePath = resolve(root, source)
    let buffer
    try {
      buffer = await readFile(filePath)
    } catch {
      throw new Error(`missing reference for ${jobKey(job)}: ${source}`)
    }
    images.push({ name: source.split('/').pop(), buffer })
  }
  if (images.length < 4) throw new Error(`${jobKey(job)} needs at least four reference images`)
  return { prompt: `${job.prompt}\n${PROMPT_SUFFIX}`, images }
}

const parseArgs = (argv) => {
  const options = { dryRun: false, samples: false, packs: null, clips: null, limit: null, resume: false }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--dry-run') options.dryRun = true
    else if (arg === '--samples') options.samples = true
    else if (arg === '--resume') options.resume = true
    else if (arg === '--packs') options.packs = argv[++index]?.split(',').filter(Boolean) ?? null
    else if (arg === '--clips') options.clips = argv[++index]?.split(',').filter(Boolean) ?? null
    else if (arg === '--limit') options.limit = Number.parseInt(argv[++index], 10)
    else if (arg === '--probe') options.probe = true
    else throw new Error(`unknown flag: ${arg}`)
  }
  return options
}

const outputFor = (job, samples) => (samples ? SAMPLE_OUTPUTS[jobKey(job)] : job.output)

const writeAtomic = async (target, buffer) => {
  await mkdir(dirname(target), { recursive: true })
  const temporary = `${target}.tmp`
  await writeFile(temporary, buffer)
  await rename(temporary, target)
}

const recordAttempt = async (record) => {
  await mkdir(resolve(ROOT, 'art-references'), { recursive: true })
  await appendFile(resolve(ROOT, RECORDS_FILE), `${JSON.stringify(record)}\n`, 'utf8')
}

// Resume only asks "did this frame already come back from the API". The raw
// response is still an opaque RGB PNG at this point; 512x512 RGBA is enforced
// later by remove-image-background.py and validate-pet-packs.mjs.
const isValidPng = async (file) => {
  try {
    const buffer = await readFile(file)
    return (
      buffer.length > 100 &&
      buffer[0] === 0x89 &&
      buffer[1] === 0x50 &&
      buffer.toString('latin1', 12, 16) === 'IHDR' &&
      buffer.readUInt32BE(16) > 0 &&
      buffer.readUInt32BE(20) > 0
    )
  } catch {
    return false
  }
}

const main = async () => {
  const options = parseArgs(process.argv.slice(2))
  const { jobs } = JSON.parse(await readFile(resolve(ROOT, JOBS_FILE), 'utf8'))
  const selected = selectJobs(jobs, options)

  if (!options.samples && !options.probe) await readSampleApproval(resolve(ROOT, APPROVAL_FILE))

  if (options.dryRun) {
    for (const job of selected) {
      const request = await buildEditRequest(job)
      console.log(
        JSON.stringify({
          pack: job.pack,
          clip: job.clip,
          frameIndex: job.frameIndex,
          output: outputFor(job, options.samples),
          references: job.references,
          previousFrame: job.previousFrame,
          images: request.images.length,
        }),
      )
    }
    console.log(`dry run listed ${selected.length} jobs; no API request was sent`)
    return
  }

  const config = await loadOpenAiImageConfig({ dshHome: defaultDshHome() })
  const client = createOpenAiImageClient(config)

  if (options.probe) {
    const models = await client.listModels()
    console.log(`baseURL ${config.baseURL}`)
    console.log(`models ${models.length}`)
    console.log(`${IMAGE_MODEL} available=${models.includes(IMAGE_MODEL)}`)
    if (!models.includes(IMAGE_MODEL)) process.exitCode = 1
    return
  }

  let generated = 0
  for (const job of selected) {
    const relativeOutput = outputFor(job, options.samples)
    const target = resolve(ROOT, relativeOutput)
    if (options.resume && (await isValidPng(target))) {
      console.log(`skip ${jobKey(job)} (already generated)`)
      continue
    }
    const request = await buildEditRequest(job)
    const started = Date.now()
    try {
      const image = await client.editImage(request)
      const rawTarget = resolve(ROOT, RAW_DIR, `${job.pack}-${job.clip}-${String(job.frameIndex).padStart(2, '0')}.png`)
      await writeAtomic(rawTarget, image)
      await writeAtomic(target, image)
      generated += 1
      await recordAttempt({
        model: IMAGE_MODEL,
        pack: job.pack,
        clip: job.clip,
        frameIndex: job.frameIndex,
        output: relativeOutput,
        references: job.references,
        previousFrame: job.previousFrame,
        status: 'generated',
        attempts: 1,
        error: null,
      })
      console.log(`ok ${jobKey(job)} -> ${relativeOutput} (${image.length} bytes, ${Date.now() - started} ms)`)
    } catch (error) {
      await recordAttempt({
        model: IMAGE_MODEL,
        pack: job.pack,
        clip: job.clip,
        frameIndex: job.frameIndex,
        output: relativeOutput,
        references: job.references,
        previousFrame: job.previousFrame,
        status: 'failed',
        attempts: 1,
        error: String(error.message ?? error).slice(0, 300),
      })
      console.error(`fail ${jobKey(job)}: ${error.message ?? error}`)
      throw error
    }
  }
  console.log(`generated ${generated}/${selected.length} images with ${IMAGE_MODEL}`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main()
}
