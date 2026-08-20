#!/usr/bin/env node
// Redraw a cropped sticker as a full body, so it can be used as a generation
// first frame without carrying its crop into every frame of the clip.
//
// Five of the sixteen pet-emoji stickers cut off below the knee, or sit on a
// full scene background that reaches the frame edge. Image-to-video matches the
// framing it is given, so a cropped first frame produces a clip with no feet in
// it -- and the feet are exactly what a desktop pet needs, because the runtime
// stands the character on a foot anchor.
//
// Padding cannot fix this: the legs are not in the picture to begin with. They
// have to be drawn. The pack's own full-body master goes in as a second
// reference so the legs, socks, shoes and tail that get invented are the ones
// the character already has, rather than a plausible guess.
//
// Credentials come from .env; see scripts/openai-image-config.mjs.

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadOpenAiImageConfig } from './openai-image-config.mjs'
import { createOpenAiImageClient } from './openai-image-client.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const parseArgs = () => {
  const args = { refs: [] }
  const argv = process.argv.slice(2)
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index]
    if (key === '--input') args.input = argv[++index]
    else if (key === '--output') args.output = argv[++index]
    else if (key === '--ref') args.refs.push(argv[++index])
    else if (key === '--pose') args.pose = argv[++index]
    else if (key === '--dry-run') args.dryRun = true
    else throw new Error(`unknown argument ${key}`)
  }
  if (!args.input || !args.output) throw new Error('--input and --output are required')
  if (args.refs.length === 0) throw new Error('at least one --ref full-body reference is required')
  return args
}

const main = async () => {
  const args = parseArgs()
  const name = basename(args.input).replace(/\.png$/u, '')
  const pose = args.pose || 'the exact pose, expression and gesture shown in the first reference image'

  const prompt = [
    `第一张参考图是这个角色的一个表情包，但它在膝盖以下被裁掉了，或者带有整幅场景背景。`,
    `请重新绘制同一个角色的【完整全身】：${pose}。`,
    `保持第一张图的姿势、手势、表情和情绪完全不变，只把缺失的下半身补全。`,
    `腿、白袜、深蓝玛丽珍鞋和身后的分叉鲸尾必须完整可见，双脚完整入画，不得裁切。`,
    `第二张参考图是这个角色的完整全身标准图，请照它来画下半身的比例、服装细节和鞋袜。`,
    `去掉原图上的所有文字、气泡、边框和背景装饰，只保留角色本身，背景为纯白。`,
    `角色完整居中，头顶和双脚四周都留有空白边距。`,
  ].join('')

  if (args.dryRun) {
    console.log(`${name}: would send ${args.refs.length + 1} images`)
    console.log(prompt)
    return
  }

  const config = await loadOpenAiImageConfig()
  const client = createOpenAiImageClient(config)

  const images = [{ name: `${name}.png`, buffer: await readFile(resolve(ROOT, args.input)) }]
  for (const ref of args.refs) {
    images.push({ name: basename(ref), buffer: await readFile(resolve(ROOT, ref)) })
  }

  const png = await client.editImage({ prompt, images })
  const out = resolve(ROOT, args.output)
  await mkdir(dirname(out), { recursive: true })
  await writeFile(out, png)
  console.log(`${name}: full body redrawn -> ${args.output} (${png.length} bytes)`)
}

await main()
