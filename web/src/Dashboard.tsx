import { useEffect, useState } from 'react'
import type { EventFeed } from './events/feed'
import { mockFeed } from './events/mock'
import { socketFeed } from './events/socket'
import { useSlateEvents } from './events/store'
import type { AgentStepStatus, Approval, Device, DeviceState, Task, TaskState } from './types/events'

const feed: EventFeed = new URLSearchParams(window.location.search).has('mock') ? mockFeed : socketFeed

const stateLabels: Record<DeviceState, string> = {
  idle: 'Idle',
  listen: 'Listening',
  mute: 'Muted',
  transcribe: 'Transcribing',
  respond: 'Responding',
  error: 'Error',
}

const taskLabels: Record<TaskState, string> = {
  queued: 'Queued',
  running: 'Working',
  awaiting_approval: 'Needs your approval',
  succeeded: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timed_out: 'Approval timed out',
}

const stepIcons: Record<AgentStepStatus, string> = {
  running: '…',
  done: '✓',
  failed: '✕',
}

function time(ts: string): string {
  return new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
}

function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  return now
}

function TaskRow({ task, approval, now, onError }: { task: Task; approval?: Approval; now: number; onError: (message: string) => void }) {
  const [busy, setBusy] = useState(false)
  const active = task.state === 'running' || task.state === 'awaiting_approval' || task.state === 'queued'
  const left = approval ? Math.max(0, Math.round((Date.parse(approval.deadline) - now) / 1000)) : 0

  async function act(work: () => Promise<void>): Promise<void> {
    setBusy(true)
    try {
      await work()
    } catch (error) {
      onError((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`task ${task.state}`}>
      <div className="head">
        <strong>{task.title}</strong>
        <span className="pill">{taskLabels[task.state]}</span>
      </div>
      <small>
        {time(task.created_at)} · {task.kind}
        {task.reason && !active ? ` · ${task.reason}` : ''}
      </small>
      {approval && (
        <div className="approval">
          <p>{approval.summary}</p>
          <p className="hint">Answer within {left} s, or Slate will not do it.</p>
          <button disabled={busy} onClick={() => void act(() => feed.decide(approval, true, task.version))}>Approve</button>
          <button disabled={busy} className="secondary" onClick={() => void act(() => feed.decide(approval, false, task.version))}>Deny</button>
        </div>
      )}
      {active && !approval && (
        <button disabled={busy} className="secondary" onClick={() => void act(() => feed.cancel(task))}>Cancel</button>
      )}
    </li>
  )
}

function Devices({ onError }: { onError: (message: string) => void }) {
  const [devices, setDevices] = useState<Device[]>()

  useEffect(() => {
    feed.devices().then(setDevices, (error: Error) => onError(error.message))
  }, [onError])

  async function revoke(device: Device): Promise<void> {
    try {
      await feed.revoke(device)
      setDevices(await feed.devices())
    } catch (error) {
      onError((error as Error).message)
    }
  }

  if (!devices) return <p className="hint">Loading devices…</p>
  if (devices.length === 0) return <p className="hint">No paired devices. Run make pair next to a Slate in setup mode.</p>
  return (
    <ul className="devices">
      {devices.map((device) => (
        <li key={device.id}>
          <span>
            <strong>{device.name}</strong>
            <small>{device.revoked_at ? `Revoked ${time(device.revoked_at)}` : device.last_seen_at ? `Last seen ${time(device.last_seen_at)}` : 'Never connected'}</small>
          </span>
          {!device.revoked_at && <button className="secondary" onClick={() => void revoke(device)}>Revoke</button>}
        </li>
      ))}
    </ul>
  )
}

export default function Dashboard() {
  const { device, lastSeen, sessions, tasks, approvals, error } = useSlateEvents(feed)
  const [actionError, setActionError] = useState<string | null>(null)
  const now = useNow()
  const shown = error ?? actionError

  return (
    <>
      {shown && <p role="alert" className="error">{shown}</p>}

      <section aria-label="Device">
        <h2>Device</h2>
        {device ? (
          <dl className="stats">
            <div><dt>Connection</dt><dd>{device.online ? 'Online' : 'Offline'}</dd></div>
            <div><dt>State</dt><dd className={`state ${device.state}`}>{stateLabels[device.state]}</dd></div>
          </dl>
        ) : (
          <p className="hint">Waiting for the device…</p>
        )}
        {lastSeen && <p className="hint">Last update {time(lastSeen)}</p>}
      </section>

      <section aria-label="Tasks">
        <h2>Tasks</h2>
        {tasks.length === 0 && <p className="hint">No tasks yet. Ask Slate to do something.</p>}
        <ul className="tasks">
          {tasks.slice(0, 8).map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              approval={approvals.find((a) => a.task_id === task.id)}
              now={now}
              onError={setActionError}
            />
          ))}
        </ul>
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

      <section aria-label="Paired devices">
        <h2>Paired devices</h2>
        <Devices onError={setActionError} />
      </section>
    </>
  )
}
