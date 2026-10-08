// Event schema between the backend and the web app (v1).
// Every message on /api/ws/events uses this envelope.

export type Envelope<T extends string, D> = {
  v: 1
  id: string
  type: T
  ts: string
  device_id: string | null
  session_id: string | null
  data: D
}

// Device

export type DeviceState = 'idle' | 'listen' | 'mute' | 'transcribe' | 'respond' | 'error'

export type DeviceStatus = {
  online: boolean
  state: DeviceState
  battery_percent: number | null
  wifi_rssi: number | null
  firmware_version: string | null
}

export type DeviceStatusEvent = Envelope<'device.status', DeviceStatus>

// Agent

export type AgentStepKind = 'request' | 'model' | 'tool' | 'browser' | 'response'
export type AgentStepStatus = 'running' | 'done' | 'failed'

export type AgentStep = {
  step_id: string
  kind: AgentStepKind
  status: AgentStepStatus
  title: string
  detail?: string
}

export type AgentStepEvent = Envelope<'agent.step', AgentStep>

// All events the web app understands. Unknown types are ignored.

export type SlateEvent = DeviceStatusEvent | AgentStepEvent