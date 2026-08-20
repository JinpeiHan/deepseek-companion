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

import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { assertDecodable, confine, decodeRgba, listPngs, readHeader } from './lib/png.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const EDGE_ALPHA = 8

// Which packs to check comes from the registry rather than a list here, so
// this cannot drift from what the runtime can actually load. Only frame packs
// belong to this validator: a rig pack has no 512x512 frame grid to compare
// against the chibi action matrix, and validate-rig.mjs covers those instead.
// chibi is the reference the others are compared to, so it is not a subject.
const registeredFramePacks = async () => {
  const registry = await readJson('assets/pet-packs.json')
  const packs = {}
  for (const [id, entry] of Object.entries(registry.packs)) {
    if (id === registry.defaultPack) continue
    const manifest = await readJson(`assets/${entry.manifest}`)
    if ((manifest.renderer ?? 'frames') !== 'frames') continue
    packs[id] = { manifest: `assets/${entry.manifest}`, root: `assets/${entry.root}` }
  }
  return packs
}

const readJson = async (relativePath) => JSON.parse(await readFile(resolve(ROOT, relativePath), 'utf8'))

// The proportion packs are the one place that still requires a fixed 512x512
// RGBA frame; the decoder in ./lib/png.mjs stays size-agnostic so the rig
// validator can share it.
const readPackHeader = (buffer, label) => {
  const header = readHeader(buffer, label)
  const { width, height } = header
  if (width !== 512 || height !== 512) throw new Error(`${label}: expected 512x512, got ${width}x${height}`)
  assertDecodable(header, label)
  return { width, height }
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

const validateSamples = async (dir) => {
  const target = resolve(ROOT, dir)
  const files = await listPngs(target)
  if (files.length === 0) throw new Error(`no PNG samples found in ${dir}`)
  for (const file of files) {
    const buffer = await readFile(resolve(target, file))
    readPackHeader(buffer, file)
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
  const packs = await registeredFramePacks()
  let failures = 0
  const declaredCounts = []
  if (Object.keys(packs).length === 0) {
    console.log('no non-default frame packs registered; rig packs are checked by validate-rig.mjs')
  }
  for (const [pack, meta] of Object.entries(packs)) {
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
      readPackHeader(buffer, `${pack}/${frame}`)
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
