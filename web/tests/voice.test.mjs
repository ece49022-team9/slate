import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'

const source = stripTypeScriptTypes(readFileSync(new URL('../src/voice.ts', import.meta.url), 'utf8'))
  .replaceAll('import.meta.env', 'env')
  .replace('export class VoiceConnection', 'class VoiceConnection')

const flush = async () => { for (let index = 0; index < 12; index++) await Promise.resolve() }

function deferred() {
  let resolve
  const promise = new Promise(yes => { resolve = yes })
  return { promise, resolve }
}

function setup({ env = { VITE_SLATE_URL: 'wss://slate.example/', VITE_SLATE_DEVICE_TOKEN: 'device-token' }, permission, resume } = {}) {
  const events = [], sockets = [], contexts = [], worklets = [], logs = [], modules = new Map(), revoked = []
  const track = { enabled: true, stopped: false, stop() { this.stopped = true } }
  const microphone = { getTracks: () => [track], getAudioTracks: () => [track] }
  let constraints
  class Socket {
    static OPEN = 1
    readyState = 0
    sent = []
    constructor(url, protocols) { this.url = url; this.protocols = protocols; sockets.push(this) }
    open() { this.readyState = 1; this.onopen() }
    send(data) { this.sent.push(typeof data === 'string' ? JSON.parse(data) : data) }
    receive(data) { this.onmessage({ data: typeof data === 'string' || data instanceof ArrayBuffer ? data : JSON.stringify(data) }) }
    close() { this.readyState = 3; this.onclose?.() }
    error() { this.onerror() }
  }
  class Context {
    state = 'suspended'
    currentTime = 5
    destination = {}
    speakers = []
    audioWorklet = { addModule: async url => { this.module = await modules.get(url).text() } }
    constructor(options) { this.options = options; contexts.push(this) }
    async resume() {
      if (this.resumeError) throw this.resumeError
      if (resume) await resume.promise
      this.state = 'running'
      this.onstatechange?.()
    }
    async close() { this.state = 'closed'; this.onstatechange?.() }
    createMediaStreamSource(stream) {
      this.stream = stream
      this.source = { connect(node) { this.node = node }, disconnect() { this.disconnected = true } }
      return this.source
    }
    createBuffer(channels, length, rate) {
      const samples = new Float32Array(length)
      return { channels, rate, duration: length / rate, getChannelData: () => samples }
    }
    createBufferSource() {
      const speaker = {
        connect(destination) { this.destination = destination },
        start(time) { this.time = time },
        stop() { this.stopped = true },
        disconnect() { this.disconnected = true },
      }
      this.speakers.push(speaker)
      return speaker
    }
  }
  class Worklet {
    constructor(context, name, options) {
      this.context = context; this.name = name; this.options = options
      this.port = {
        generation: 0,
        postMessage(generation) { this.generation = generation },
        close() { this.closed = true },
        frame(pcm, generation = this.generation) { this.onmessage({ data: { generation, pcm } }) },
      }
      worklets.push(this)
    }
    connect(destination) { this.destination = destination }
    disconnect() { this.disconnected = true }
  }
  class ModuleURL extends URL {
    static createObjectURL(blob) { const url = `blob:${modules.size}`; modules.set(url, blob); return url }
    static revokeObjectURL(url) { revoked.push(url) }
  }
  const VoiceConnection = runInNewContext(`${source}\nVoiceConnection`, {
    env, WebSocket: Socket, AudioContext: Context, AudioWorkletNode: Worklet, URL: ModuleURL,
    navigator: { mediaDevices: { getUserMedia: async options => {
      constraints = options
      return permission ? permission.promise : microphone
    } } },
    ArrayBuffer, DataView, Blob, Error, setTimeout, clearTimeout,
    console: { error: (...args) => logs.push(args) },
  })
  const client = new VoiceConnection(
    state => events.push(['state', state]),
    (text, final) => events.push(['transcript', text, final]),
    text => events.push(['reply', text]),
    blocked => events.push(['playback', blocked]),
    text => events.push(['error', text]),
    (tool, approval) => events.push(['agent', tool, approval && { ...approval }]),
  )
  return { client, events, sockets, contexts, worklets, track, microphone, logs, revoked, get constraints() { return constraints } }
}

async function fixture(options) {
  const f = setup(options)
  const connecting = f.client.connect()
  await flush()
  assert.equal(f.sockets.length, 1)
  f.socket = f.sockets[0]
  f.socket.open()
  await connecting
  f.context = f.contexts[0]
  f.worklet = f.worklets[0]
  return f
}

async function start(f, turn = 'turn') {
  const starting = f.client.start()
  f.socket.receive({ type: 'turn', turn_id: turn })
  await starting
}

const pcm = values => {
  const frame = new ArrayBuffer(values.length * 2)
  const view = new DataView(frame)
  values.forEach((value, index) => view.setInt16(index * 2, value, true))
  return frame
}

const latestState = f => f.events.filter(([kind]) => kind === 'state').at(-1)[1]

test('connect authenticates the device socket and sends the 24 kHz hello first', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  assert.equal(f.socket.url, 'wss://slate.example/api/device/socket')
  assert.deepEqual(Array.from(f.socket.protocols), ['slate', 'device-token'])
  assert.equal(f.socket.binaryType, 'arraybuffer')
  assert.deepEqual(f.socket.sent, [{ type: 'hello', rate: 24000 }])
  assert.equal(f.context.options.sampleRate, 24000)
  assert.deepEqual({ ...f.constraints.audio }, { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 })
  assert.equal(f.track.enabled, false)
  assert.equal(f.worklet.name, 'slate-microphone')
  assert.equal(f.revoked.length, 1)
  assert.equal(latestState(f), 'ready')
})

test('missing URL or token reports a clear configuration error before microphone access', async () => {
  for (const env of [{}, { VITE_SLATE_URL: 'wss://slate.example' }, { VITE_SLATE_DEVICE_TOKEN: 'token' }]) {
    const f = setup({ env })
    await f.client.connect()
    assert.ok(f.events.some(([kind, text]) => kind === 'error' && text.includes('VITE_SLATE_URL') && text.includes('VITE_SLATE_DEVICE_TOKEN')))
    assert.equal(f.contexts.length, 0)
    assert.equal(f.sockets.length, 0)
    assert.equal(latestState(f), 'offline')
  }
})

test('Talk waits for a turn, transmits only its microphone frames, then sends end', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const frame = pcm([100, -100])
  const starting = f.client.start()
  assert.equal(latestState(f), 'starting')
  f.worklet.port.frame(frame)
  assert.equal(f.socket.sent.filter(data => data instanceof ArrayBuffer).length, 0)
  f.socket.receive({ type: 'turn', turn_id: 'current' })
  await starting
  assert.equal(f.track.enabled, true)
  assert.equal(latestState(f), 'recording')
  f.worklet.port.frame(frame)
  assert.equal(f.socket.sent.at(-1), frame)
  await f.client.finish()
  assert.equal(f.track.enabled, false)
  assert.deepEqual(f.socket.sent.at(-1), { type: 'end' })
  f.worklet.port.frame(frame)
  assert.deepEqual(f.socket.sent.at(-1), { type: 'end' })
  assert.equal(latestState(f), 'transcribing')
})

test('releasing Talk before acknowledgement finishes the admitted recording', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const starting = f.client.start()
  const finishing = f.client.finish()
  f.socket.receive({ type: 'turn', turn_id: 'quick' })
  await Promise.all([starting, finishing])
  assert.equal(f.track.enabled, false)
  assert.deepEqual(f.socket.sent.at(-1), { type: 'end' })
})

test('cancel invalidates a pending start and its late acknowledgement cannot open the microphone', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const starting = f.client.start()
  await f.client.cancel()
  await starting
  assert.deepEqual(f.socket.sent.at(-1), { type: 'cancel' })
  f.socket.receive({ type: 'turn', turn_id: 'cancelled' })
  assert.equal(f.track.enabled, false)
  assert.equal(latestState(f), 'ready')
})

test('a new start ignores the acknowledgement and frames from a cancelled start', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const old = f.client.start()
  const oldGeneration = f.worklet.port.generation
  await f.client.cancel()
  const next = f.client.start()
  f.socket.receive({ type: 'turn', turn_id: 'old' })
  assert.equal(f.track.enabled, false)
  f.socket.receive({ type: 'turn', turn_id: 'new' })
  await Promise.all([old, next])
  assert.equal(f.track.enabled, true)
  const count = f.socket.sent.length
  f.worklet.port.frame(pcm([1]), oldGeneration)
  assert.equal(f.socket.sent.length, count)
  f.worklet.port.frame(pcm([1]))
  assert.equal(f.socket.sent.length, count + 1)
})

test('stale and missing turn IDs cannot change the transcript, reply, tool, or approval', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  await start(f, 'old')
  await f.client.finish()
  const next = f.client.start()
  const count = f.events.length
  for (const turn_id of ['old', undefined]) {
    for (const event of [
      { type: 'transcript', text: 'old', final: true },
      { type: 'reply', text: 'old', final: true },
      { type: 'tool', tool: 'old' },
      { type: 'approval', run_id: 'old', request_id: 'old', command: 'old' },
      { type: 'cancelled' }, { type: 'error', message: 'old error' },
    ]) f.socket.receive({ ...event, turn_id })
  }
  assert.equal(f.events.length, count)
  f.socket.receive({ type: 'turn', turn_id: 'new' })
  await next
  assert.equal(latestState(f), 'recording')
})

test('final transcript enters responding and every terminal event ends the current turn', async () => {
  for (const terminal of [
    { type: 'reply', text: 'done', final: true },
    { type: 'cancelled', message: 'cancelled' },
    { type: 'error', message: 'failed' },
    { type: 'transcript', text: '', final: true },
  ]) {
    const f = await fixture()
    try {
      await start(f)
      await f.client.finish()
      f.socket.receive({ type: 'transcript', turn_id: 'turn', text: 'hello', final: true })
      assert.equal(latestState(f), 'responding')
      f.socket.receive({ ...terminal, turn_id: 'turn' })
      assert.equal(latestState(f), 'ready')
      assert.equal(f.track.enabled, false)
      const count = f.events.length
      f.socket.receive({ type: 'reply', turn_id: 'turn', text: 'late reply' })
      assert.equal(f.events.length, count)
      if (terminal.type === 'error') assert.ok(f.events.some(([kind, text]) => kind === 'error' && text === 'failed'))
    } finally { await f.client.disconnect() }
  }
})

test('approvals are sent only once for the current turn and current request', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  await start(f, 'old')
  const old = { run_id: 'old-run', request_id: 'old-request', command: 'old command' }
  f.socket.receive({ type: 'approval', turn_id: 'old', ...old })
  await f.client.finish()
  await start(f, 'new')
  const current = { run_id: 'run', request_id: 'request', command: 'command' }
  f.socket.receive({ type: 'approval', turn_id: 'new', ...current })
  const count = f.socket.sent.length
  await f.client.approve(old, 'once')
  await f.client.approve({ ...current, request_id: 'different' }, 'once')
  assert.equal(f.socket.sent.length, count)
  await f.client.approve(current, 'deny')
  assert.deepEqual(f.socket.sent.at(-1), { type: 'approve', turn_id: 'new', run_id: 'run', request_id: 'request', choice: 'deny' })
  assert.deepEqual(f.events.at(-1), ['agent', '', undefined])
  await f.client.approve(current, 'once')
  assert.equal(f.socket.sent.length, count + 1)
})

test('tool progress and approval requests reach the existing agent callback', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  await start(f)
  f.socket.receive({ type: 'tool', turn_id: 'turn', tool: 'terminal' })
  assert.deepEqual(f.events.at(-1), ['agent', 'terminal', undefined])
  const approval = { run_id: 'run', request_id: 'request', command: 'echo hello' }
  f.socket.receive({ type: 'approval', turn_id: 'turn', ...approval })
  assert.deepEqual(f.events.at(-1), ['agent', '', approval])
})

test('announcements reach replies and enable speech only while idle', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const announcement = { type: 'reply', turn_id: 'background', text: 'Build passed', announcement: true, final: true }
  f.socket.receive(announcement)
  assert.deepEqual(f.events.at(-1), ['reply', 'Build passed'])
  f.socket.receive(pcm([100]))
  assert.equal(f.context.speakers.length, 1)
  const starting = f.client.start()
  const count = f.events.length
  f.socket.receive(announcement)
  f.socket.receive(pcm([100]))
  assert.equal(f.events.length, count)
  assert.equal(f.context.speakers.length, 1)
  f.socket.receive({ type: 'turn', turn_id: 'turn' })
  await starting
  const activeCount = f.events.length
  f.socket.receive(announcement)
  assert.equal(f.events.length, activeCount)
  await f.client.cancel()
  f.socket.receive(announcement)
  assert.deepEqual(f.events.at(-1), ['reply', 'Build passed'])
})

test('reply PCM16LE is decoded and adjacent chunks are scheduled without gaps', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  await start(f)
  await f.client.finish()
  f.socket.receive(pcm([100]))
  assert.equal(f.context.speakers.length, 0)
  f.socket.receive({ type: 'reply', turn_id: 'turn', text: 'hello', final: false })
  f.socket.receive(pcm([-32768, 0, 16384, 32767]))
  f.socket.receive(pcm([1, 2]))
  const [first, second] = f.context.speakers
  assert.equal(first.buffer.channels, 1)
  assert.equal(first.buffer.rate, 24000)
  assert.deepEqual(Array.from(first.buffer.getChannelData(0)), [-1, 0, 0.5, 32767 / 32768])
  assert.equal(first.time, f.context.currentTime)
  assert.equal(second.time, first.time + first.buffer.duration)
  first.onended()
  assert.equal(first.disconnected, true)
})

test('cancel, server cancellation, error, and new Talk stop scheduled audio', async () => {
  for (const action of ['cancel', 'cancelled', 'error', 'start']) {
    const f = await fixture()
    try {
      await start(f)
      await f.client.finish()
      f.socket.receive({ type: 'reply', turn_id: 'turn', text: 'hello' })
      f.socket.receive(pcm([100]))
      if (action === 'cancel') await f.client.cancel()
      else if (action === 'start') {
        const starting = f.client.start()
        f.socket.receive({ type: 'turn', turn_id: 'next' })
        await starting
      } else f.socket.receive({ type: action, turn_id: 'turn', message: 'failed' })
      const speaker = f.context.speakers[0]
      assert.equal(speaker.stopped, true)
      assert.equal(speaker.disconnected, true)
      f.socket.receive(pcm([100]))
      assert.equal(f.context.speakers.length, 1)
    } finally { await f.client.disconnect() }
  }
})

test('enableAudio resumes the audio context and exposes autoplay failure', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  f.context.state = 'suspended'
  f.context.onstatechange()
  assert.deepEqual(f.events.at(-1), ['playback', true])
  f.context.resumeError = new Error('Audio blocked')
  await f.client.enableAudio()
  assert.ok(f.events.some(([kind, value]) => kind === 'playback' && value === true))
  assert.deepEqual(f.events.at(-1), ['error', 'Audio blocked'])
  f.context.resumeError = undefined
  await f.client.enableAudio()
  assert.equal(f.context.state, 'running')
  assert.deepEqual(f.events.at(-1), ['playback', false])
})

test('a pending autoplay resume does not block the socket connection', async t => {
  const resume = deferred()
  const f = await fixture({ resume })
  t.after(() => f.client.disconnect())
  assert.equal(latestState(f), 'ready')
  assert.equal(f.context.state, 'suspended')
  assert.ok(f.events.some(([kind, blocked]) => kind === 'playback' && blocked === true))
  resume.resolve()
  await flush()
  assert.equal(f.context.state, 'running')
  assert.deepEqual(f.events.at(-1), ['playback', false])
})

test('display commands receive an explicit unsupported receipt', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  f.socket.receive({ type: 'command', request_id: 'display', operation: 'render', arguments: {} })
  assert.deepEqual(f.socket.sent.at(-1), { type: 'receipt', request_id: 'display', error: 'This client has no display' })
})

test('unexpected socket closure and error fail the connection with the reconnect message', async () => {
  for (const end of ['close', 'error']) {
    const f = await fixture()
    f.socket[end]()
    await flush()
    assert.ok(f.events.some(([kind, text]) => kind === 'error' && text === 'Session ended. Connect again to continue.'))
    assert.equal(latestState(f), 'offline')
    assert.equal(f.track.stopped, true)
    assert.equal(f.context.state, 'closed')
  }
})

test('disconnect closes the socket, microphone, processor, sources, and audio context', async () => {
  const f = await fixture()
  const starting = f.client.start()
  await f.client.disconnect()
  await starting
  assert.equal(f.socket.readyState, 3)
  assert.equal(f.track.stopped, true)
  assert.equal(f.track.enabled, false)
  assert.equal(f.worklet.port.closed, true)
  assert.equal(f.worklet.disconnected, true)
  assert.equal(f.context.source.disconnected, true)
  assert.equal(f.context.state, 'closed')
  assert.equal(latestState(f), 'offline')
  const count = f.events.length
  f.socket.receive({ type: 'turn', turn_id: 'late' })
  f.socket.receive({ type: 'reply', text: 'late', announcement: true })
  f.socket.receive(pcm([1]))
  await f.client.disconnect()
  assert.equal(f.events.length, count)
})

test('disconnect while microphone permission is pending stops the eventual stream', async () => {
  const permission = deferred()
  const f = setup({ permission })
  const connecting = f.client.connect()
  await f.client.disconnect()
  permission.resolve(f.microphone)
  await connecting
  assert.equal(f.track.stopped, true)
  assert.equal(f.sockets.length, 0)
  assert.equal(f.contexts[0].state, 'closed')
  assert.equal(latestState(f), 'offline')
})

test('the microphone worklet emits 480 sample PCM16LE frames and resets partial frames on a new turn', async t => {
  const f = await fixture()
  t.after(() => f.client.disconnect())
  const frames = []
  let WorkletProcessor
  class AudioWorkletProcessor {
    port = { postMessage: data => frames.push(data) }
  }
  runInNewContext(f.context.module, { AudioWorkletProcessor, ArrayBuffer, DataView, registerProcessor: (_name, processor) => { WorkletProcessor = processor } })
  const worklet = new WorkletProcessor()
  worklet.port.onmessage({ data: 1 })
  worklet.process([[new Float32Array(300).fill(1)]])
  assert.equal(frames.length, 0)
  worklet.port.onmessage({ data: 2 })
  worklet.process([[new Float32Array(480).fill(-1)]])
  assert.equal(frames.length, 1)
  assert.equal(frames[0].generation, 2)
  assert.equal(frames[0].pcm.byteLength, 960)
  const view = new DataView(frames[0].pcm)
  for (let offset = 0; offset < 960; offset += 2) assert.equal(view.getInt16(offset, true), -32768)
})
