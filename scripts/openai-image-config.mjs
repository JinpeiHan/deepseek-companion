// Reads the OpenAI-compatible image endpoint from a local .env file, or failing
// that from the local DSH installation.
//
// The API key never leaves this process: it is returned to the caller in memory
// and every error message built here is free of credential material. .env is
// git-ignored; .env.example is the committed template.

import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE_URL_RE = /^\s*baseURL:\s*(\S+)\s*$/mu
const API_KEY_RE = /^\s*OPENAI_API_KEY:\s*["']?([^\r\n"']+)["']?\s*$/mu

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const IMAGE_MODEL = 'gpt-image-2'

export const defaultDshHome = () => process.env.DSH_HOME || resolve(homedir(), '.dsh')

export const defaultEnvFile = () => process.env.DSH_ENV_FILE || resolve(ROOT, '.env')

/**
 * Minimal KEY=VALUE parser. Deliberately not a dotenv dependency: this runs
 * before any install step and must never surprise us about what it evaluates.
 * Supports `export KEY=value`, `#` comments, and single or double quotes.
 */
export const parseEnv = (text) => {
  const values = {}
  for (const rawLine of text.split(/\r?\n/u)) {
    const line = rawLine.trim()
    if (line === '' || line.startsWith('#')) continue
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/u.exec(line)
    if (!match) continue
    let value = match[2].trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    } else {
      value = value.split(' #')[0].trim()
    }
    values[match[1]] = value
  }
  return values
}

const fromEnvValues = (values) => {
  const baseURL = (values.OPENAI_BASE_URL || values.OPENAI_API_BASE || '').trim().replace(/\/$/u, '')
  const apiKey = (values.OPENAI_API_KEY || '').trim()
  return { baseURL, apiKey }
}

/**
 * Resolution order, first complete source wins:
 *   1. .env in the repo root (or DSH_ENV_FILE)
 *   2. OPENAI_BASE_URL + OPENAI_API_KEY in the real environment
 *   3. the DSH installation's settings.yaml + .credentials.yaml
 * A source that supplies only one of the two values is reported as incomplete
 * rather than silently half-merged with another source.
 */
export async function loadOpenAiImageConfig({
  dshHome = process.env.DSH_HOME,
  envFile = defaultEnvFile(),
  env = process.env,
  readText = readFile,
} = {}) {
  const attempts = []

  if (envFile) {
    let text = null
    try {
      text = await readText(envFile, 'utf8')
    } catch {
      attempts.push(`no .env file at ${envFile}`)
    }
    if (text !== null) {
      const { baseURL, apiKey } = fromEnvValues(parseEnv(text))
      if (baseURL && apiKey) return { baseURL, apiKey, model: IMAGE_MODEL, source: '.env' }
      if (!baseURL) attempts.push('.env is missing OPENAI_BASE_URL')
      if (!apiKey) attempts.push('.env is missing OPENAI_API_KEY')
    }
  }

  const fromProcess = fromEnvValues(env ?? {})
  if (fromProcess.baseURL && fromProcess.apiKey) {
    return { ...fromProcess, model: IMAGE_MODEL, source: 'environment' }
  }

  if (dshHome) {
    try {
      const settings = await readText(resolve(dshHome, 'settings.yaml'), 'utf8')
      const credentials = await readText(resolve(dshHome, '.credentials.yaml'), 'utf8')
      const baseURL = BASE_URL_RE.exec(settings)?.[1]?.replace(/\/$/u, '')
      const apiKey = API_KEY_RE.exec(credentials)?.[1]?.trim()
      if (!baseURL) throw new Error('OpenAI image baseURL is not configured in DSH settings')
      if (!apiKey) throw new Error('OPENAI_API_KEY is not configured in DSH credentials')
      return { baseURL, apiKey, model: IMAGE_MODEL, source: 'dsh' }
    } catch (error) {
      // Keep the specific DSH complaint when DSH was explicitly requested.
      if (/is not configured/u.test(error.message)) throw error
      attempts.push(`no usable DSH config at ${dshHome}`)
    }
  }

  throw new Error(
    `OpenAI image credentials are not configured (${attempts.join('; ')}). ` +
      'Copy .env.example to .env and set OPENAI_BASE_URL and OPENAI_API_KEY.',
  )
}
