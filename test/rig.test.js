import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const run = promisify(execFile)
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PACKS = ['standard', 'chibi']

const manifestOf = async (pack) =>
  JSON.parse(await readFile(join(repositoryRoot, 'assets', `pet-${pack}-rig.json`), 'utf8'))

// The full validator is the real gate: it decodes every layer and recomposites
// the rest pose against the master. Shelling out to it keeps one implementation
// of those rules instead of a second, drifting copy inside the test suite.
test('validate-rig passes for every rig pack', async () => {
  const { stdout } = await run(process.execPath, [join(repositoryRoot, 'scripts', 'validate-rig.mjs')], {
    cwd: repositoryRoot,
  })
  for (const pack of PACKS) {
    assert.match(stdout, new RegExp(`pet-${pack}-rig: 20 parts`), `${pack} was not validated`)
  }
  assert.match(stdout, /OK: 2 rig pack\(s\) validated\./)
})

test('rig manifests declare the shape the runtime loader requires', async () => {
  for (const pack of PACKS) {
    const manifest = await manifestOf(pack)
    assert.equal(manifest.formatVersion, 3, `${pack} formatVersion`)
    assert.equal(manifest.renderer, 'rig', `${pack} renderer`)
    assert.equal(manifest.logicalHeight, 260, `${pack} logicalHeight`)
    assert.equal(manifest.maxFrameWidth, 512, `${pack} maxFrameWidth`)
    assert.deepEqual(manifest.footAnchor, [0.5, 0.97], `${pack} footAnchor`)
    assert.deepEqual(manifest.bubbleAnchor, [0.5, 0.04], `${pack} bubbleAnchor`)
    assert.ok(manifest.parts.length <= 20, `${pack} exceeds the 20-part cap`)
    assert.ok(manifest.stateMap.IDLE, `${pack} has no IDLE state`)
    for (const clip of Object.values(manifest.stateMap)) {
      assert.ok(manifest.clips[clip], `${pack} stateMap names unknown clip ${clip}`)
    }
    for (const [name, chain] of Object.entries(manifest.chains)) {
      assert.equal(chain.segmentParams.length, chain.bones.length, `${pack} chain ${name} segmentParams`)
    }
  }
})

test('every proportion exposes the same rig vocabulary', async () => {
  const [first, ...rest] = await Promise.all(PACKS.map(manifestOf))
  const vocabulary = (manifest) => ({
    parts: manifest.parts.map((part) => part.id).sort(),
    bones: manifest.bones.map((bone) => bone.id).sort(),
    chains: Object.keys(manifest.chains).sort(),
    params: Object.keys(manifest.params).sort(),
    clips: Object.keys(manifest.clips).sort(),
    hitGroups: Object.keys(manifest.hitGroups).sort(),
    interactions: Object.keys(manifest.interactions).sort(),
  })
  for (const other of rest) {
    assert.deepEqual(vocabulary(other), vocabulary(first))
  }
})

// Paint order is allowed to differ between proportions -- the chibi's tail lies
// in front of its hair and the standard's does not -- but it must stay a strict
// order, or two layers would swap unpredictably between frames.
test('part z values are strictly ordered within each pack', async () => {
  for (const pack of PACKS) {
    const manifest = await manifestOf(pack)
    const zs = manifest.parts.map((part) => part.z)
    assert.equal(new Set(zs).size, zs.length, `${pack} has duplicate z values`)
  }
})
