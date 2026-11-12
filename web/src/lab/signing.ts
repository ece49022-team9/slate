// Same signing as server/slate/access/signing.py, done in the browser with
// WebCrypto so the lab can show every step. A real device does this in C.

const encoder = new TextEncoder()

export const SCHEME = 'SLATE-HMAC-SHA256-V1'

export type Lab = { id: string; secret: string }

export type Draft = {
  method: 'GET' | 'POST'
  path: string
  body: string
  timestamp: number
  nonce: string
  deviceId: string
}

function hex(bytes: ArrayBuffer | Uint8Array): string {
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('')
}

function fromHex(text: string): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(text.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16)
  return out
}

export function randomHex(bytes: number): string {
  return hex(crypto.getRandomValues(new Uint8Array(bytes)))
}

export async function sha256(text: string): Promise<string> {
  return hex(await crypto.subtle.digest('SHA-256', encoder.encode(text)))
}

export async function canonicalLines(d: Draft): Promise<Array<[string, string]>> {
  return [
    ['scheme', SCHEME],
    ['method', d.method],
    ['path', d.path],
    ['time', String(d.timestamp)],
    ['nonce', d.nonce],
    ['device', d.deviceId],
    ['sha256(body)', await sha256(d.body)],
  ]
}

export async function sign(secretHex: string, d: Draft): Promise<string> {
  const text = (await canonicalLines(d)).map(([, value]) => value).join('\n')
  const key = await crypto.subtle.importKey(
    'raw',
    fromHex(secretHex),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  return hex(await crypto.subtle.sign('HMAC', key, encoder.encode(text)))
}
