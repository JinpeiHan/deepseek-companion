// Reads the OpenAI-compatible image endpoint out of the local DSH installation.
// The API key never leaves this process: it is returned to the caller in memory
// and every error message built here is free of credential material.

import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const BASE_URL_RE = /^\s*baseURL:\s*(\S+)\s*$/mu
const API_KEY_RE = /^\s*OPENAI_API_KEY:\s*["']?([^\r\n"']+)["']?\s*$/mu

export const IMAGE_MODEL = 'gpt-image-2'

export const defaultDshHome = () => process.env.DSH_HOME || resolve(homedir(), '.dsh')

export async function loadOpenAiImageConfig({ dshHome = process.env.DSH_HOME, readText = readFile } = {}) {
  if (!dshHome) throw new Error('DSH_HOME is required for OpenAI image generation')
  const settings = await readText(resolve(dshHome, 'settings.yaml'), 'utf8')
  const credentials = await readText(resolve(dshHome, '.credentials.yaml'), 'utf8')
  const baseURL = BASE_URL_RE.exec(settings)?.[1]?.replace(/\/$/u, '')
  const apiKey = API_KEY_RE.exec(credentials)?.[1]?.trim()
  if (!baseURL) throw new Error('OpenAI image baseURL is not configured in DSH settings')
  if (!apiKey) throw new Error('OPENAI_API_KEY is not configured in DSH credentials')
  return { baseURL, apiKey, model: IMAGE_MODEL }
}
