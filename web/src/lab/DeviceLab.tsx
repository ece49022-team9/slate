import { useCallback, useEffect, useMemo, useState } from 'react'
import { canonicalLines, randomHex, sign, type Draft, type Lab } from './signing'

type Command = 'ping' | 'approve' | 'deny' | 'cancel'

type Attack = 'none' | 'tamper' | 'skew' | 'wrong-key' | 'unknown-device' | 'replay'

type Pending = { approval: string; task: string; summary: string }

type Sent = {
  n: number
  label: string
  attack: Attack
  draft: Draft
  sentBody: string
  headers: Record<string, string>
  status: number
  reply: unknown
}

const STORAGE = 'slate.deviceLab'

const attacks: Array<{ id: Exclude<Attack, 'none' | 'replay'>; label: string; what: string }> = [
  { id: 'tamper', label: 'Change the body after signing', what: 'Signs one decision, sends the opposite.' },
  { id: 'skew', label: 'Clock 2 minutes off', what: 'Signs with a time 120 s in the future.' },
  { id: 'wrong-key', label: 'Wrong key', what: 'Signs with a random key, like an attacker guessing.' },
  { id: 'unknown-device', label: 'Unknown device', what: 'Claims a device id that was never paired.' },
]

const meaning: Record<string, string> = {
  ok: 'The signature matched, so the server ran the command.',
  replay: 'This exact request (same nonce) was already used once. A copied request cannot be sent again.',
  bad_signature: 'The signature does not match what arrived. Something changed after signing, or the key is wrong.',
  clock_skew: 'The time in the request is more than 30 s from the server clock. Old requests expire.',
  before_boot: 'This was signed before the server started, so it may be a replay from before a restart.',
  unknown_device: 'No paired device has this id.',
  revoked: 'This device was revoked, so its key no longer works.',
  bad_format: 'The signing headers are missing or malformed.',
  rate_limited: 'Too many requests from this device in 30 s.',
  not_found: 'The signature was fine, but there is no such approval or task.',
  conflict: 'The signature was fine, but the task said no (already decided, or in the wrong state).',
}

function load(): Lab | null {
  try {
    const raw = localStorage.getItem(STORAGE)
    return raw ? (JSON.parse(raw) as Lab) : null
  } catch {
    return null
  }
}

function save(lab: Lab | null): void {
  try {
    if (lab) localStorage.setItem(STORAGE, JSON.stringify(lab))
    else localStorage.removeItem(STORAGE)
  } catch {
    // Storage is a convenience here; the lab still works without it.
  }
}

function reasonOf(s: Sent): string {
  if (s.status >= 200 && s.status < 300) return 'ok'
  const reply = s.reply as { reason?: string } | null
  if (reply && typeof reply === 'object' && reply.reason) return reply.reason
  if (s.status === 404) return 'not_found'
  if (s.status === 409) return 'conflict'
  return `http_${s.status}`
}

function request(command: Command, target: Pending | null): Pick<Draft, 'method' | 'path' | 'body'> {
  if (command === 'ping') return { method: 'GET', path: '/api/device/ping', body: '' }
  if (command === 'cancel') return { method: 'POST', path: `/api/device/tasks/${target?.task ?? 'tsk_none'}/cancel`, body: '' }
  return {
    method: 'POST',
    path: `/api/device/approvals/${target?.approval ?? 'apr_none'}`,
    body: JSON.stringify({ decision: command }),
  }
}

export default function DeviceLab() {
  const [lab, setLab] = useState<Lab | null>(load)
  const [command, setCommand] = useState<Command>('ping')
  const [pending, setPending] = useState<Pending[]>([])
  const [target, setTarget] = useState<string>('')
  // Time and nonce of the request on screen. Made fresh at every send.
  const [seed, setSeed] = useState(() => ({ timestamp: Math.floor(Date.now() / 1000), nonce: randomHex(16) }))
  const [lines, setLines] = useState<Array<[string, string]>>([])
  const [signature, setSignature] = useState('')
  const [log, setLog] = useState<Sent[]>([])
  const [stats, setStats] = useState<Record<string, number>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const chosen = pending.find((p) => p.approval === target) ?? pending[0] ?? null

  const refresh = useCallback(async () => {
    try {
      const tasks = (await (await fetch('/api/tasks?limit=20')).json()) as Array<{ id: string; state: string }>
      const waiting: Pending[] = []
      for (const t of tasks.filter((t) => t.state === 'awaiting_approval')) {
        const detail = (await (await fetch(`/api/tasks/${t.id}`)).json()) as {
          approvals: Array<{ id: string; status: string; summary: string }>
        }
        for (const a of detail.approvals.filter((a) => a.status === 'pending')) {
          waiting.push({ approval: a.id, task: t.id, summary: a.summary })
        }
      }
      setPending(waiting)
      const s = await fetch('/api/devices/auth-stats')
      if (s.ok) setStats((await s.json()) as Record<string, number>)
    } catch {
      setError('Cannot reach the backend. Is make server running?')
    }
  }, [])

  useEffect(() => {
    // Poll the backend: first right away, then every 3 s.
    const first = setTimeout(() => void refresh(), 0)
    const timer = setInterval(() => void refresh(), 3000)
    return () => {
      clearTimeout(first)
      clearInterval(timer)
    }
  }, [refresh])

  const draft = useMemo<Draft | null>(
    () => (lab ? { ...request(command, chosen), ...seed, deviceId: lab.id } : null),
    [lab, command, chosen, seed],
  )

  useEffect(() => {
    if (!draft || !lab) return
    let live = true
    void (async () => {
      const [l, sig] = await Promise.all([canonicalLines(draft), sign(lab.secret, draft)])
      if (live) {
        setLines(l)
        setSignature(sig)
      }
    })()
    return () => {
      live = false
    }
  }, [draft, lab])

  async function pair(): Promise<void> {
    setError('')
    const reply = await fetch('/api/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Device lab' }),
    })
    if (!reply.ok) {
      setError(`Pairing failed: ${reply.status} ${await reply.text()}`)
      return
    }
    const made = (await reply.json()) as { device: { id: string }; secret: string }
    const next = { id: made.device.id, secret: made.secret }
    save(next)
    setLab(next)
    setLog([])
  }

  async function revoke(): Promise<void> {
    if (!lab) return
    await fetch(`/api/devices/${lab.id}`, { method: 'DELETE' })
    void refresh()
  }

  function forget(): void {
    save(null)
    setLab(null)
    setLog([])
  }

  async function transmit(label: string, attack: Attack, d: Draft, sentBody: string, headers: Record<string, string>) {
    const reply = await fetch(d.path, {
      method: d.method,
      headers: d.method === 'POST' ? { ...headers, 'Content-Type': 'application/json' } : headers,
      body: d.method === 'POST' ? sentBody : undefined,
    })
    const text = await reply.text()
    let parsed: unknown = text
    try {
      parsed = JSON.parse(text)
    } catch {
      // keep the text
    }
    setLog((old) => [
      { n: (old[0]?.n ?? 0) + 1, label, attack, draft: d, sentBody, headers, status: reply.status, reply: parsed },
      ...old,
    ])
  }

  async function send(attack: Exclude<Attack, 'replay'>): Promise<void> {
    if (!lab || !draft) return
    setBusy(true)
    setError('')
    try {
      // Fresh time and nonce at the moment of sending, like a real device.
      const fresh = { ...draft, timestamp: Math.floor(Date.now() / 1000), nonce: randomHex(16) }
      setSeed({ timestamp: fresh.timestamp, nonce: fresh.nonce })
      let d = fresh
      let key = lab.secret
      let sentBody = draft.body
      if (attack === 'skew') d = { ...d, timestamp: d.timestamp + 120 }
      if (attack === 'wrong-key') key = randomHex(32)
      if (attack === 'unknown-device') d = { ...d, deviceId: `dev_${randomHex(8)}` }
      if (attack === 'tamper') {
        const signed = command === 'approve' ? 'deny' : 'approve'
        d = { ...d, body: JSON.stringify({ decision: signed }) }
        sentBody = draft.body
      }
      const sig = await sign(key, d)
      const headers = {
        'X-Slate-Device': d.deviceId,
        'X-Slate-Timestamp': String(d.timestamp),
        'X-Slate-Nonce': d.nonce,
        'X-Slate-Signature': sig,
      }
      const label = attack === 'none' ? 'Normal request' : attacks.find((a) => a.id === attack)!.label
      await transmit(label, attack, d, sentBody, headers)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function replay(): Promise<void> {
    const last = log.find((s) => s.status >= 200 && s.status < 300)
    if (!last) return
    setBusy(true)
    try {
      await transmit('Replay: same request sent again', 'replay', last.draft, last.sentBody, last.headers)
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  const needsTarget = command !== 'ping'
  const canSend = !!lab && !!draft && !busy && (!needsTarget || !!chosen)
  const canReplay = !busy && log.some((s) => s.status >= 200 && s.status < 300)
  const refused = Object.entries(stats).filter(([k]) => k !== 'accepted' && k !== 'remembered_nonces')
  const maxCount = Math.max(1, ...Object.entries(stats).filter(([k]) => k !== 'remembered_nonces').map(([, v]) => v))

  return (
    <main className="lab">
      <p className="eyebrow">Security and credential management</p>
      <h1>Device lab</h1>
      <p className="hint">
        This page plays the Slate device. It signs each command with the device's secret key, sends it, and shows what
        the server decided. Use the red buttons to try attacks.
      </p>
      {error && <p className="error">{error}</p>}

      <section>
        <h2>1. Pair a device</h2>
        {lab ? (
          <>
            <dl className="stats">
              <div><dt>Device id</dt><dd className="mono">{lab.id}</dd></div>
              <div><dt>Secret key (shown once by the server)</dt><dd className="mono">{lab.secret.slice(0, 12)}…</dd></div>
            </dl>
            <div className="controls" style={{ marginTop: '1rem' }}>
              <button className="secondary" onClick={() => void revoke()}>Revoke this device</button>
              <button className="secondary" onClick={forget}>Forget and pair a new one</button>
            </div>
          </>
        ) : (
          <>
            <p className="hint">The server makes a device id and a random 32-byte key. Only the device and the server know the key.</p>
            <div className="controls" style={{ marginTop: '1rem' }}>
              <button onClick={() => void pair()}>Pair a device</button>
            </div>
          </>
        )}
      </section>

      {lab && (
        <section>
          <h2>2. Pick a command</h2>
          <div className="controls">
            {(['ping', 'approve', 'deny', 'cancel'] as Command[]).map((c) => (
              <button key={c} className={command === c ? '' : 'secondary'} onClick={() => setCommand(c)}>
                {c === 'ping' ? 'Ping' : c === 'cancel' ? 'Cancel task' : c === 'approve' ? 'Approve' : 'Deny'}
              </button>
            ))}
          </div>
          {needsTarget && (
            pending.length === 0 ? (
              <p className="hint">No task is waiting for approval. Start the server with SLATE_DEMO_TASKS=1 and one appears every ~20 s.</p>
            ) : (
              <label className="pick">
                Task waiting for approval:{' '}
                <select value={chosen?.approval ?? ''} onChange={(e) => setTarget(e.target.value)}>
                  {pending.map((p) => (
                    <option key={p.approval} value={p.approval}>{p.summary} ({p.approval})</option>
                  ))}
                </select>
              </label>
            )
          )}
        </section>
      )}

      {lab && draft && (
        <section>
          <h2>3. What the device signs</h2>
          <p className="hint">These lines are joined and signed with the secret key (HMAC-SHA256). Change any line and the signature changes completely. Time and nonce are made fresh each time you send.</p>
          <table className="canon">
            <tbody>
              {lines.map(([name, value]) => (
                <tr key={name}><th>{name}</th><td className="mono">{value}</td></tr>
              ))}
              <tr className="sig"><th>signature</th><td className="mono">{signature}</td></tr>
            </tbody>
          </table>
          {draft.body && <p className="hint">Body: <span className="mono">{draft.body}</span></p>}
        </section>
      )}

      {lab && draft && (
        <section>
          <h2>4. Send it</h2>
          <div className="controls">
            <button disabled={!canSend} onClick={() => void send('none')}>Send normally</button>
            <button className="attack" disabled={!canReplay} onClick={() => void replay()} title="Sends the last successful request again, unchanged. Try it within 30 s, and again after 30 s.">
              Replay last good request
            </button>
          </div>
          <p className="hint">Attacks:</p>
          <div className="controls">
            {attacks.map((a) => (
              <button
                key={a.id}
                className="attack"
                title={a.what}
                disabled={!canSend || (a.id === 'tamper' && command !== 'approve' && command !== 'deny')}
                onClick={() => void send(a.id)}
              >
                {a.label}
              </button>
            ))}
          </div>
        </section>
      )}

      {lab && (
        <section>
          <h2>Server counts</h2>
          <ul className="bars">
            <li><span>accepted</span><span className="bar ok" style={{ width: `${((stats.accepted ?? 0) / maxCount) * 100}%` }} /><b>{stats.accepted ?? 0}</b></li>
            {refused.map(([reason, count]) => (
              <li key={reason}><span>{reason}</span><span className="bar no" style={{ width: `${(count / maxCount) * 100}%` }} /><b>{count}</b></li>
            ))}
          </ul>
          <p className="hint">Nonces the server remembers right now: {stats.remembered_nonces ?? 0} (each is forgotten 30 s after its time).</p>
        </section>
      )}

      {log.length > 0 && (
        <section>
          <h2>Results</h2>
          <ol className="results">
            {log.map((s) => {
              const reason = reasonOf(s)
              const ok = reason === 'ok'
              return (
                <li key={s.n} className={ok ? 'ok' : 'no'}>
                  <div className="head">
                    <strong>#{s.n} {s.label}</strong>
                    <span className="pill">{s.status} {ok ? 'accepted' : reason}</span>
                  </div>
                  <p>{meaning[reason] ?? 'See the reply below.'}</p>
                  <details>
                    <summary>{s.draft.method} {s.draft.path}</summary>
                    <pre>{Object.entries(s.headers).map(([k, v]) => `${k}: ${v}`).join('\n')}{s.sentBody ? `\n\n${s.sentBody}` : ''}</pre>
                    <pre>{JSON.stringify(s.reply, null, 2)}</pre>
                  </details>
                </li>
              )
            })}
          </ol>
        </section>
      )}
    </main>
  )
}
