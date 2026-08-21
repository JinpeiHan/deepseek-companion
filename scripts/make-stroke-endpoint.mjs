#!/usr/bin/env node
// Redraw a prop-holding pose at the other end of its stroke, so image-to-video
// has two different endpoints to move between.
//
// Sweeping defeated three prompt rewrites. MiniMax will happily move one arm a
// long way -- salute, point, shrug all came out fine -- but asking it to work
// both arms against a grounded prop produced the same thing every time: the
// broom held at a fixed angle while the whole character slid sideways. Measured
// on the shipped clip, the broom head travelled 19.8% of the body's width
// horizontally and 1.6% vertically. That is a character being translated, not a
// floor being swept.
//
// Pinning first and last frame to the *same* image is what froze the clips in
// the first place. Pinning them to two ends of the stroke is the opposite
// instruction: the model has to carry the prop from one to the other, and the
// arms have to follow because the hands are attached to it.
//
// Credentials come from .env; see scripts/openai-image-config.mjs.

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadOpenAiImageConfig } from './openai-image-config.mjs'
import { createOpenAiImageClient } from './openai-image-client.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const parseArgs = () => {
  const args = {}
  const argv = process.argv.slice(2)
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]
    if (key === '--input') args.input = argv[++i]
    else if (key === '--output') args.output = argv[++i]
    else if (key === '--pose') args.pose = argv[++i]
    else if (key === '--dry-run') args.dryRun = true
    else throw new Error(`unknown argument ${key}`)
  }
  if (!args.input || !args.output || !args.pose) {
    throw new Error('--input, --output and --pose are required')
  }
  return args
}

const main = async () => {
  const args = parseArgs()
  const name = basename(args.input).replace(/\.png$/u, '')

  const prompt = [
    '参考图里的角色正握着一件道具。请重新绘制【同一个角色、同一件道具】，',
    `只改变动作到这个瞬间：${args.pose}。`,
    '角色的身份、发型、发色、女仆装、围裙纹样、鞋袜和身后的分叉鲸尾必须与参考图完全一致，',
    '身高与头身比例也必须完全一致，脚踩的位置和画面里的大小不变。',
    '双脚完整入画不得裁切，头顶和四周留有空白边距，背景纯白，画面里只有角色和这件道具，',
    '没有文字、气泡、边框或任何其他物体。',
  ].join('')

  if (args.dryRun) {
    console.log(`${name}: would send 1 image\n${prompt}`)
    return
  }

  const config = await loadOpenAiImageConfig()
  const client = createOpenAiImageClient(config)
  const png = await client.editImage({
    prompt,
    images: [{ name: `${name}.png`, buffer: await readFile(resolve(ROOT, args.input)) }],
  })
  const out = resolve(ROOT, args.output)
  await mkdir(dirname(out), { recursive: true })
  await writeFile(out, png)
  console.log(`${name}: stroke endpoint drawn -> ${args.output} (${png.length} bytes)`)
}

await main()
