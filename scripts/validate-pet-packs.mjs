#!/usr/bin/env node
// Strict validation for the generated proportion packs.
//
//   node scripts/validate-pet-packs.mjs
//     validates assets/pet-standard and assets/pet-slender against their
//     manifests and against the chibi action matrix.
//
//   node scripts/validate-pet-packs.mjs --samples <dir>
//     validates loose sample PNGs: 512x512 RGBA, real transparency, and a
//     character that does not touch the frame border.

import { readFile, readdir } from 'node:fs/promises'
import { inflateSync } from 'node:zlib'
import { dirname, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
const EDGE_ALPHA = 8

const PACKS = {
  standard: { manifest: 'assets/pet-standard-manifest.json', root: 'assets/pet-standard' },
  slender: { manifest: 'assets/pet-slender-manifest.json', root: 'assets/pet-slender' },
}

const readJson = async (relativePath) => JSON.parse(await readFile(resolve(ROOT, relativePath), 'utf8'))

const readHeader = (buffer, label) => {
  if (!buffer.subarray(0, 8).equals(SIGNATURE)) throw new Error(`${label}: not a PNG file`)
  if (buffer.readUInt32BE(8) !== 13 || buffer.toString('latin1', 12, 16) !== 'IHDR') {
    throw new Error(`${label}: malformed IHDR chunk`)
  }
  const width = buffer.readUInt32BE(16)
  const height = buffer.readUInt32BE(20)
  const bitDepth = buffer.readUInt8(24)
  const colorType = buffer.readUInt8(25)
  const interlace = buffer.readUInt8(28)
  if (width !== 512 || height !== 512) throw new Error(`${label}: expected 512x512, got ${width}x${height}`)
  if (colorType !== 6) throw new Error(`${label}: expected RGBA color type 6, got ${colorType}`)
  if (bitDepth !== 8) throw new Error(`${label}: expected bit depth 8, got ${bitDepth}`)
  if (interlace !== 0) throw new Error(`${label}: interlaced PNG is not supported`)
  return { width, height }
}

const paeth = (a, b, c) => {
  const p = a + b - c
  const pa = Math.abs(p - a)
  const pb = Math.abs(p - b)
  const pc = Math.abs(p - c)
  if (pa <= pb && pa <= pc) return a
  return pb <= pc ? b : c
}

const decodeRgba = (buffer, label) => {
  const { width, height } = readHeader(buffer, label)
  const chunks = []
  let offset = 8
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset)
    const type = buffer.toString('latin1', offset + 4, offset + 8)
    if (type === 'IDAT') chunks.push(buffer.subarray(offset + 8, offset + 8 + length))
    if (type === 'IEND') break
    offset += length + 12
  }
  if (chunks.length === 0) throw new Error(`${label}: no IDAT data`)
  const raw = inflateSync(Buffer.concat(chunks))
  const bpp = 4
  const stride = width * bpp
  const pixels = Buffer.alloc(height * stride)
  for (let y = 0; y < height; y += 1) {
    const filter = raw[y * (stride + 1)]
    const line = raw.subarray(y * (stride + 1) + 1, y * (stride + 1) + 1 + stride)
    const out = pixels.subarray(y * stride, (y + 1) * stride)
    const prior = y === 0 ? null : pixels.subarray((y - 1) * stride, y * stride)
    for (let x = 0; x < stride; x += 1) {
      const left = x >= bpp ? out[x - bpp] : 0
      const up = prior ? prior[x] : 0
      const upLeft = prior && x >= bpp ? prior[x - bpp] : 0
      const value = line[x]
      if (filter === 0) out[x] = value
      else if (filter === 1) out[x] = (value + left) & 0xff
      else if (filter === 2) out[x] = (value + up) & 0xff
      else if (filter === 3) out[x] = (value + ((left + up) >> 1)) & 0xff
      else if (filter === 4) out[x] = (value + paeth(left, up, upLeft)) & 0xff
      else throw new Error(`${label}: unknown PNG filter ${filter}`)
    }
  }
  return { width, height, pixels }
}

const inspectAlpha = ({ width, height, pixels }) => {
  let transparent = 0
  let opaque = 0
  let edgeInk = 0
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const alpha = pixels[(y * width + x) * 4 + 3]
      if (alpha <= EDGE_ALPHA) transparent += 1
      else opaque += 1
      const onEdge = x === 0 || y === 0 || x === width - 1 || y === height - 1
      if (onEdge && alpha > EDGE_ALPHA) edgeInk += 1
    }
  }
  return { transparent, opaque, edgeInk }
}

const confine = (rootDir, filePath, label) => {
  const rel = relative(rootDir, filePath)
  if (rel.startsWith('..') || rel.startsWith(sep) || rel === '') throw new Error(`${label}: path escapes pack root`)
}

const listPngs = async (dir, base = dir) => {
  const entries = await readdir(dir, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const full = resolve(dir, entry.name)
    if (entry.isDirectory()) files.push(...(await listPngs(full, base)))
    else if (entry.name.toLowerCase().endsWith('.png')) files.push(relative(base, full).split(sep).join('/'))
  }
  return files.sort()
}

const validateSamples = async (dir) => {
  const target = resolve(ROOT, dir)
  const files = await listPngs(target)
  if (files.length === 0) throw new Error(`no PNG samples found in ${dir}`)
  for (const file of files) {
    const buffer = await readFile(resolve(target, file))
    const image = decodeRgba(buffer, file)
    const { transparent, edgeInk } = inspectAlpha(image)
    if (transparent === 0) throw new Error(`${file}: no transparent pixels`)
    if (edgeInk > 0) throw new Error(`${file}: character touches the frame border (${edgeInk} opaque edge pixels)`)
    const coverage = ((512 * 512 - transparent) / (512 * 512)) * 100
    console.log(`${file}: 512x512 RGBA OK, ${coverage.toFixed(1)}% opaque coverage, border clear`)
  }
  console.log(`${files.length} samples OK`)
}

const validatePacks = async () => {
  const chibi = await readJson('assets/pet-manifest.json')
  let failures = 0
  const declaredCounts = []
  for (const [pack, meta] of Object.entries(PACKS)) {
    const manifest = await readJson(meta.manifest)
    const packRoot = resolve(ROOT, meta.root)
    const declared = []
    for (const [clip, definition] of Object.entries(manifest.clips)) {
      const reference = chibi.clips[clip]
      if (!reference) throw new Error(`${pack}: clip ${clip} is not part of the chibi action matrix`)
      if (definition.frames.length !== reference.frames.length) {
        throw new Error(`${pack}/${clip}: frame count ${definition.frames.length} != ${reference.frames.length}`)
      }
      if (definition.frameMs !== reference.frameMs) throw new Error(`${pack}/${clip}: frameMs differs from chibi`)
      if (definition.loop !== reference.loop) throw new Error(`${pack}/${clip}: loop differs from chibi`)
      if ((definition.motion ?? null) !== (reference.motion ?? null)) {
        throw new Error(`${pack}/${clip}: motion differs from chibi`)
      }
      declared.push(...definition.frames)
    }
    // The pack may fund a subset of the chibi action matrix, but nothing the
    // helper resolves without a fallback is allowed to point outside that subset.
    for (const [state, clip] of Object.entries(manifest.stateMap)) {
      if (!manifest.clips[clip]) throw new Error(`${pack}: stateMap.${state} points at missing clip ${clip}`)
    }
    for (const [activity, clip] of Object.entries(manifest.workingActivityMap ?? {})) {
      if (!manifest.clips[clip]) {
        throw new Error(`${pack}: workingActivityMap.${activity} points at missing clip ${clip}`)
      }
    }
    for (const clip of manifest.idleMicroClips ?? []) {
      if (!manifest.clips[clip]) throw new Error(`${pack}: idleMicroClips references missing clip ${clip}`)
    }
    if (declared.length === 0) throw new Error(`${pack}: manifest declares no frames`)
    declaredCounts.push(declared.length)

    let present = []
    try {
      present = await listPngs(packRoot)
    } catch {
      console.error(`${pack}: asset directory ${meta.root} does not exist yet (0/50 frames)`)
      failures += 1
      continue
    }
    const declaredSorted = [...declared].sort()
    const missing = declaredSorted.filter((frame) => !present.includes(frame))
    const extra = present.filter((frame) => !declaredSorted.includes(frame))
    if (missing.length > 0 || extra.length > 0) {
      console.error(`${pack}: ${missing.length} missing, ${extra.length} unexpected PNG files`)
      if (missing.length > 0) console.error(`  first missing: ${missing[0]}`)
      if (extra.length > 0) console.error(`  first unexpected: ${extra[0]}`)
      failures += 1
      continue
    }
    for (const frame of declaredSorted) {
      const filePath = resolve(packRoot, frame)
      confine(packRoot, filePath, `${pack}/${frame}`)
      const buffer = await readFile(filePath)
      readHeader(buffer, `${pack}/${frame}`)
    }
    console.log(`${pack}: ${Object.keys(manifest.clips).length} clips, ${declaredSorted.length} RGBA frames OK`)
  }
  if (new Set(declaredCounts).size > 1) {
    console.error(`packs are asymmetric: ${declaredCounts.join(' vs ')} declared frames`)
    failures += 1
  }
  if (failures > 0) process.exitCode = 1
}

const args = process.argv.slice(2)
const sampleFlag = args.indexOf('--samples')
if (sampleFlag !== -1) {
  const dir = args[sampleFlag + 1]
  if (!dir) throw new Error('--samples requires a directory')
  await validateSamples(dir)
} else {
  await validatePacks()
}
