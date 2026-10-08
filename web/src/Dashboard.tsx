import { mockFeed } from './events/mock'
import { useSlateEvents } from './events/store'
import type { AgentStepStatus, DeviceState } from './types/events'

const stateLabels: Record<DeviceState, string> = {
  idle: 'Idle',
  listen: 'Listening',
  mute: 'Muted',
  transcribe: 'Transcribing',
  respond: 'Responding',
  error: 'Error',
}

const stepIcons: Record<AgentStepStatus, string> = {
  running: '…',
  done: '✓',
  failed: '✕',
}

function time(ts: string): string {
  return new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
}

export default function Dashboard() {
  const { device, lastSeen, sessions } = useSlateEvents(mockFeed)

  return (
    <>
      <section aria-label="Device">
        <h2>Device</h2>
        {device ? (
          <dl className="stats">
            <div><dt>Connection</dt><dd>{device.online ? 'Online' : 'Offline'}</dd></div>
            <div><dt>State</dt><dd className={`state ${device.state}`}>{stateLabels[device.state]}</dd></div>
            <div><dt>Battery</dt><dd>{device.battery_percent === null ? '–' : `${device.battery_percent}%`}</dd></div>
            <div><dt>Wi-Fi</dt><dd>{device.wifi_rssi === null ? '–' : `${device.wifi_rssi} dBm`}</dd></div>
            <div><dt>Firmware</dt><dd>{device.firmware_version ?? 'Unknown'}</dd></div>
          </dl>
        ) : (
          <p className="hint">Waiting for the device…</p>
        )}
        {lastSeen && <p className="hint">Last update {time(lastSeen)}</p>}
      </section>

      <section aria-label="Agent activity">
        <h2>Agent activity</h2>
        {sessions.length === 0 && <p className="hint">No requests yet.</p>}
        {sessions.slice(0, 3).map((session) => (
          <div key={session.id} className="session">
            <p className="eyebrow">{time(session.started)} · {session.id}</p>
            <ol className="steps">
              {session.steps.map((s) => (
                <li key={s.step_id} className={s.status}>
                  <span className="icon" aria-hidden="true">{stepIcons[s.status]}</span>
                  <span className="body">
                    <strong>{s.title}</strong>
                    {s.detail && <small>{s.detail}</small>}
                  </span>
                  <span className="kind">{s.kind}</span>
                </li>
              ))}
            </ol>
          </div>
        ))}
      </section>
    </>
  )
}