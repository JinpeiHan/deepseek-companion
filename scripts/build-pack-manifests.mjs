#!/usr/bin/env node
// Derives the standard and slender manifests from the chibi action matrix.
//
// The standard and slender packs mirror the full chibi action matrix, so all
// three proportions support the same actions and any pack can be swapped in
// without the runtime losing a behaviour.
//
// Clips are still filtered through PACK_CLIPS rather than copied blindly: a
// clip left out is dropped from the manifest entirely rather than declared with
// missing files, because the Python loader reads every declared frame eagerly
// while `play_overlay()` degrades to a no-op for an absent clip. stateMap and
// workingActivityMap are remapped so they can never point at a dropped clip.

import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const PACK_CLIPS = [
  // DSH state skeleton: every state in stateMap must resolve to one of these.
  'idle',
  'thinking',
  'working',
  'waiting',
  'success',
  'error',
  'error_dizzy',
  'dragging',
  // Idle micro-motion, so IDLE is not a frozen icon.
  'blink',
  'glance',
  // Interactions.
  'head_pat',
  'poke',
  'tail',
  // Locomotion: the walk cycles plus their start/stop transitions.
  'working_search',
  'working_command',
  'walk_start_left',
  'walk_stop_left',
  'walk_start_right',
  'walk_stop_right',
]

// Kept as an alias so existing importers keep working.
export const KEYFRAME_CLIPS = PACK_CLIPS

const PACKS = [
  ['assets/pet-standard-manifest.json', 'whale-girl-standard'],
  ['assets/pet-slender-manifest.json', 'whale-girl-slender'],
]

const renameFrame = (frame) => {
  if (!frame.includes('_238')) throw new Error(`unexpected chibi frame name: ${frame}`)
  return frame.replaceAll('_238.png', '_512.png').replaceAll('_238_', '_512_')
}

const build = (chibi, characterId) => {
  const clips = {}
  for (const name of PACK_CLIPS) {
    const clip = chibi.clips[name]
    if (!clip) throw new Error(`chibi manifest has no clip named ${name}`)
    clips[name] = {
      frames: clip.frames.map(renameFrame),
      frameMs: clip.frameMs,
      loop: clip.loop,
      ...(clip.motion ? { motion: clip.motion } : {}),
    }
  }

  const stateMap = Object.fromEntries(
    Object.entries(chibi.stateMap).map(([state, clip]) => [state, clips[clip] ? clip : chibi.stateMap.IDLE]),
  )
  const workingActivityMap = Object.fromEntries(
    Object.entries(chibi.workingActivityMap).map(([activity, clip]) => [
      activity,
      clips[clip] ? clip : stateMap.WORKING,
    ]),
  )
  const idleMicroClips = chibi.idleMicroClips.filter((clip) => Boolean(clips[clip]))

  for (const [state, clip] of Object.entries(stateMap)) {
    if (!clips[clip]) throw new Error(`stateMap.${state} points at missing clip ${clip}`)
  }
  for (const [activity, clip] of Object.entries(workingActivityMap)) {
    if (!clips[clip]) throw new Error(`workingActivityMap.${activity} points at missing clip ${clip}`)
  }

  return {
    formatVersion: 2,
    characterId,
    sourceWidth: 512,
    sourceHeight: 512,
    maxFrameWidth: 512,
    maxFrameHeight: 512,
    logicalWidth: 260,
    logicalHeight: 260,
    footAnchor: [0.5, 0.97],
    bubbleAnchor: [0.5, 0.04],
    stateMap,
    workingActivityMap,
    idleMicroClips,
    clips,
  }
}

const main = async () => {
  const chibi = JSON.parse(await readFile(resolve(ROOT, 'assets/pet-manifest.json'), 'utf8'))
  for (const [file, characterId] of PACKS) {
    const manifest = build(chibi, characterId)
    const frames = Object.values(manifest.clips).reduce((sum, clip) => sum + clip.frames.length, 0)
    await writeFile(resolve(ROOT, file), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
    console.log(`${file}: ${Object.keys(manifest.clips).length} clips, ${frames} frames`)
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main()
}
