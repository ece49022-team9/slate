import type { Approval, Device, SlateEvent, Task } from '../types/events'

export type TaskDetail = { task: Task; approvals: Approval[] }

export type EventFeed = {
  connect(onEvent: (event: SlateEvent) => void): () => void
  tasks(): Promise<TaskDetail[]>
  decide(approval: Approval, approve: boolean, version: number): Promise<void>
  cancel(task: Task): Promise<void>
  devices(): Promise<Device[]>
  revoke(device: Device): Promise<void>
}
