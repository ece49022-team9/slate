export type VoiceState = 'offline' | 'connecting' | 'ready' | 'starting' | 'recording' | 'transcribing' | 'responding'
export type Approval = { run_id: string; request_id: string; command: string }

type VoiceEvent = {
  type: string
  turn_id?: string
  text?: string
  final?: boolean
  announcement?: boolean
  message?: string
  tool?: string
  run_id?: string
  request_id?: string
  command?: string
}

type PendingTurn = { generation: number; resolve: () => void }
const rate = 24000
const processor = `
class Microphone extends AudioWorkletProcessor {
  constructor() {
    super()
    this.frame = new ArrayBuffer(960)
    this.samples = new DataView(this.frame)
    this.offset = 0
    this.generation = 0
    this.port.onmessage = ({ data }) => {
      this.generation = data
      this.offset = 0
    }
  }
  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input) return true
    for (const sample of input) {
      const value = Math.max(-1, Math.min(1, sample))
      this.samples.setInt16(this.offset * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true)
      if (++this.offset === 480) {
        this.port.postMessage({ generation: this.generation, pcm: this.frame }, [this.frame])
        this.frame = new ArrayBuffer(960)
        this.samples = new DataView(this.frame)
        this.offset = 0
      }
    }
    return true
  }
}
registerProcessor('slate-microphone', Microphone)
`

export class VoiceConnection {
  private socket?: WebSocket
  private context?: AudioContext
  private microphone?: MediaStream
  private source?: MediaStreamAudioSourceNode
  private worklet?: AudioWorkletNode
  private opening?: () => void
  private turn?: string
  private starting?: Promise<void>
  private pendingTurns: PendingTurn[] = []
  private generation = 0
  private approval?: Approval
  private recording = false
  private playback = false
  private playhead = 0
  private speakers = new Set<AudioBufferSourceNode>()
  private closed = false
  private timer?: ReturnType<typeof setTimeout>
  private onState: (state: VoiceState) => void
  private onTranscript: (text: string, final: boolean) => void
  private onReply: (text: string) => void
  private onPlayback: (blocked: boolean) => void
  private onError: (message: string) => void
  private onAgent: (tool: string, approval?: Approval) => void

  constructor(
    onState: (state: VoiceState) => void,
    onTranscript: (text: string, final: boolean) => void,
    onReply: (text: string) => void,
    onPlayback: (blocked: boolean) => void,
    onError: (message: string) => void,
    onAgent: (tool: string, approval?: Approval) => void,
  ) {
    this.onState = onState
    this.onTranscript = onTranscript
    this.onReply = onReply
    this.onPlayback = onPlayback
    this.onError = onError
    this.onAgent = onAgent
  }

  async connect(): Promise<void> {
    this.onState('connecting')
    try {
      const base = import.meta.env?.VITE_SLATE_URL
      const token = import.meta.env?.VITE_SLATE_DEVICE_TOKEN
      if (!base || !token) throw new Error('Set VITE_SLATE_URL and VITE_SLATE_DEVICE_TOKEN to connect to Slate.')
      const url = new URL(`${base.replace(/\/$/, '')}/api/device/socket`)
      if (url.protocol !== 'wss:' && url.protocol !== 'ws:') throw new Error('VITE_SLATE_URL must use wss:// or ws://.')
      const context = this.context = new AudioContext({ sampleRate: rate })
      context.onstatechange = () => {
        if (!this.closed) this.onPlayback(context.state !== 'running')
      }
      void this.enableAudio()
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      } })
      if (this.closed) {
        microphone.getTracks().forEach(track => track.stop())
        return
      }
      this.microphone = microphone
      this.setMicrophone(false)
      const module = URL.createObjectURL(new Blob([processor], { type: 'text/javascript' }))
      try {
        await context.audioWorklet.addModule(module)
      } finally {
        URL.revokeObjectURL(module)
      }
      if (this.closed) return
      this.source = context.createMediaStreamSource(microphone)
      this.worklet = new AudioWorkletNode(context, 'slate-microphone', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
      this.worklet.port.onmessage = ({ data }: MessageEvent<{ generation: number; pcm: ArrayBuffer }>) => {
        if (!this.closed && this.recording && data.generation === this.generation) {
          try {
            this.send(data.pcm)
          } catch (error) {
            void this.fail(error)
          }
        }
      }
      this.worklet.onprocessorerror = () => {
        if (!this.closed) void this.fail(new Error('Microphone audio processing failed. Connect again to continue.'))
      }
      this.source.connect(this.worklet)
      this.worklet.connect(context.destination)
      const socket = this.socket = new WebSocket(url.toString(), ['slate', token])
      socket.binaryType = 'arraybuffer'
      socket.onmessage = ({ data }) => this.receive(data)
      await new Promise<void>((resolve, reject) => {
        this.opening = () => reject(new Error('Connection closed'))
        socket.onopen = () => {
          if (this.closed) return
          try {
            this.send({ type: 'hello', rate })
            resolve()
          } catch (error) {
            reject(error)
          }
        }
        const ended = () => {
          reject(new Error('Session ended. Connect again to continue.'))
          if (!this.closed) void this.fail(new Error('Session ended. Connect again to continue.'))
        }
        socket.onclose = ended
        socket.onerror = ended
      })
      this.opening = undefined
      if (!this.closed) this.onState('ready')
    } catch (error) {
      if (!this.closed) await this.fail(error)
    }
  }

  private send(message: object | ArrayBuffer): void {
    if (this.closed || this.socket?.readyState !== WebSocket.OPEN) throw new Error('Connect your microphone first')
    this.socket.send(message instanceof ArrayBuffer ? message : JSON.stringify(message))
  }

  private receive(data: string | ArrayBuffer): void {
    if (this.closed) return
    try {
      if (data instanceof ArrayBuffer) {
        this.play(data)
        return
      }
      const event = JSON.parse(data) as VoiceEvent
      if (event.type === 'command' && typeof event.request_id === 'string') {
        this.send({ type: 'receipt', request_id: event.request_id, error: 'This client has no display' })
        return
      }
      if (event.type === 'turn') {
        if (typeof event.turn_id !== 'string') return
        const pending = this.pendingTurns.shift()
        if (!pending) return
        if (pending.generation === this.generation) {
          this.turn = event.turn_id
          this.worklet?.port.postMessage(this.generation)
          this.setMicrophone(true)
          this.recording = true
          this.onState('recording')
          this.timer = setTimeout(() => void this.finish(), 120000)
        }
        pending.resolve()
        return
      }
      if (event.announcement) {
        if (this.turn || this.starting || event.type !== 'reply') return
        if (typeof event.text === 'string' && event.text) {
          this.playback = true
          this.onReply(event.text)
        }
        return
      }
      if (!this.turn || event.turn_id !== this.turn) return
      if (event.type === 'tool') {
        this.approval = undefined
        this.onAgent(event.tool ?? '')
      } else if (event.type === 'approval') {
        if (typeof event.run_id !== 'string' || typeof event.request_id !== 'string' || typeof event.command !== 'string') return
        this.approval = { run_id: event.run_id, request_id: event.request_id, command: event.command }
        this.onAgent('', this.approval)
      } else if (event.type === 'transcript' && typeof event.text === 'string') {
        this.onTranscript(event.text, event.final === true)
        if (event.final && event.text) this.onState('responding')
      } else if (event.type === 'reply' && typeof event.text === 'string') {
        if (event.text) this.playback = true
        this.onReply(event.text)
      } else if (event.type === 'error') {
        console.error('[slate.voice] Turn failed', event.message)
        this.onError(event.message ?? 'Slate could not finish your request')
      }
      if ((event.type === 'reply' && event.final) || event.type === 'cancelled' || event.type === 'error' || (event.type === 'transcript' && event.final && !event.text)) {
        if (event.type === 'cancelled' || event.type === 'error') this.clearAudio()
        this.endTurn()
        this.onState('ready')
        this.onAgent('')
      }
    } catch (error) {
      console.error('[slate.voice] Invalid socket event', error)
    }
  }

  private setMicrophone(open: boolean): void {
    this.microphone?.getAudioTracks().forEach(track => { track.enabled = open })
  }

  private clearAudio(): void {
    this.playback = false
    this.playhead = 0
    for (const source of this.speakers) {
      source.stop()
      source.disconnect()
    }
    this.speakers.clear()
  }

  private play(pcm: ArrayBuffer): void {
    const context = this.context
    if (!context || !this.playback || pcm.byteLength === 0) return
    if (pcm.byteLength % 2) throw new Error('Reply audio must contain complete 16-bit samples')
    const buffer = context.createBuffer(1, pcm.byteLength / 2, rate)
    const samples = buffer.getChannelData(0)
    const view = new DataView(pcm)
    for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true) / 32768
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    source.onended = () => {
      source.disconnect()
      this.speakers.delete(source)
    }
    this.speakers.add(source)
    this.playhead = Math.max(context.currentTime, this.playhead)
    source.start(this.playhead)
    this.playhead += buffer.duration
    this.onPlayback(context.state !== 'running')
  }

  private endTurn(): void {
    ++this.generation
    this.turn = undefined
    this.approval = undefined
    this.recording = false
    clearTimeout(this.timer)
    this.setMicrophone(false)
    for (const pending of this.pendingTurns) pending.resolve()
    this.starting = undefined
  }

  async start(): Promise<void> {
    if (this.starting || this.recording || this.closed) return
    this.endTurn()
    const generation = this.generation
    this.clearAudio()
    this.onError('')
    this.onTranscript('', false)
    this.onReply('')
    this.onAgent('')
    this.onState('starting')
    void this.enableAudio()
    const starting = new Promise<void>(resolve => { this.pendingTurns.push({ generation, resolve }) })
    this.starting = starting
    try {
      this.send({ type: 'start' })
      await starting
    } catch (error) {
      if (generation === this.generation && !this.closed) await this.fail(error)
    } finally {
      if (this.starting === starting) this.starting = undefined
    }
  }

  async finish(): Promise<void> {
    const generation = this.generation
    await this.starting
    if (generation !== this.generation || !this.turn || !this.recording || this.closed) return
    this.recording = false
    this.setMicrophone(false)
    clearTimeout(this.timer)
    this.onState('transcribing')
    try {
      this.send({ type: 'end' })
    } catch (error) {
      if (generation === this.generation && !this.closed) await this.fail(error)
    }
  }

  async cancel(): Promise<void> {
    if (this.closed) return
    this.endTurn()
    this.clearAudio()
    this.onTranscript('', false)
    this.onReply('')
    this.onAgent('')
    try {
      this.send({ type: 'cancel' })
      this.onState('ready')
    } catch (error) {
      await this.fail(error)
    }
  }

  private async fail(error: unknown): Promise<void> {
    console.error('[slate.voice] Microphone session failed', error)
    if (!this.closed) this.onError(error instanceof Error ? error.message : 'Microphone session failed')
    await this.disconnect()
  }

  async approve(approval: Approval, choice: 'once' | 'deny'): Promise<void> {
    if (!this.turn || this.closed || this.approval?.run_id !== approval.run_id || this.approval.request_id !== approval.request_id) return
    try {
      this.send({ type: 'approve', turn_id: this.turn, run_id: approval.run_id, request_id: approval.request_id, choice })
      this.approval = undefined
      this.onAgent('')
    } catch (error) {
      console.error('[slate.voice] Approval failed', error)
      this.onError(error instanceof Error ? error.message : 'Could not resolve approval')
    }
  }

  async enableAudio(): Promise<void> {
    const context = this.context
    if (!context || this.closed) return
    this.onPlayback(context.state !== 'running')
    try {
      await context.resume()
      if (!this.closed) this.onPlayback(context.state !== 'running')
    } catch (error) {
      if (this.closed) return
      console.error('[slate.voice] Reply playback failed', error)
      this.onPlayback(true)
      this.onError(error instanceof Error ? error.message : 'Could not play Slate’s reply')
    }
  }

  async disconnect(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.endTurn()
    this.pendingTurns = []
    this.clearAudio()
    this.opening?.()
    this.opening = undefined
    this.socket?.close()
    this.socket = undefined
    this.worklet?.port.close()
    this.worklet?.disconnect()
    this.source?.disconnect()
    this.microphone?.getTracks().forEach(track => track.stop())
    this.microphone = undefined
    this.worklet = undefined
    this.source = undefined
    this.onPlayback(false)
    this.onAgent('')
    this.onState('offline')
    const context = this.context
    this.context = undefined
    if (context && context.state !== 'closed') {
      await context.close().catch(error => console.error('[slate.voice] Audio cleanup failed', error))
    }
  }
}
