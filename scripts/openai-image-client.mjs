// Minimal OpenAI-compatible Images API client for asset generation.
//
// Only two endpoints are used: `/models` for a free capability probe and
// `/images/edits` for reference-driven frame generation. Authentication and
// permission failures are terminal; only timeouts, 429 and 5xx are retried.

import { IMAGE_MODEL } from './openai-image-config.mjs'

const RETRY_DELAYS = [1000, 2500, 5000]

const detectMime = (buffer, name) => {
  if (buffer.length >= 8 && buffer[0] === 0x89 && buffer[1] === 0x50) return 'image/png'
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8) return 'image/jpeg'
  if (name.toLowerCase().endsWith('.png')) return 'image/png'
  return 'image/jpeg'
}

const isRetryableStatus = (status) => status === 429 || status >= 500

const isRetryableError = (error) => error?.name === 'AbortError' || error instanceof TypeError

class OpenAiImageError extends Error {
  constructor(message, { status = null, body = null } = {}) {
    super(message)
    this.name = 'OpenAiImageError'
    this.status = status
    this.body = body
  }
}

export function createOpenAiImageClient({
  baseURL,
  apiKey,
  model = IMAGE_MODEL,
  fetchImpl = fetch,
  sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
} = {}) {
  if (!baseURL) throw new Error('baseURL is required')
  if (!apiKey) throw new Error('apiKey is required')

  const redact = (value) => String(value ?? '').replaceAll(apiKey, '[REDACTED]')

  const fail = async (response, endpoint) => {
    const raw = await response.text().catch(() => '')
    const body = redact(raw).slice(0, 400)
    let detail = body
    try {
      detail = redact(JSON.parse(raw)?.error?.message ?? body).slice(0, 400)
    } catch {
      // Non-JSON error bodies are already redacted above.
    }
    return new OpenAiImageError(`${endpoint} failed with HTTP ${response.status}: ${detail}`, {
      status: response.status,
      body,
    })
  }

  // `makeInit` is a factory so every retry sends a freshly built multipart body
  // instead of re-reading one that the previous attempt already consumed.
  const request = async (endpoint, makeInit) => {
    let lastError
    for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt += 1) {
      let response
      try {
        response = await fetchImpl(`${baseURL}${endpoint}`, makeInit())
      } catch (error) {
        if (!isRetryableError(error) || attempt === RETRY_DELAYS.length) {
          throw new OpenAiImageError(`${endpoint} request failed: ${redact(error?.message)}`)
        }
        lastError = error
        await sleep(RETRY_DELAYS[attempt])
        continue
      }
      if (response.ok) return response
      const error = await fail(response, endpoint)
      if (!isRetryableStatus(response.status) || attempt === RETRY_DELAYS.length) throw error
      lastError = error
      await sleep(RETRY_DELAYS[attempt])
    }
    throw lastError
  }

  const listModels = async () => {
    const response = await request('/models', () => ({ headers: { Authorization: `Bearer ${apiKey}` } }))
    const payload = await response.json()
    return (payload?.data ?? []).map((entry) => entry.id)
  }

  // `background` defaults to `opaque`: this endpoint rejects `transparent` for
  // gpt-image-2 with HTTP 400, so the alpha channel is produced downstream by
  // scripts/remove-image-background.py instead. Pass `null` to omit the field.
  const editImage = async ({ prompt, images, size = '1024x1024', quality = 'high', background = 'opaque' }) => {
    if (!prompt) throw new Error('prompt is required')
    if (!Array.isArray(images) || images.length === 0) throw new Error('at least one reference image is required')

    const buildBody = () => {
      const form = new FormData()
      form.set('model', model)
      form.set('prompt', prompt)
      form.set('size', size)
      form.set('quality', quality)
      if (background) form.set('background', background)
      form.set('output_format', 'png')
      for (const image of images) {
        form.append('image[]', new Blob([image.buffer], { type: detectMime(image.buffer, image.name) }), image.name)
      }
      return form
    }

    const response = await request('/images/edits', () => ({
      method: 'POST',
      headers: { Authorization: `Bearer ${apiKey}` },
      body: buildBody(),
    }))
    const payload = await response.json()
    const entry = payload?.data?.[0]
    if (entry?.b64_json) return Buffer.from(entry.b64_json, 'base64')
    if (entry?.url) {
      const download = await fetchImpl(entry.url)
      if (!download.ok) {
        throw new OpenAiImageError(`image download failed with HTTP ${download.status}`, { status: download.status })
      }
      return Buffer.from(await download.arrayBuffer())
    }
    throw new OpenAiImageError('image response contained no b64_json or url payload')
  }

  return { listModels, editImage, model }
}
