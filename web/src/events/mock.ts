import type { AgentStep, DeviceStatus, SlateEvent } from '../types/events'

// Anything that can deliver events to the app.
// Today: mockFeed. Later: a real WebSocket feed with the same shape.
export type EventFeed = {
  connect(onEvent: (event: SlateEvent) => void): () => void
}

let counter = 0

function nextId(): string {
  counter += 1
  return `evt_${String(counter).padStart(4, '0')}`
}

function device(data: DeviceStatus): SlateEvent {
  return {
    v: 1,
    id: nextId(),
    type: 'device.status',
    ts: new Date().toISOString(),
    device_id: 'dev_01',
    session_id: null,
    data,
  }
}

function step(session: string, data: AgentStep): SlateEvent {
  return {
    v: 1,
    id: nextId(),
    type: 'agent.step',
    ts: new Date().toISOString(),
    device_id: 'dev_01',
    session_id: session,
    data,
  }
}

const status: DeviceStatus = {
  online: true,
  state: 'idle',
  battery_percent: 82,
  wifi_rssi: -54,
  firmware_version: '0.1.0',
}

// One request, start to finish. It replays forever with a new session ID.
const script: Array<(session: string) => SlateEvent> = [
  () => device({ ...status, state: 'idle' }),
  () => device({ ...status, state: 'listen' }),
  () => device({ ...status, state: 'transcribe' }),
  (s) => step(s, { step_id: 'st_1', kind: 'request', status: 'done', title: 'Order paper towels on Amazon' }),
  (s) => step(s, { step_id: 'st_2', kind: 'model', status: 'running', title: 'Planning the task' }),
  (s) => step(s, { step_id: 'st_2', kind: 'model', status: 'done', title: 'Planning the task' }),
  (s) => step(s, { step_id: 'st_3', kind: 'browser', status: 'running', title: 'Opening amazon.com' }),
  (s) => step(s, { step_id: 'st_3', kind: 'browser', status: 'done', title: 'Opening amazon.com' }),
  (s) => step(s, { step_id: 'st_4', kind: 'browser', status: 'running', title: 'Searching for paper towels' }),
  (s) => step(s, { step_id: 'st_4', kind: 'browser', status: 'done', title: 'Searching for paper towels', detail: 'Picked the top result, $24.99' }),
  (s) => step(s, { step_id: 'st_5', kind: 'tool', status: 'done', title: 'Used saved Amazon login' }),
  () => device({ ...status, state: 'respond' }),
  (s) => step(s, { step_id: 'st_6', kind: 'response', status: 'done', title: 'Found it. Waiting for your approval.' }),
  () => device({ ...status, state: 'idle', battery_percent: 81 }),
]

export const mockFeed: EventFeed = {
  connect(onEvent) {
    let index = 0
    let loop = 1
    const timer = setInterval(() => {
      const make = script[index]
      if (make) onEvent(make(`ses_${String(loop).padStart(2, '0')}`))
      index += 1
      if (index === script.length) {
        index = 0
        loop += 1
      }
    }, 1500)
    return () => clearInterval(timer)
  },
}