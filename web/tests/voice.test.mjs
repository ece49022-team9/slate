import assert from 'node:assert/strict'
import { test } from 'node:test'
import { VoiceConnection } from '../src/voice.ts'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fixture(rpc = async ({ method }) => method === 'start_turn' ? 'new-turn' : 'ok') {
  const events = [], calls = []
  const client = new VoiceConnection(
    state => events.push(['state', state]),
    text => events.push(['transcript', text]),
    text => events.push(['reply', text]),
    () => {},
    text => events.push(['error', text]),
    (tool, approval) => events.push(['agent', tool, approval]),
  )
  Object.assign(client, {
    session: { session_id: 'session', worker_identity: 'worker' },
    microphone: { mute: async () => calls.push('mute'), unmute: async () => calls.push('unmute'), stop() {} },
    speaker: { muted: false, remove() {} },
    room: { localParticipant: { performRpc: request => { calls.push(request); return rpc(request) } }, disconnect: async () => {} },
    turn: 'old-turn',
  })
  const receive = (topic, body, identity = 'worker') => client.receiveEvent(new TextEncoder().encode(JSON.stringify(body)), identity, topic)
  return { client, events, calls, receive }
}

test('Talk interrupts response immediately and only current reply unmutes playback', async () => {
  const receipt = deferred()
  const { client, events, receive } = fixture(async ({ method }) => method === 'start_turn' ? receipt.promise : 'ok')
  const start = client.start()
  assert.equal(client.speaker.muted, true)
  assert.equal(client.turn, undefined)
  const count = events.length
  receive('slate.reply', { turn_id: 'old-turn', text: 'late answer', final: true })
  receive('slate.agent', { turn_id: 'old-turn', approval: { run_id: 'old', request_id: 'old', command: 'old' } })
  assert.equal(events.length, count)
  receipt.resolve('new-turn')
  await start
  await client.finish()
  assert.equal(client.speaker.muted, true)
  receive('slate.reply', { text: 'missing ID' })
  receive('slate.reply', { turn_id: 'new-turn', text: 'wrong sender' }, 'other')
  assert.equal(client.speaker.muted, true)
  receive('slate.reply', { turn_id: 'new-turn', text: 'new answer', final: false })
  assert.equal(client.speaker.muted, false)
  receive('slate.reply', { turn_id: 'new-turn', text: 'new answer', final: true })
  assert.equal(client.turn, undefined)
})

test('cancel while start is pending cancels its eventual receipt without opening microphone', async () => {
  const receipt = deferred()
  const { client, calls } = fixture(async ({ method }) => method === 'start_turn' ? receipt.promise : 'ok')
  const start = client.start()
  const cancel = client.cancel()
  assert.equal(client.speaker.muted, true)
  receipt.resolve('new-turn')
  await Promise.all([start, cancel])
  assert.equal(calls.includes('unmute'), false)
  assert.ok(calls.some(call => call.method === 'cancel_turn' && call.payload === 'new-turn'))
  assert.equal(client.turn, undefined)
  assert.equal(client.recording, false)
})

test('an old approval response cannot clear the new turn approval', async () => {
  const resolution = deferred()
  const { client, events, calls, receive } = fixture(async ({ method }) => method === 'approve_tool' ? resolution.promise : method === 'start_turn' ? 'new-turn' : 'ok')
  const old = { run_id: 'old-run', request_id: 'old-request', command: 'old command' }
  receive('slate.agent', { turn_id: 'old-turn', approval: old })
  const approve = client.approve(old, 'once')
  await client.start()
  await client.finish()
  const next = { run_id: 'new-run', request_id: 'new-request', command: 'new command' }
  receive('slate.agent', { turn_id: 'new-turn', approval: next })
  const count = events.length
  resolution.resolve('ok')
  await approve
  await client.approve(old, 'once')
  assert.equal(events.length, count)
  assert.equal(calls.filter(call => call.method === 'approve_tool').length, 1)
})

test('delayed old cancellation failure cannot disconnect a new recording', async () => {
  const cancellation = deferred()
  const { client, events } = fixture(async ({ method }) => method === 'cancel_turn' ? cancellation.promise : 'new-turn')
  const cancel = client.cancel()
  await Promise.resolve()
  await Promise.resolve()
  await client.start()
  cancellation.reject(new Error('old request expired'))
  await cancel
  assert.equal(client.closed, false)
  assert.equal(client.turn, 'new-turn')
  assert.equal(events.at(-1)[1], 'recording')
  await client.finish()
})

test('server cancellation mutes audio and returns the current turn to ready', () => {
  const { client, events, receive } = fixture()
  receive('slate.reply', { turn_id: 'old-turn', cancelled: true })
  assert.equal(client.speaker.muted, true)
  assert.equal(client.turn, undefined)
  assert.ok(events.some(event => event[0] === 'state' && event[1] === 'ready'))
})

test('current approval clears after its RPC succeeds', async () => {
  const { client, events, receive } = fixture()
  const approval = { run_id: 'run', request_id: 'request', command: 'command' }
  receive('slate.agent', { turn_id: 'old-turn', approval })
  await client.approve(approval, 'once')
  assert.deepEqual(events.at(-1), ['agent', '', undefined])
  assert.equal(client.approval, undefined)
})

test('cancel skips a queued microphone open for an invalidated turn', async () => {
  const blocked = deferred()
  const { client, calls } = fixture()
  client.microphoneChanges = blocked.promise
  const start = client.start()
  await Promise.resolve()
  const cancel = client.cancel()
  blocked.resolve()
  await Promise.all([start, cancel])
  assert.equal(calls.includes('unmute'), false)
  assert.equal(client.recording, false)
})

test('terminal packet makes a pending end RPC failure stale', async () => {
  for (const terminal of [{ cancelled: true }, { text: 'done', final: true }]) {
    const ending = deferred()
    const admitted = deferred()
    const { client, events, receive } = fixture(async ({ method }) => {
      if (method === 'end_turn') { admitted.resolve(); return ending.promise }
      return 'ok'
    })
    client.recording = true
    const finish = client.finish()
    await admitted.promise
    receive('slate.reply', { turn_id: 'old-turn', ...terminal })
    const count = events.length
    ending.reject(new Error('This recording has ended'))
    await finish
    assert.equal(client.closed, false)
    assert.equal(events.length, count)
    assert.equal(client.turn, undefined)
  }
})
