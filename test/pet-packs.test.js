import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { KEYFRAME_CLIPS } from '../scripts/build-pack-manifests.mjs'

const MANIFESTS = ['../assets/pet-standard-manifest.json', '../assets/pet-slender-manifest.json']

const readJson = async (relative) => JSON.parse(await readFile(new URL(relative, import.meta.url), 'utf8'))

test('registry declares three stable proportion packs', async () => {
  const registry = await readJson('../assets/pet-packs.json')
  assert.equal(registry.defaultPack, 'chibi')
  assert.deepEqual(Object.keys(registry.packs), ['chibi', 'standard', 'slender'])
})

test('new manifests declare the funded keyframe subset symmetrically', async () => {
  const q = await readJson('../assets/pet-manifest.json')
  const counts = []
  for (const file of MANIFESTS) {
    const manifest = await readJson(file)
    assert.deepEqual(Object.keys(manifest.clips), KEYFRAME_CLIPS)
    for (const clip of KEYFRAME_CLIPS) assert.ok(q.clips[clip], `${clip} is not a chibi clip`)
    assert.equal(manifest.logicalHeight, 260)
    assert.equal(manifest.sourceWidth, 512)
    assert.equal(manifest.sourceHeight, 512)
    counts.push(Object.values(manifest.clips).reduce((sum, clip) => sum + clip.frames.length, 0))
  }
  assert.equal(new Set(counts).size, 1, `packs are asymmetric: ${counts.join(' vs ')}`)
  assert.equal(counts[0], 23)
})

test('new manifests keep chibi timing and reference 512 frame files', async () => {
  const q = await readJson('../assets/pet-manifest.json')
  for (const file of MANIFESTS) {
    const manifest = await readJson(file)
    for (const [clip, definition] of Object.entries(manifest.clips)) {
      assert.equal(definition.frames.length, q.clips[clip].frames.length, `${clip} frame count`)
      assert.equal(definition.frameMs, q.clips[clip].frameMs, `${clip} frameMs`)
      assert.equal(definition.loop, q.clips[clip].loop, `${clip} loop`)
      assert.equal(definition.motion ?? null, q.clips[clip].motion ?? null, `${clip} motion`)
      for (const frame of definition.frames) {
        assert.ok(frame.includes('_512'), `${clip} frame ${frame} is not a 512 asset`)
        assert.ok(!frame.includes('_238'), `${clip} frame ${frame} still points at a chibi asset`)
      }
    }
  }
})

test('nothing the helper resolves without a fallback points at an unfunded clip', async () => {
  for (const file of MANIFESTS) {
    const manifest = await readJson(file)
    // animation_model._clip_for indexes these directly, so a dangling target
    // would raise KeyError on the first state change instead of degrading.
    for (const [state, clip] of Object.entries(manifest.stateMap)) {
      assert.ok(manifest.clips[clip], `${file} stateMap.${state} -> ${clip}`)
    }
    for (const [activity, clip] of Object.entries(manifest.workingActivityMap)) {
      assert.ok(manifest.clips[clip], `${file} workingActivityMap.${activity} -> ${clip}`)
    }
    for (const clip of manifest.idleMicroClips) {
      assert.ok(manifest.clips[clip], `${file} idleMicroClips -> ${clip}`)
    }
  }
})

test('generation jobs cover both packs exactly once per frame', async () => {
  const { jobs } = await readJson('../art-references/generation-jobs.json')
  assert.equal(jobs.length, 46)
  assert.equal(jobs.filter((job) => job.pack === 'standard').length, 23)
  assert.equal(jobs.filter((job) => job.pack === 'slender').length, 23)
  assert.equal(new Set(jobs.map((job) => job.output)).size, jobs.length)
  for (const job of jobs) {
    assert.ok(job.references.length >= 4, `${job.output} needs at least four references`)
    assert.ok(job.prompt.length > 40, `${job.output} prompt is too short`)
    assert.equal(job.frameIndex === 0, job.previousFrame === null, `${job.output} previousFrame`)
  }
})
