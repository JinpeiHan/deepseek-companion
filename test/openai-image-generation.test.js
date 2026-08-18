import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { loadOpenAiImageConfig } from '../scripts/openai-image-config.mjs'
import { createOpenAiImageClient } from '../scripts/openai-image-client.mjs'
import { selectJobs, buildEditRequest, readSampleApproval, SAMPLE_KEYS } from '../scripts/generate-openai-assets.mjs'

const SECRET = 'secret-for-test'

const makeDshHome = async ({ settings, credentials } = {}) => {
  const home = await mkdtemp(join(tmpdir(), 'dsh-openai-'))
  const dshHome = join(home, '.dsh')
  await mkdir(dshHome, { recursive: true })
  await writeFile(
    join(dshHome, 'settings.yaml'),
    settings ??
      ['llm-pi-ai:', '  providers:', '    openai:', `      baseURL: https://example.invalid/v1`, ''].join('\n'),
    'utf8',
  )
  await writeFile(join(dshHome, '.credentials.yaml'), credentials ?? `OPENAI_API_KEY: ${SECRET}\n`, 'utf8')
  return dshHome
}

test('config reads base url and key from the DSH home', async () => {
  const dshHome = await makeDshHome()
  const config = await loadOpenAiImageConfig({ dshHome })
  assert.deepEqual(config, {
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    model: 'gpt-image-2',
  })
})

test('config fails clearly when base url or key is missing', async () => {
  const noBase = await makeDshHome({ settings: 'llm-pi-ai: {}\n' })
  await assert.rejects(loadOpenAiImageConfig({ dshHome: noBase }), /baseURL is not configured/u)
  const noKey = await makeDshHome({ credentials: 'DEEPSEEK_API_KEY: other\n' })
  await assert.rejects(loadOpenAiImageConfig({ dshHome: noKey }), /OPENAI_API_KEY is not configured/u)
  await assert.rejects(loadOpenAiImageConfig({ dshHome: '' }), /DSH_HOME is required/u)
})

test('client lists models and never leaks the key in errors', async () => {
  const client = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    fetchImpl: async () => new Response(JSON.stringify({ data: [{ id: 'gpt-image-2' }] }), { status: 200 }),
  })
  assert.deepEqual(await client.listModels(), ['gpt-image-2'])

  const failing = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    fetchImpl: async () => new Response(`bad key ${SECRET}`, { status: 401 }),
    sleep: async () => {},
  })
  await assert.rejects(failing.listModels(), (error) => {
    assert.equal(error.status, 401)
    assert.ok(!String(error).includes(SECRET), 'error text leaked the api key')
    assert.ok(!JSON.stringify(error.body ?? '').includes(SECRET), 'error body leaked the api key')
    return true
  })
})

test('client posts a multipart image edit request', async () => {
  let seenUrl
  let seenInit
  const client = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    fetchImpl: async (url, init) => {
      seenUrl = url
      seenInit = init
      return new Response(JSON.stringify({ data: [{ b64_json: Buffer.from('png').toString('base64') }] }), {
        status: 200,
      })
    },
  })

  const result = await client.editImage({
    prompt: 'draw',
    images: [
      { name: 'a.png', buffer: Buffer.from('a') },
      { name: 'b.jpg', buffer: Buffer.from('b') },
      { name: 'c.png', buffer: Buffer.from('c') },
    ],
  })

  assert.equal(seenUrl, 'https://example.invalid/v1/images/edits')
  assert.equal(seenInit.method, 'POST')
  assert.equal(seenInit.headers.Authorization, `Bearer ${SECRET}`)
  assert.ok(seenInit.body instanceof FormData)
  assert.equal(seenInit.body.get('model'), 'gpt-image-2')
  assert.equal(seenInit.body.get('size'), '1024x1024')
  assert.equal(seenInit.body.get('quality'), 'high')
  assert.equal(seenInit.body.get('background'), 'opaque')
  assert.equal(seenInit.body.get('output_format'), 'png')
  assert.equal(seenInit.body.getAll('image[]').length, 3)
  assert.deepEqual(result, Buffer.from('png'))
})

test('client decodes url responses with a second fetch', async () => {
  const calls = []
  const client = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    fetchImpl: async (url) => {
      calls.push(String(url))
      if (calls.length === 1) {
        return new Response(JSON.stringify({ data: [{ url: 'https://cdn.invalid/out.png' }] }), { status: 200 })
      }
      return new Response(Buffer.from('remote-png'), { status: 200 })
    },
  })
  const result = await client.editImage({ prompt: 'draw', images: [{ name: 'a.png', buffer: Buffer.from('a') }] })
  assert.equal(calls.length, 2)
  assert.equal(calls[1], 'https://cdn.invalid/out.png')
  assert.deepEqual(result, Buffer.from('remote-png'))
})

test('client retries 429 and 5xx but never 400-class failures', async () => {
  let attempts = 0
  const retrying = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    sleep: async () => {},
    fetchImpl: async () => {
      attempts += 1
      if (attempts < 3) return new Response('slow down', { status: 429 })
      return new Response(JSON.stringify({ data: [{ b64_json: Buffer.from('ok').toString('base64') }] }), {
        status: 200,
      })
    },
  })
  assert.deepEqual(
    await retrying.editImage({ prompt: 'p', images: [{ name: 'a.png', buffer: Buffer.from('a') }] }),
    Buffer.from('ok'),
  )
  assert.equal(attempts, 3)

  let badRequests = 0
  const fatal = createOpenAiImageClient({
    baseURL: 'https://example.invalid/v1',
    apiKey: SECRET,
    sleep: async () => {},
    fetchImpl: async () => {
      badRequests += 1
      return new Response('bad request', { status: 400 })
    },
  })
  await assert.rejects(fatal.editImage({ prompt: 'p', images: [{ name: 'a.png', buffer: Buffer.from('a') }] }))
  assert.equal(badRequests, 1)
})

test('sample selection is pinned to the four approval frames', async () => {
  const { jobs } = JSON.parse(
    await (await import('node:fs/promises')).readFile(
      new URL('../art-references/generation-jobs.json', import.meta.url),
      'utf8',
    ),
  )
  const selected = selectJobs(jobs, { samples: true })
  assert.deepEqual(
    selected.map((job) => [job.pack, job.clip, job.frameIndex]),
    [
      ['standard', 'idle', 0],
      ['standard', 'head_pat', 0],
      ['slender', 'idle', 0],
      ['slender', 'head_pat', 0],
    ],
  )
  assert.deepEqual(SAMPLE_KEYS, ['standard/idle/0', 'standard/head_pat/0', 'slender/idle/0', 'slender/head_pat/0'])
})

test('job selection filters by pack, clip and limit', async () => {
  const jobs = [
    { pack: 'standard', clip: 'idle', frameIndex: 0 },
    { pack: 'standard', clip: 'blink', frameIndex: 0 },
    { pack: 'slender', clip: 'idle', frameIndex: 0 },
  ]
  assert.equal(selectJobs(jobs, { packs: ['standard'] }).length, 2)
  assert.equal(selectJobs(jobs, { clips: ['idle'] }).length, 2)
  assert.equal(selectJobs(jobs, { limit: 1 }).length, 1)
})

test('batch generation refuses to start until all four samples are approved', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-approval-'))
  const file = join(dir, 'openai-sample-approval.json')
  await assert.rejects(readSampleApproval(file), /four OpenAI samples must be approved/u)

  await writeFile(file, JSON.stringify({ model: 'gpt-image-2', samples: { 'standard/idle/0': 'approved' } }), 'utf8')
  await assert.rejects(readSampleApproval(file), /four OpenAI samples must be approved/u)

  const rejected = Object.fromEntries(SAMPLE_KEYS.map((key) => [key, 'approved']))
  rejected['slender/head_pat/0'] = 'rejected'
  await writeFile(file, JSON.stringify({ model: 'gpt-image-2', samples: rejected }), 'utf8')
  await assert.rejects(readSampleApproval(file), /four OpenAI samples must be approved/u)

  const wrongModel = Object.fromEntries(SAMPLE_KEYS.map((key) => [key, 'approved']))
  await writeFile(file, JSON.stringify({ model: 'gpt-image-1', samples: wrongModel }), 'utf8')
  await assert.rejects(readSampleApproval(file), /four OpenAI samples must be approved/u)

  await writeFile(file, JSON.stringify({ model: 'gpt-image-2', samples: wrongModel }), 'utf8')
  assert.deepEqual(await readSampleApproval(file), wrongModel)
})

test('edit requests carry every reference plus the previous frame', async () => {
  const root = resolve(new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/u, '$1'))
  const { jobs } = JSON.parse(
    await (await import('node:fs/promises')).readFile(
      new URL('../art-references/generation-jobs.json', import.meta.url),
      'utf8',
    ),
  )
  const first = jobs.find((job) => job.pack === 'standard' && job.clip === 'blink' && job.frameIndex === 0)
  const request = await buildEditRequest(first, root)
  assert.equal(request.images.length, 5)
  assert.ok(request.prompt.includes('纯白色 #FFFFFF 单色平涂'))
  assert.ok(request.images.every((image) => image.buffer.length > 0))

  const missing = { ...first, references: [...first.references, 'art-references/does-not-exist.jpg'] }
  await assert.rejects(buildEditRequest(missing, root), /missing reference/u)
})
