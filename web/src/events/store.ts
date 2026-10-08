import { useEffect, useReducer } from 'react'
import type { AgentStep, DeviceStatus, SlateEvent } from '../types/events'
import type { EventFeed } from './mock'

export type Session = {
  id: string
  started: string
  steps: AgentStep[]
}

export type SlateState = {
  device: DeviceStatus | null
  lastSeen: string | null
  sessions: Session[] // newest first
}

const initial: SlateState = { device: null, lastSeen: null, sessions: [] }

function reduce(state: SlateState, event: SlateEvent): SlateState {
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

    // Schema rule: ignore event types we don't know yet.
    default:
      return state
  }
}

export function useSlateEvents(feed: EventFeed): SlateState {
  const [state, dispatch] = useReducer(reduce, initial)
  useEffect(() => feed.connect(dispatch), [feed])
  return state
}