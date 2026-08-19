#!/usr/bin/env node
// Structural and pixel validation for the rig packs.
//
// The load-bearing check is the rest-pose recomposite. Every other assertion
// here can be satisfied by a pack that is quietly wrong -- a layer cut two
// pixels short still decodes, still trims tight, still has a pivot in bounds.
// Only stacking the layers back up and diffing against the master proves the
// cut actually partitioned the art.
//
// What it deliberately does NOT prove is fill quality. Occluded pixels are by
// definition invisible at rest, so the recomposite is blind to them; that is
// what the posed sweep in art-references/rig/<pack>/qa/pose-sweep.png is for,
// and why it is a human gate rather than a number.

import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { decodeRgba, readHeader, assertDecodable, confine } from './lib/png.mjs'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PACKS = ['standard', 'chibi', 'slender']
const CANVAS = 512
const ALPHA_FLOOR = 8

const COVERAGE_MIN = 0.995
const SPILL_MAX = 0.005
const MEAN_DELTA_MAX = 6

const failures = []
const fail = (label, message) => failures.push(`${label}: ${message}`)
const check = (condition, label, message) => {
  if (!condition) fail(label, message)
  return condition
}

const loadJson = async (path) => JSON.parse(await readFile(path, 'utf8'))

/** Depth-first walk of the bone forest: one root, no cycles, parents declared. */
const validateBones = (label, bones) => {
  const byId = new Map()
  for (const bone of bones) {
    if (byId.has(bone.id)) fail(label, `duplicate bone id ${bone.id}`)
    byId.set(bone.id, bone)
  }
  const roots = bones.filter((bone) => bone.parent === null || bone.parent === undefined)
  check(roots.length === 1, label, `expected exactly one root bone, found ${roots.length}`)
  for (const bone of bones) {
    if (bone.parent != null && !byId.has(bone.parent)) {
      fail(label, `bone ${bone.id} has unknown parent ${bone.parent}`)
    }
  }
  for (const bone of bones) {
    const seen = new Set()
    let cursor = bone.id
    while (cursor != null) {
      if (seen.has(cursor)) {
        fail(label, `bone ${bone.id} is part of a parent cycle`)
        break
      }
      seen.add(cursor)
      cursor = byId.get(cursor)?.parent ?? null
    }
  }
  return byId
}

const validatePack = async (pack) => {
  const label = `pet-${pack}-rig`
  const manifestPath = resolve(REPO_ROOT, 'assets', `${label}.json`)
  const manifest = await loadJson(manifestPath)
  const packRoot = resolve(REPO_ROOT, 'assets', label)

  check(manifest.formatVersion === 3, label, `formatVersion must be 3, got ${manifest.formatVersion}`)
  check(manifest.renderer === 'rig', label, `renderer must be "rig", got ${manifest.renderer}`)
  check(manifest.logicalWidth === 260 && manifest.logicalHeight === 260, label,
    `logical box must be 260x260, got ${manifest.logicalWidth}x${manifest.logicalHeight}`)
  check(manifest.footAnchor?.[0] === 0.5 && manifest.footAnchor?.[1] === 0.97, label,
    `footAnchor must be [0.5, 0.97], got ${JSON.stringify(manifest.footAnchor)}`)
  check(manifest.bubbleAnchor?.[0] === 0.5 && manifest.bubbleAnchor?.[1] === 0.04, label,
    `bubbleAnchor must be [0.5, 0.04], got ${JSON.stringify(manifest.bubbleAnchor)}`)
  check(manifest.parts.length <= 20, label, `${manifest.parts.length} parts exceeds the cap of 20`)

  const bones = validateBones(label, manifest.bones)
  const overflow = manifest.overflow ?? {}
  for (const bone of manifest.bones) {
    const [x, y] = bone.pivot
    check(
      x >= -overflow.left && x <= CANVAS + overflow.right &&
      y >= -overflow.top && y <= CANVAS + overflow.bottom,
      label, `bone ${bone.id} pivot ${JSON.stringify(bone.pivot)} is outside the padded canvas`,
    )
  }

  // -- per-layer geometry ------------------------------------------------- //
  const layers = new Map()
  for (const part of manifest.parts) {
    const partLabel = `${label}/${part.id}`
    check(!part.file.includes('..') && !part.file.includes('\\') && !part.file.startsWith('/'),
      partLabel, `unconfined part path ${part.file}`)
    const filePath = resolve(packRoot, part.file)
    confine(packRoot, filePath, partLabel)
    const buffer = await readFile(filePath)
    const header = readHeader(buffer, partLabel)
    assertDecodable(header, partLabel)

    const [rx, ry, rw, rh] = part.rect
    check(header.width === rw && header.height === rh, partLabel,
      `png is ${header.width}x${header.height} but rect says ${rw}x${rh}`)
    check(rx >= 0 && ry >= 0 && rx + rw <= CANVAS && ry + rh <= CANVAS, partLabel,
      `rect ${JSON.stringify(part.rect)} leaves the ${CANVAS}x${CANVAS} canvas`)
    check(bones.has(part.bone), partLabel, `references unknown bone ${part.bone}`)
    const [px, py] = part.pivot
    check(px >= rx - 64 && px <= rx + rw + 64 && py >= ry - 64 && py <= ry + rh + 64,
      partLabel, `pivot ${JSON.stringify(part.pivot)} is far outside rect ${JSON.stringify(part.rect)}`)
    for (const boneId of part.stripBones ?? []) {
      check(bones.has(boneId), partLabel, `strip bone ${boneId} is not declared`)
    }

    const { width, height, pixels } = decodeRgba(buffer, partLabel)
    let opaque = 0
    let solid = 0
    let firstRow = false, lastRow = false, firstCol = false, lastCol = false
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const alpha = pixels[(y * width + x) * 4 + 3]
        if (alpha === 0) continue
        opaque += 1
        if (alpha >= 250) solid += 1
        if (y === 0) firstRow = true
        if (y === height - 1) lastRow = true
        if (x === 0) firstCol = true
        if (x === width - 1) lastCol = true
      }
    }
    check(opaque >= 24, partLabel, `only ${opaque} non-transparent pixels; layer is effectively empty`)
    check(solid >= 1, partLabel, 'no fully opaque pixel; layer is a ghost')
    check(firstRow && lastRow && firstCol && lastCol, partLabel,
      'layer is not tight-trimmed (an edge row or column is fully transparent)')
    layers.set(part.id, { width, height, pixels, part })
  }

  // -- chain continuity ---------------------------------------------------- //
  for (const [name, chain] of Object.entries(manifest.chains ?? {})) {
    const parts = manifest.parts.filter((part) => chain.bones.includes(part.bone))
      .sort((a, b) => a.z - b.z)
    check(parts.length === chain.bones.length, `${label}/chain:${name}`,
      `${parts.length} parts for ${chain.bones.length} chain bones`)
    for (let index = 0; index + 1 < parts.length; index += 1) {
      const a = parts[index].rect
      const b = parts[index + 1].rect
      const overlapX = Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0])
      const overlapY = Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1])
      const overlap = Math.min(overlapX, overlapY)
      check(overlap >= 2, `${label}/chain:${name}`,
        `segments ${parts[index].id} and ${parts[index + 1].id} overlap only ${overlap}px; a bend would tear`)
    }
    check((chain.segmentParams ?? []).length === chain.bones.length, `${label}/chain:${name}`,
      'segmentParams must name one param per chain bone')
    for (const param of chain.segmentParams ?? []) {
      check(param in manifest.params, `${label}/chain:${name}`, `segment param ${param} is undeclared`)
    }
  }

  // -- rest-pose recomposite ---------------------------------------------- //
  const masterPath = resolve(REPO_ROOT, manifest.qa.master)
  const masterBuffer = await readFile(masterPath)
  const masterSha = createHash('sha256').update(masterBuffer).digest('hex')
  check(masterSha === manifest.qa.masterSha256, label,
    `master ${manifest.qa.master} has changed since the pack was cut (sha ${masterSha.slice(0, 12)} vs ${String(manifest.qa.masterSha256).slice(0, 12)}); re-run extract-rig-layers.py`)
  const master = decodeRgba(masterBuffer, `${label}/master`)
  check(master.width === CANVAS && master.height === CANVAS, label,
    `master is ${master.width}x${master.height}, expected ${CANVAS}x${CANVAS}`)

  // Every binding is neutral at its parameter default, so the rest pose is the
  // identity for every part -- except the eyelids, which the manifest names.
  const hidden = new Set(manifest.qa.restHiddenParts ?? [])
  const composite = Buffer.alloc(CANVAS * CANVAS * 4)
  const ordered = [...manifest.parts].sort((a, b) => a.z - b.z)
  for (const part of ordered) {
    if (hidden.has(part.id)) continue
    const { width, height, pixels } = layers.get(part.id)
    const [rx, ry] = part.rect
    for (let y = 0; y < height; y += 1) {
      const cy = ry + y
      if (cy < 0 || cy >= CANVAS) continue
      for (let x = 0; x < width; x += 1) {
        const cx = rx + x
        if (cx < 0 || cx >= CANVAS) continue
        const src = (y * width + x) * 4
        const alpha = pixels[src + 3] / 255
        if (alpha === 0) continue
        const dst = (cy * CANVAS + cx) * 4
        const under = composite[dst + 3] / 255
        const out = alpha + under * (1 - alpha)
        for (let channel = 0; channel < 3; channel += 1) {
          composite[dst + channel] = Math.round(
            (pixels[src + channel] * alpha + composite[dst + channel] * under * (1 - alpha)) / out,
          )
        }
        composite[dst + 3] = Math.round(out * 255)
      }
    }
  }

  let masterOpaque = 0
  let covered = 0
  let spilled = 0
  let deltaSum = 0
  let deltaCount = 0
  for (let index = 0; index < CANVAS * CANVAS; index += 1) {
    const offset = index * 4
    const inMaster = master.pixels[offset + 3] > ALPHA_FLOOR
    const inComposite = composite[offset + 3] > ALPHA_FLOOR
    if (inMaster) masterOpaque += 1
    if (inMaster && inComposite) {
      covered += 1
      deltaSum += Math.abs(composite[offset] - master.pixels[offset])
        + Math.abs(composite[offset + 1] - master.pixels[offset + 1])
        + Math.abs(composite[offset + 2] - master.pixels[offset + 2])
      deltaCount += 3
    }
    if (!inMaster && inComposite) spilled += 1
  }
  const coverage = covered / masterOpaque
  const spill = spilled / masterOpaque
  const meanDelta = deltaSum / Math.max(1, deltaCount)
  check(coverage >= COVERAGE_MIN, label,
    `rest recomposite covers only ${(coverage * 100).toFixed(3)}% of the master (need ${COVERAGE_MIN * 100}%)`)
  check(spill <= SPILL_MAX, label,
    `rest recomposite spills ${(spill * 100).toFixed(3)}% outside the master (max ${SPILL_MAX * 100}%)`)
  check(meanDelta <= MEAN_DELTA_MAX, label,
    `rest recomposite mean |dRGB| is ${meanDelta.toFixed(2)}/255 (max ${MEAN_DELTA_MAX})`)

  console.log(
    `${label}: ${manifest.parts.length} parts, coverage ${(coverage * 100).toFixed(3)}%, ` +
    `spill ${(spill * 100).toFixed(3)}%, mean |dRGB| ${meanDelta.toFixed(2)}/255`,
  )
  return manifest
}

const idsOf = (manifest) => ({
  parts: manifest.parts.map((part) => part.id).sort(),
  bones: manifest.bones.map((bone) => bone.id).sort(),
  chains: Object.keys(manifest.chains ?? {}).sort(),
  params: Object.keys(manifest.params ?? {}).sort(),
  clips: Object.keys(manifest.clips ?? {}).sort(),
  hitGroups: Object.keys(manifest.hitGroups ?? {}).sort(),
  interactions: Object.keys(manifest.interactions ?? {}).sort(),
  segmentParams: Object.values(manifest.chains ?? {}).flatMap((chain) => chain.segmentParams ?? []).sort(),
})

const main = async () => {
  const manifests = new Map()
  for (const pack of PACKS) {
    manifests.set(pack, await validatePack(pack))
  }
  // The two proportions are two cuts of one character: a driver, a clip, a hit
  // group or an interaction that exists for one and not the other would make
  // switching proportion at runtime a behaviour change, not a look change.
  const [first, ...rest] = PACKS
  const reference = idsOf(manifests.get(first))
  for (const pack of rest) {
    const other = idsOf(manifests.get(pack))
    for (const key of Object.keys(reference)) {
      const a = JSON.stringify(reference[key])
      const b = JSON.stringify(other[key])
      check(a === b, 'symmetry', `${first} and ${pack} disagree on ${key}:\n    ${a}\n    ${b}`)
    }
  }

  if (failures.length) {
    console.error(`\n${failures.length} rig validation failure(s):`)
    for (const message of failures) console.error(`  - ${message}`)
    process.exitCode = 1
    return
  }
  console.log(`\nOK: ${PACKS.length} rig pack(s) validated.`)
}

await main()
