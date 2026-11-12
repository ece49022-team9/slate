export type Connection = { base: URL; token: string }

export function connection(): Connection {
  const base = import.meta.env?.VITE_SLATE_URL
  const token = import.meta.env?.VITE_SLATE_DEVICE_TOKEN
  if (!base || !token) throw new Error('Set VITE_SLATE_URL and VITE_SLATE_DEVICE_TOKEN to connect to Slate.')
  const url = new URL(base)
  if (url.protocol !== 'wss:' && url.protocol !== 'ws:') throw new Error('VITE_SLATE_URL must use wss:// or ws://.')
  return { base: url, token }
}

export function socketUrl(path: string): URL {
  return new URL(path, connection().base)
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const { base, token } = connection()
  const url = new URL(path, base)
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:'
  const reply = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!reply.ok) {
    const detail = await reply.json().then((data: { detail?: unknown }) => data.detail, () => undefined)
    if (typeof detail === 'string') throw new Error(detail)
    if (detail && typeof detail === 'object' && 'error' in detail) throw new Error(String(detail.error).replaceAll('_', ' '))
    throw new Error(`Slate answered ${reply.status}`)
  }
  return (reply.status === 204 ? undefined : await reply.json()) as T
}
