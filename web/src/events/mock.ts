import type { AgentStep, Approval, Device, DeviceStatus, SlateEvent, Task, TaskState } from '../types/events'
import type { EventFeed } from './feed'

let counter = 0
let listener: ((event: SlateEvent) => void) | undefined
const tasks = new Map<string, Task>()
const approvals = new Map<string, Approval>()
const devices: Device[] = [
  { id: 'dev_kitchen', name: 'Slate-4F2A', created_at: '2026-11-10T15:00:00.000Z', revoked_at: null, last_seen_at: new Date().toISOString() },
]

function envelope<T extends SlateEvent['type']>(type: T, data: unknown, session: string | null): SlateEvent {
  counter += 1
  return {
    v: 1,
    id: `evt_mock_${String(counter).padStart(10, '0')}`,
    type,
    ts: new Date().toISOString(),
    device_id: 'dev_kitchen',
    session_id: session,
    data,
  } as SlateEvent
}

function emit(event: SlateEvent): void {
  listener?.(event)
}

function device(data: DeviceStatus): void {
  emit(envelope('device.status', data, null))
}

function step(session: string, data: AgentStep): void {
  emit(envelope('agent.step', data, session))
}

function task(id: string, session: string, title: string, state: TaskState, reason: string | null = null): void {
  const now = new Date().toISOString()
  const known = tasks.get(id)
  const next: Task = {
    id,
    title,
    kind: 'voice',
    session_id: session,
    state,
    version: (known?.version ?? 0) + 1,
    reason,
    created_at: known?.created_at ?? now,
    updated_at: now,
  }
  tasks.set(id, next)
  emit(envelope('task.updated', next, session))
}

function approval(id: string, taskId: string, session: string, summary: string, status: Approval['status'], by: string | null = null): void {
  const now = new Date()
  const next: Approval = {
    id,
    task_id: taskId,
    summary,
    status,
    deadline: approvals.get(id)?.deadline ?? new Date(now.getTime() + 120_000).toISOString(),
    decided_by: by,
    decided_at: by ? now.toISOString() : null,
    created_at: approvals.get(id)?.created_at ?? now.toISOString(),
    credentials: [],
  }
  approvals.set(id, next)
  emit(envelope(status === 'pending' ? 'approval.requested' : 'approval.resolved', next, session))
}

const status: DeviceStatus = { online: true, state: 'idle', battery_percent: null, wifi_rssi: null, firmware_version: null }

function play(loop: number): Array<() => void> {
  const s = `turn_${loop}`
  const t = `tsk_mock_${loop}`
  const a = `apr_mock_${loop}`
  return [
    () => device({ ...status, state: 'listen' }),
    () => device({ ...status, state: 'transcribe' }),
    () => {
      step(s, { step_id: 'request', kind: 'request', status: 'done', title: 'Pay my electric bill' })
      task(t, s, 'Pay my electric bill', 'running')
      device({ ...status, state: 'respond' })
    },
    () => step(s, { step_id: 'tool-1', kind: 'tool', status: 'running', title: 'browser_navigate' }),
    () => {
      step(s, { step_id: 'tool-1', kind: 'tool', status: 'done', title: 'browser_navigate' })
      step(s, { step_id: 'tool-2', kind: 'tool', status: 'running', title: 'browser_click' })
      task(t, s, 'Pay my electric bill', 'awaiting_approval')
      approval(a, t, s, 'Click "Pay $84.12" on duke-energy.com', 'pending')
    },
  ]
}

export const mockFeed: EventFeed = {
  connect(onEvent) {
    listener = onEvent
    let loop = 1
    let index = 0
    let script = play(loop)
    const timer = setInterval(() => {
      const waiting = [...approvals.values()].some((item) => item.status === 'pending')
      if (index < script.length) {
        script[index]()
        index += 1
      } else if (!waiting) {
        loop += 1
        index = 0
        script = play(loop)
      }
    }, 1500)
    device(status)
    return () => {
      clearInterval(timer)
      listener = undefined
    }
  },

  async tasks() {
    return [...tasks.values()].map((item) => ({ task: item, approvals: [...approvals.values()].filter((a) => a.task_id === item.id) }))
  },

  async decide(item, approve) {
    const owner = tasks.get(item.task_id)
    if (!owner || !owner.session_id) return
    approval(item.id, owner.id, owner.session_id, item.summary, approve ? 'approved' : 'denied', 'web')
    step(owner.session_id, { step_id: 'tool-2', kind: 'tool', status: approve ? 'done' : 'failed', title: 'browser_click' })
    task(owner.id, owner.session_id, owner.title, approve ? 'succeeded' : 'cancelled', approve ? 'Paid $84.12.' : null)
    step(owner.session_id, { step_id: 'response', kind: 'response', status: 'done', title: approve ? 'Paid $84.12.' : 'I did not pay.' })
    device({ ...status, state: 'idle' })
  },

  async cancel(item) {
    if (!item.session_id) return
    for (const pending of approvals.values()) {
      if (pending.task_id === item.id && pending.status === 'pending') approval(pending.id, item.id, item.session_id, pending.summary, 'cancelled', 'web')
    }
    task(item.id, item.session_id, item.title, 'cancelled', 'cancelled by web')
    device({ ...status, state: 'idle' })
  },

  async devices() {
    return devices
  },

  async revoke(item) {
    const index = devices.findIndex((d) => d.id === item.id)
    if (index >= 0) devices[index] = { ...devices[index], revoked_at: new Date().toISOString() }
  },
}
