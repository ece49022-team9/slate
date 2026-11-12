import type { AgentStep, Approval, DeviceStatus, SlateEvent, Task } from '../types/events'
import type { TaskDetail } from './feed'

export type Session = {
  id: string
  started: string
  steps: AgentStep[]
}

export type SlateState = {
  device: DeviceStatus | null
  lastSeen: string | null
  sessions: Session[]
  tasks: Task[]
  approvals: Approval[]
}

export type Loaded = { type: 'tasks.loaded'; details: TaskDetail[] }

export const initial: SlateState = { device: null, lastSeen: null, sessions: [], tasks: [], approvals: [] }
const KEPT_TASKS = 20

function withTask(tasks: Task[], task: Task): Task[] {
  const known = tasks.find((t) => t.id === task.id)
  if (known && known.version > task.version) return tasks
  const others = tasks.filter((t) => t.id !== task.id)
  return [task, ...others].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, KEPT_TASKS)
}

function withApproval(approvals: Approval[], approval: Approval): Approval[] {
  const others = approvals.filter((a) => a.id !== approval.id)
  return approval.status === 'pending' ? [...others, approval] : others
}

export function reduce(state: SlateState, event: SlateEvent | Loaded): SlateState {
  switch (event.type) {
    case 'device.status':
      return { ...state, device: event.data, lastSeen: event.ts }

    case 'agent.step': {
      const id = event.session_id ?? 'unknown'
      const session = state.sessions.find((s) => s.id === id) ?? { id, started: event.ts, steps: [] }
      const known = session.steps.some((s) => s.step_id === event.data.step_id)
      const steps = known
        ? session.steps.map((s) => (s.step_id === event.data.step_id ? event.data : s))
        : [...session.steps, event.data]
      const others = state.sessions.filter((s) => s.id !== id)
      return { ...state, sessions: [{ ...session, steps }, ...others].slice(0, 10) }
    }

    case 'task.updated':
      return { ...state, tasks: withTask(state.tasks, event.data) }

    case 'approval.requested':
    case 'approval.resolved':
      return { ...state, approvals: withApproval(state.approvals, event.data) }

    case 'tasks.loaded':
      return {
        ...state,
        tasks: event.details.reduce((tasks, detail) => withTask(tasks, detail.task), state.tasks),
        approvals: event.details
          .flatMap((detail) => detail.approvals)
          .reduce((approvals, approval) => withApproval(approvals, approval), state.approvals),
      }

    case 'system.resync':
      return { ...initial, tasks: state.tasks, approvals: state.approvals }

    default:
      return state
  }
}
