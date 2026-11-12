import { connection, request, socketUrl } from '../slate'
import type { Device, SlateEvent, Task } from '../types/events'
import type { EventFeed, TaskDetail } from './feed'

export const socketFeed: EventFeed = {
  connect(onEvent) {
    let socket: WebSocket | undefined
    let lastId: string | undefined
    let delay = 1000
    let timer: ReturnType<typeof setTimeout> | undefined
    let closed = false

    const open = () => {
      const url = socketUrl('/api/events/socket')
      if (lastId) url.searchParams.set('after', lastId)
      socket = new WebSocket(url, ['slate', connection().token])
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
      socket.onclose = (event) => {
        if (closed) return
        if (event.code === 4401) {
          console.error('[slate.events] The owner token was refused')
          return
        }
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

  async tasks() {
    const tasks = await request<Task[]>('GET', '/api/tasks?limit=20')
    const waiting = tasks.filter((task) => task.state === 'awaiting_approval')
    const details = await Promise.all(waiting.map((task) => request<TaskDetail>('GET', `/api/tasks/${task.id}`)))
    return tasks.map((task) => details.find((detail) => detail.task.id === task.id) ?? { task, approvals: [] })
  },

  async decide(approval, approve, version) {
    await request('POST', `/api/approvals/${approval.id}`, { decision: approve ? 'approve' : 'deny', expected_version: version })
  },

  async cancel(task) {
    await request('POST', `/api/tasks/${task.id}/cancel`)
  },

  devices() {
    return request<Device[]>('GET', '/api/devices')
  },

  async revoke(device) {
    await request('DELETE', `/api/devices/${device.id}`)
  },
}
