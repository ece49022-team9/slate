import { useState } from 'react'

type Connection = 'unchecked' | 'checking' | 'connected' | 'unavailable'

const messages: Record<Connection, string> = {
  unchecked: 'Check the connection to your local server.',
  checking: 'Connecting…',
  connected: 'Connected to the Slate server.',
  unavailable: 'Server unavailable. Run make server, then try again.',
}

export default function App() {
  const [connection, setConnection] = useState<Connection>('unchecked')

  async function checkConnection(): Promise<void> {
    setConnection('checking')
    try {
      const response = await fetch('/api/health', {
        signal: AbortSignal.timeout(5000),
        cache: 'no-store',
      })
      if (response.status !== 204) {
        throw new Error(`Health check returned HTTP ${response.status}`)
      }
      setConnection('connected')
    } catch (error) {
      console.error('[slate.connection] Backend health check failed', error)
      setConnection('unavailable')
    }
  }

  return (
    <main>
      <p className="eyebrow">Development workspace</p>
      <h1>Slate</h1>
      <p>The starting point for setup, devices, and task approvals.</p>
      <section aria-label="Server connection">
        <p role="status">{messages[connection]}</p>
        <button onClick={checkConnection} disabled={connection === 'checking'}>
          Check connection
        </button>
      </section>
      <a href="/api/docs">API documentation</a>
    </main>
  )
}
