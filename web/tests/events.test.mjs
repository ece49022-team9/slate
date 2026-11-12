import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'

const source = stripTypeScriptTypes(readFileSync(new URL('../src/events/reduce.ts', import.meta.url), 'utf8'))
  .replaceAll('export ', '')
const scope = {}
runInNewContext(`${source}\nthis.reduce = reduce; this.initial = initial`, scope)
const { reduce, initial } = scope

const plain = (value) => JSON.parse(JSON.stringify(value))

let seq = 0
function event(type, data, session = 'turn_1') {
  seq += 1
  return { v: 1, id: `evt_${seq}`, type, ts: `2026-11-12T16:00:${String(seq).padStart(2, '0')}.000Z`, device_id: 'dev_1', session_id: session, data }
}

function task(id, version, state, created = '2026-11-12T16:00:00.000Z') {
  return { id, title: id, kind: 'voice', session_id: 'turn_1', state, version, reason: null, created_at: created, updated_at: created }
}

function approval(id, status) {
  return { id, task_id: 'tsk_a', summary: 'Pay $84', status, deadline: '2026-11-12T16:02:00.000Z', decided_by: null, decided_at: null, created_at: '2026-11-12T16:00:00.000Z', credentials: [] }
}

test('newest task first and an older version never replaces a newer one', () => {
  let state = reduce(initial, event('task.updated', task('tsk_a', 1, 'running', '2026-11-12T16:00:00.000Z')))
  state = reduce(state, event('task.updated', task('tsk_b', 1, 'running', '2026-11-12T16:01:00.000Z')))
  state = reduce(state, event('task.updated', task('tsk_a', 3, 'succeeded')))
  state = reduce(state, event('task.updated', task('tsk_a', 2, 'awaiting_approval')))
  assert.deepEqual(plain(state.tasks.map((t) => [t.id, t.state])), [['tsk_b', 'running'], ['tsk_a', 'succeeded']])
})

test('only pending approvals stay on screen', () => {
  let state = reduce(initial, event('approval.requested', approval('apr_1', 'pending')))
  assert.equal(state.approvals.length, 1)
  state = reduce(state, event('approval.resolved', approval('apr_1', 'approved')))
  assert.equal(state.approvals.length, 0)
})

test('loaded tasks merge with live ones and bring their pending approvals', () => {
  let state = reduce(initial, event('task.updated', task('tsk_a', 4, 'succeeded')))
  state = reduce(state, {
    type: 'tasks.loaded',
    details: [
      { task: task('tsk_a', 2, 'awaiting_approval'), approvals: [approval('apr_old', 'approved')] },
      { task: task('tsk_c', 1, 'awaiting_approval', '2026-11-12T15:00:00.000Z'), approvals: [approval('apr_2', 'pending')] },
    ],
  })
  assert.deepEqual(plain(state.tasks.map((t) => [t.id, t.state])), [['tsk_a', 'succeeded'], ['tsk_c', 'awaiting_approval']])
  assert.deepEqual(plain(state.approvals.map((a) => a.id)), ['apr_2'])
})

test('a resync clears live activity but keeps tasks', () => {
  let state = reduce(initial, event('task.updated', task('tsk_a', 1, 'running')))
  state = reduce(state, event('agent.step', { step_id: 'request', kind: 'request', status: 'done', title: 'Hi' }))
  state = reduce(state, event('system.resync', {}))
  assert.equal(state.sessions.length, 0)
  assert.equal(state.tasks.length, 1)
})

test('steps of one turn update in place', () => {
  let state = reduce(initial, event('agent.step', { step_id: 'tool-1', kind: 'tool', status: 'running', title: 'browser_click' }))
  state = reduce(state, event('agent.step', { step_id: 'tool-1', kind: 'tool', status: 'done', title: 'browser_click' }))
  assert.deepEqual(plain(state.sessions[0].steps.map((s) => s.status)), ['done'])
})
