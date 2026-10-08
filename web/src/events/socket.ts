import type { SlateEvent } from '../types/events'
import type { EventFeed } from './mock'

// Live events from the backend at /api/ws/events.
// Reconnects with backoff (1s, 2s, 4s ... 30s) and resumes after the last event ID.
export const socketFeed: EventFeed = {
  connect(onEvent) {
    let socket: WebSocket | undefined
    let lastId: string | undefined
    let delay = 1000
    let timer: ReturnType<typeof setTimeout> | undefined
    let closed = false

    const open = () => {
      const url = new URL('/api/ws/events', window.location.href)
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      if (lastId) url.searchParams.set('after', lastId)

      socket = new WebSocket(url)
      socket.onopen = () => {
        delay = 1000
      }
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as SlateEvent
          lastId = event.id
          onEvent(event)
        } catch (error) {
          console.error('[slate.events] Invalid event', error)
        }
      }
      socket.onclose = () => {
        if (closed) return
        timer = setTimeout(open, delay)
        delay = Math.min(delay * 2, 30000)
      }
    }

    open()
    return () => {
      closed = true
      clearTimeout(timer)
      socket?.close()
    }
  },
}