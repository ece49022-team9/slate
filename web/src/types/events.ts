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

// Tasks and approvals (server/slate/tasks)

export type TaskState =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'timed_out'

export type Task = {
  id: string
  title: string
  kind: string
  session_id: string | null
  state: TaskState
  version: number
  reason: string | null
  created_at: string
  updated_at: string
}

export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'timed_out' | 'cancelled'

export type Approval = {
  id: string
  task_id: string
  summary: string
  status: ApprovalStatus
  deadline: string
  decided_by: string | null
  decided_at: string | null
  created_at: string
}

export type TaskUpdatedEvent = Envelope<'task.updated', Task>
export type ApprovalRequestedEvent = Envelope<'approval.requested', Approval>
export type ApprovalResolvedEvent = Envelope<'approval.resolved', Approval>

// System

export type SystemResyncEvent = Envelope<'system.resync', Record<string, never>>

// All events the web app understands. Unknown types are ignored.

export type SlateEvent =
  | DeviceStatusEvent
  | AgentStepEvent
  | TaskUpdatedEvent
  | ApprovalRequestedEvent
  | ApprovalResolvedEvent
  | SystemResyncEvent