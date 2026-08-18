#!/usr/bin/env node
// Catches the two ways a generated frame pack looks wrong in motion even though
// every individual frame is a valid 512x512 RGBA PNG.
//
// 1. Scale pop. remove-image-background.py applies ONE scale per group and
//    preserves relative sizes inside it, so a frame that arrives oversized stays
//    oversized. assets/pet-slender/head_pat/head_pat_512_00.png shipped as a
//    byte-identical copy of the approved sample and rendered 27% taller than
//    every other slender frame, which reads as the character popping bigger for
//    one frame. Pose legitimately changes height (ducking, feet leaving the
//    ground), so this compares each frame against its clip's MEDIAN height and
//    only fails well outside that.
//
// 2. Dead frames. When build-generation-jobs.mjs emitted only an ordinal
//    ("第 3/5 阶段") the model redrew the same pose every frame, so a five-frame
//    blink was five copies of the same wink. A clip whose consecutive frames are
//    nearly identical is not animating.
//
// What this deliberately does NOT check: whether a blink closes BOTH eyes.
// Locating eyelids needs face landmarks the repo does not have, and a mirror
// symmetry heuristic drowns in the character's deliberately asymmetric hair.
// That one stays a human check -- see docs/ACCEPTANCE.md.

import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { decodeRgba } from './lib/png.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// A frame more than this far from its clip's median height is a scale pop.
const HEIGHT_TOLERANCE = 0.12
// Packs must stand the same height ON SCREEN; the acceptance criterion is 等高.
// Canvas sizes differ (chibi 238x260, the 512 packs 512x512), so heights are
// converted to logical units before comparing or the numbers are meaningless.
const CROSS_PACK_TOLERANCE = 0.1
// Below this fraction of changed pixels, consecutive frames are the same drawing.
// A single repeated frame is a legitimate hold at the extreme of a motion (chibi
// glance holds twice on purpose), so a lone duplicate only warns. A clip where
// MOST transitions are dead is not animating at all, which is the blink defect.
const MIN_FRAME_DELTA = 0.002
const DEAD_TRANSITION_RATIO = 0.5
const ALPHA_FLOOR = 8

const readJson = async (relative) => JSON.parse(await readFile(resolve(ROOT, relative), 'utf8'))

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b)
  const mid = sorted.length >> 1
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

const loadFrame = async (path, label) => {
  const { width, height, pixels } = decodeRgba(await readFile(path), label)
  const data = pixels
  let top = height
  let bottom = -1
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (data[(y * width + x) * 4 + 3] > ALPHA_FLOOR) {
        if (y < top) top = y
        bottom = y
        break
      }
    }
  }
  return { width, height, data, charHeight: bottom < 0 ? 0 : bottom - top + 1 }
}

// Fraction of pixels whose colour or alpha moved meaningfully between frames.
const frameDelta = (a, b) => {
  if (a.width !== b.width || a.height !== b.height) return 1
  let changed = 0
  for (let i = 0; i < a.data.length; i += 4) {
    const da = Math.abs(a.data[i + 3] - b.data[i + 3])
    const dc =
      Math.abs(a.data[i] - b.data[i]) +
      Math.abs(a.data[i + 1] - b.data[i + 1]) +
      Math.abs(a.data[i + 2] - b.data[i + 2])
    if (da > 24 || dc > 72) changed += 1
  }
  return changed / (a.data.length / 4)
}

const main = async () => {
  const registry = await readJson('assets/pet-packs.json')
  const failures = []
  const warnings = []
  const idleHeights = {}

  for (const [packId, entry] of Object.entries(registry.packs)) {
    const manifest = await readJson(`assets/${entry.manifest}`)
    const packRoot = resolve(ROOT, 'assets', entry.root)
    let checked = 0
    let skipped = 0

    for (const [clipName, clip] of Object.entries(manifest.clips)) {
      const frames = []
      let missing = false
      for (const frame of clip.frames) {
        const label = `${packId}/${frame}`
        try {
          frames.push(await loadFrame(resolve(packRoot, frame), label))
        } catch (error) {
          // Only an absent file means "not generated yet". A file that exists but
          // will not decode is a real defect and must not be silently skipped.
          if (error.code !== 'ENOENT') throw error
          missing = true
          break
        }
      }
      if (missing) {
        skipped += 1
        continue
      }
      checked += frames.length
      const logicalScale = (manifest.logicalHeight ?? manifest.maxFrameHeight) / (manifest.sourceHeight ?? manifest.maxFrameHeight)
      if (clipName === 'idle') idleHeights[packId] = frames[0].charHeight * logicalScale

      const heights = frames.map((frame) => frame.charHeight)
      const clipMedian = median(heights)
      heights.forEach((height, index) => {
        const drift = Math.abs(height - clipMedian) / clipMedian
        if (drift > HEIGHT_TOLERANCE) {
          failures.push(
            `${packId}/${clipName} frame ${index} scale pop: ${height}px vs clip median ${clipMedian}px (${(drift * 100).toFixed(1)}%)`,
          )
        }
      })

      const dead = []
      for (let index = 1; index < frames.length; index += 1) {
        const delta = frameDelta(frames[index - 1], frames[index])
        if (delta < MIN_FRAME_DELTA) dead.push(`${index - 1}-${index} (${(delta * 100).toFixed(3)}%)`)
      }
      const transitions = Math.max(1, frames.length - 1)
      if (dead.length / transitions > DEAD_TRANSITION_RATIO) {
        failures.push(
          `${packId}/${clipName} is not animating: ${dead.length}/${transitions} transitions are near-identical [${dead.join(', ')}]`,
        )
      } else if (dead.length > 0) {
        warnings.push(`${packId}/${clipName} holds on repeated frames ${dead.join(', ')}`)
      }
    }
    console.log(`${packId}: ${checked} frames checked, ${skipped} clip(s) skipped (frames not generated yet)`)
  }

  const packsWithIdle = Object.entries(idleHeights)
  if (packsWithIdle.length > 1) {
    const heights = packsWithIdle.map(([, height]) => height)
    const tallest = Math.max(...heights)
    const shortest = Math.min(...heights)
    const spread = (tallest - shortest) / tallest
    const summary = packsWithIdle.map(([pack, height]) => `${pack} ${height.toFixed(0)} logical px`).join(', ')
    if (spread > CROSS_PACK_TOLERANCE) {
      failures.push(`packs do not stand the same height: ${summary} (${(spread * 100).toFixed(1)}% apart)`)
    } else {
      console.log(`cross-pack idle height OK: ${summary}`)
    }
  }

  for (const warning of warnings) console.log(`note: ${warning}`)

  if (failures.length > 0) {
    console.error(`\n${failures.length} animation problem(s):`)
    for (const failure of failures) console.error(`  ${failure}`)
    process.exitCode = 1
    return
  }
  console.log('animation checks OK')
}

await main()
