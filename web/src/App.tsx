import { useEffect, useRef, useState } from 'react'
import { VoiceConnection, type VoiceState } from './voice'

const labels: Record<VoiceState, string> = {
  offline: 'Microphone disconnected',
  connecting: 'Connecting your microphone…',
  ready: 'Ready',
  starting: 'Opening microphone…',
  recording: 'Listening',
  transcribing: 'Finishing the transcript…',
}

export default function App() {
  const connection = useRef<VoiceConnection | null>(null)
  const [state, setState] = useState<VoiceState>('offline')
  const [transcript, setTranscript] = useState('')
  const [final, setFinal] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const cancel = () => {
      if (document.hidden) void connection.current?.cancel()
    }
    const disconnect = () => void connection.current?.disconnect()
    document.addEventListener('visibilitychange', cancel)
    window.addEventListener('pagehide', disconnect)
    return () => {
      document.removeEventListener('visibilitychange', cancel)
      window.removeEventListener('pagehide', disconnect)
      disconnect()
    }
  }, [])

  function connect(): void {
    setError('')
    const client = new VoiceConnection(setState, (text, done) => {
      setTranscript(text)
      setFinal(done)
    }, setError)
    connection.current = client
    void client.connect()
  }

  const canTalk = state === 'ready' || state === 'starting' || state === 'recording'
  const busy = state === 'starting' || state === 'recording' || state === 'transcribing'

  return (
    <main>
      <p className="eyebrow">Microphone test</p>
      <h1>Slate</h1>
      <p>Hold Talk to speak. Release it to finish.</p>
      <section aria-label="Microphone">
        <p className={`status ${state}`} role="status">{labels[state]}</p>
        <div className="controls">
          {state === 'offline' ? (
            <button onClick={connect}>Connect microphone</button>
          ) : (
            <button className="secondary" disabled={state === 'connecting'} onClick={() => void connection.current?.disconnect()}>
              Disconnect
            </button>
          )}
          <button
            className={`talk ${state === 'recording' ? 'active' : ''}`}
            disabled={!canTalk}
            aria-label="Hold to talk"
            aria-pressed={state === 'recording'}
            onPointerDown={(event) => {
              if (event.button !== 0) return
              event.preventDefault()
              event.currentTarget.setPointerCapture(event.pointerId)
              void connection.current?.start()
            }}
            onPointerUp={() => void connection.current?.finish()}
            onPointerCancel={() => void connection.current?.cancel()}
            onKeyDown={(event) => {
              if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
                event.preventDefault()
                void connection.current?.start()
              }
            }}
            onKeyUp={(event) => {
              if (event.key === ' ' || event.key === 'Enter') {
                event.preventDefault()
                void connection.current?.finish()
              }
            }}
            onBlur={() => { if (state === 'recording') void connection.current?.cancel() }}
          >
            {state === 'recording' ? 'Listening…' : 'Talk'}
          </button>
          {busy && <button className="secondary" onClick={() => void connection.current?.cancel()}>Cancel</button>}
        </div>
        <p className="hint">Audio is sent only while you hold Talk. This test shows text and does not speak back.</p>
        {error && <p role="alert" className="error">{error}</p>}
      </section>
      <section aria-label="Transcript" className="transcript">
        <h2>Transcript</h2>
        <p aria-live="polite" aria-atomic="true">
          {transcript || (final ? 'No speech detected. Try again.' : 'Your words will appear here.')}
        </p>
      </section>
    </main>
  )
}
