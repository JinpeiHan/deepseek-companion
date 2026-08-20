// Minimal PNG reader shared by the asset validators.
//
// The repo validates generated art without a native image dependency, so this
// module carries just enough of the format to answer the questions the
// validators ask: header geometry and a fully expanded RGBA buffer. It stays
// policy-free on purpose -- `readHeader` reports what the file says instead of
// asserting a pack-specific size, so a rig validator (whose parts are tightly
// cropped and never 512x512) can reuse the same decoder.

import { readdir } from 'node:fs/promises'
import { inflateSync } from 'node:zlib'
import { relative, resolve, sep } from 'node:path'

export const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

/** Parse the IHDR chunk. Throws only for files that are not PNGs at all. */
export const readHeader = (buffer, label) => {
  if (!buffer.subarray(0, 8).equals(SIGNATURE)) throw new Error(`${label}: not a PNG file`)
  if (buffer.readUInt32BE(8) !== 13 || buffer.toString('latin1', 12, 16) !== 'IHDR') {
    throw new Error(`${label}: malformed IHDR chunk`)
  }
  return {
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
    bitDepth: buffer.readUInt8(24),
    colorType: buffer.readUInt8(25),
    interlace: buffer.readUInt8(28),
  }
}

/** The subset this decoder implements: 8-bit non-interlaced RGBA. */
export const assertDecodable = ({ bitDepth, colorType, interlace }, label) => {
  if (colorType !== 6) throw new Error(`${label}: expected RGBA color type 6, got ${colorType}`)
  if (bitDepth !== 8) throw new Error(`${label}: expected bit depth 8, got ${bitDepth}`)
  if (interlace !== 0) throw new Error(`${label}: interlaced PNG is not supported`)
}

export const paeth = (a, b, c) => {
  const p = a + b - c
  const pa = Math.abs(p - a)
  const pb = Math.abs(p - b)
  const pc = Math.abs(p - c)
  if (pa <= pb && pa <= pc) return a
  return pb <= pc ? b : c
}

export const decodeRgba = (buffer, label) => {
  const header = readHeader(buffer, label)
  assertDecodable(header, label)
  const { width, height } = header
  const chunks = []
  let offset = 8
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset)
    const type = buffer.toString('latin1', offset + 4, offset + 8)
    if (type === 'IDAT') chunks.push(buffer.subarray(offset + 8, offset + 8 + length))
    if (type === 'IEND') break
    offset += length + 12
  }
  if (chunks.length === 0) throw new Error(`${label}: no IDAT data`)
  const raw = inflateSync(Buffer.concat(chunks))
  const bpp = 4
  const stride = width * bpp
  const pixels = Buffer.alloc(height * stride)
  for (let y = 0; y < height; y += 1) {
    const filter = raw[y * (stride + 1)]
    const line = raw.subarray(y * (stride + 1) + 1, y * (stride + 1) + 1 + stride)
    const out = pixels.subarray(y * stride, (y + 1) * stride)
    const prior = y === 0 ? null : pixels.subarray((y - 1) * stride, y * stride)
    for (let x = 0; x < stride; x += 1) {
      const left = x >= bpp ? out[x - bpp] : 0
      const up = prior ? prior[x] : 0
      const upLeft = prior && x >= bpp ? prior[x - bpp] : 0
      const value = line[x]
      if (filter === 0) out[x] = value
      else if (filter === 1) out[x] = (value + left) & 0xff
      else if (filter === 2) out[x] = (value + up) & 0xff
      else if (filter === 3) out[x] = (value + ((left + up) >> 1)) & 0xff
      else if (filter === 4) out[x] = (value + paeth(left, up, upLeft)) & 0xff
      else throw new Error(`${label}: unknown PNG filter ${filter}`)
    }
  }
  return { width, height, pixels }
}

/** Reject any path that resolves outside `rootDir`. */
export const confine = (rootDir, filePath, label) => {
  const rel = relative(rootDir, filePath)
  if (rel.startsWith('..') || rel.startsWith(sep) || rel === '') throw new Error(`${label}: path escapes pack root`)
}

/** Every PNG below `dir`, as sorted POSIX paths relative to `base`. */
export const listPngs = async (dir, base = dir) => {
  const entries = await readdir(dir, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const full = resolve(dir, entry.name)
    if (entry.isDirectory()) files.push(...(await listPngs(full, base)))
    else if (entry.name.toLowerCase().endsWith('.png')) files.push(relative(base, full).split(sep).join('/'))
  }
  return files.sort()
}
