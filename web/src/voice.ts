import { createLocalAudioTrack, LocalAudioTrack, Room, RoomEvent, Track } from 'livekit-client'

export type VoiceState = 'offline' | 'connecting' | 'ready' | 'starting' | 'recording' | 'transcribing' | 'responding'

type Session = {
  session_id: string
  server_url: string
  participant_token: string
  worker_identity: string
}

type Transcript = { turn_id: string; text?: string; final?: boolean; error?: string }

export class VoiceConnection {
  private room = new Room()
  private microphone?: LocalAudioTrack
  private session?: Session
  private turn?: string
  private starting?: Promise<void>
  private recording = false
  private closed = false
  private timer?: ReturnType<typeof setTimeout>
  private onState: (state: VoiceState) => void
  private onTranscript: (text: string, final: boolean) => void
  private onReply: (text: string) => void
  private onPlayback: (blocked: boolean) => void
  private onError: (message: string) => void
  private speaker?: HTMLAudioElement

  constructor(
    onState: (state: VoiceState) => void,
    onTranscript: (text: string, final: boolean) => void,
    onReply: (text: string) => void,
    onPlayback: (blocked: boolean) => void,
    onError: (message: string) => void,
  ) {
    this.onState = onState
    this.onTranscript = onTranscript
    this.onReply = onReply
    this.onPlayback = onPlayback
    this.onError = onError
  }

  async connect(): Promise<void> {
    this.onState('connecting')
    try {
      this.microphone = await createLocalAudioTrack({
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      })
      if (this.closed) throw new Error('Connection closed')
      await this.microphone.mute()
      const response = await fetch('/api/voice/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: AbortSignal.timeout(15000),
      })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail ?? 'Could not connect')
      this.session = body as Session
      if (this.closed) throw new Error('Connection closed')
      this.room.on(RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
        this.receiveEvent(payload, participant?.identity, topic)
      })
      this.room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
        if (participant.identity !== this.session?.worker_identity || track.kind !== Track.Kind.Audio) return
        this.speaker = track.attach() as HTMLAudioElement
        this.speaker.hidden = true
        document.body.appendChild(this.speaker)
      })
      this.room.on(RoomEvent.TrackUnsubscribed, (track) => {
        for (const element of track.detach()) element.remove()
        this.speaker = undefined
      })
      this.room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        this.onPlayback(!this.room.canPlaybackAudio)
      })
      this.room.on(RoomEvent.Disconnected, () => {
        if (!this.closed) void this.fail(new Error('Session ended. Connect again to continue.'))
      })
      await this.room.connect(this.session.server_url, this.session.participant_token)
      if (this.closed) throw new Error('Connection closed')
      await this.room.localParticipant.publishTrack(this.microphone, { source: Track.Source.Microphone })
      if (this.closed) throw new Error('Connection closed')
      this.onState('ready')
    } catch (error) {
      if (!this.closed) this.onError(error instanceof Error ? error.message : 'Could not connect your microphone')
      await this.disconnect()
    }
  }

  private receiveEvent(payload: Uint8Array, identity?: string, topic?: string): void {
    if ((topic !== 'slate.transcript' && topic !== 'slate.reply') || identity !== this.session?.worker_identity) return
    try {
      const event = JSON.parse(new TextDecoder().decode(payload)) as Transcript
      if (event.turn_id !== this.turn || this.closed) return
      if (event.error) this.onError(event.error)
      if (topic === 'slate.transcript' && typeof event.text === 'string') {
        this.onTranscript(event.text, event.final === true)
        if (event.final && event.text) this.onState('responding')
      }
      if (topic === 'slate.reply' && typeof event.text === 'string') this.onReply(event.text)
      if ((topic === 'slate.reply' && event.final) || event.error || (topic === 'slate.transcript' && event.final && !event.text)) {
        this.turn = undefined
        this.recording = false
        clearTimeout(this.timer)
        void this.microphone?.mute().catch((error) => this.fail(error))
        this.onState('ready')
      }
    } catch (error) {
      console.error('[slate.voice] Invalid transcript event', error)
    }
  }

  private async rpc(method: string, payload = ''): Promise<string> {
    if (!this.session || this.closed) throw new Error('Connect your microphone first')
    return this.room.localParticipant.performRpc({
      destinationIdentity: this.session.worker_identity,
      method,
      payload,
    })
  }

  async start(): Promise<void> {
    if (this.starting || this.turn || this.closed) return
    this.starting = this.begin()
    try {
      await this.starting
    } catch (error) {
      await this.fail(error)
    } finally {
      this.starting = undefined
    }
  }

  private async begin(): Promise<void> {
    this.onError('')
    this.onTranscript('', false)
    this.onReply('')
    this.onState('starting')
    this.turn = await this.rpc('start_turn')
    if (this.closed) return
    await this.microphone?.unmute()
    if (!this.turn || this.closed) return
    this.recording = true
    this.onState('recording')
    this.timer = setTimeout(() => void this.finish(), 120000)
  }

  async finish(): Promise<void> {
    try {
      await this.starting
      if (!this.turn || !this.recording || this.closed) return
      const turn = this.turn
      this.recording = false
      clearTimeout(this.timer)
      this.onState('transcribing')
      await this.microphone?.mute()
      if (this.turn === turn) await this.rpc('end_turn', turn)
    } catch (error) {
      await this.fail(error)
    }
  }

  async cancel(): Promise<void> {
    try {
      await this.starting
      const turn = this.turn
      if (!turn || this.closed) return
      this.turn = undefined
      this.recording = false
      clearTimeout(this.timer)
      await this.microphone?.mute()
      await this.rpc('cancel_turn', turn)
      this.onTranscript('', false)
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

  async enableAudio(): Promise<void> {
    try {
      await this.room.startAudio()
      this.onPlayback(!this.room.canPlaybackAudio)
    } catch (error) {
      this.onError(error instanceof Error ? error.message : 'Could not play Slate’s reply')
    }
  }

  async disconnect(): Promise<void> {
    this.closed = true
    this.turn = undefined
    this.recording = false
    clearTimeout(this.timer)
    this.microphone?.stop()
    this.speaker?.remove()
    this.speaker = undefined
    this.onPlayback(false)
    await this.room.disconnect()
    const session = this.session
    this.session = undefined
    if (session) {
      await fetch(`/api/voice/sessions/${session.session_id}`, { method: 'DELETE', keepalive: true })
        .catch((error) => console.error('[slate.voice] Session cleanup failed', error))
    }
    this.onState('offline')
  }
}
