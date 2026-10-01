# Slate — Backend ↔ Web App Event Schema
Scope: Communication between the Python backend and the React web app only. Device ↔ backend audio/WebSocket traffic is a separate spec.

## 1. Goals

The web app needs to show four things:

1. Device status — Is the device online? What is it doing right now?
2. Agent activity — Which steps is the agent taking (model calls, tools, MCP, browser)?
3. Approvals — Sensitive actions and credential use that need user approval.
4. Credentials — Adding and managing saved logins, plus a usage history ("password activity").

Ground rules:

- The web app talks only to the Python backend. It never talks to the model provider, Infisical, or Playwright directly.
- Credential values are write-only from the web app. They can be submitted but never read back, and they never appear in any event.
- No database for now. The backend keeps recent events in an in-memory ring buffer.

## 2. Transport

- Backend → Web: WebSocket GET /api/ws/events - All live events (server push)
- Web → Backend: REST (JSON over HTTPS) - Approval decisions, credential changes, initial state
- Web → Backend: REST GET - Browser frames (images), event backfill

Why the split: Pushing events over WebSocket keeps the UI live. Writes go over REST, where authentication, validation, and error codes are simpler, and nothing sensitive travels over the long-lived socket.

This WebSocket is separate from the device audio WebSocket.

### Connection lifecycle

1. The web app logs in, which gives it a session cookie or token.
2. The web app calls GET /api/state to get the current snapshot.
3. The web app opens /api/ws/events?after={last_event_id}.
4. The backend replays any buffered events after that ID, then streams live events.
5. On disconnect, the web app reconnects with exponential backoff (1s → 2s → 4s … max 30s) and resumes from after={last_event_id}.
6. If the requested ID is no longer in the buffer, the backend sends system.resync. The web app then calls GET /api/state again.

## 3. Event Envelope

Every event sent over the WebSocket uses the same envelope:

```
{
  "v": 1,
  "id": "evt_01J9ZK3M8Q2X",
  "type": "agent.step",
  "ts": "2026-10-01T14:02:11.123Z",
  "device_id": "dev_01",
  "session_id": "ses_01J9ZK2A",
  "data": { }
}
```

- v: int - Schema version. Adding fields does not bump it. Removing or renaming fields does.
- id: string - Unique and sortable by time (ULID). Used as the resume cursor.
- type: string - {domain}.{event}. See section 4.
- ts: string - ISO 8601 timestamp in UTC, with milliseconds.
- device_id: string | null - null for events that are not about a device.
- session_id: string | null - One voice request, from wake word to final response. null if not tied to a request.
- data: object - Payload that depends on type.

Clients must ignore unknown type values and unknown fields. This lets the backend add things without breaking the UI.

## 4. Event Types

### 4.1 Device

#### device.status
The backend emits this whenever any field changes, and also as a heartbeat every 30 seconds while the device is connected.

```
{
  "online": true,
  "state": "listening",
  "battery_pct": 82,
  "charging": false,
  "wifi_rssi_dbm": -58,
  "firmware_version": "0.3.1",
  "last_seen": "2026-10-01T14:02:10.900Z"
}
```

- state: offline · idle · listening · thinking · speaking · error
- battery_pct: 0–100, or null if unknown
- wifi_rssi_dbm: Negative integer, or null

The device state maps to the voice pipeline:
- listening: wake word detected, audio is streaming.
- thinking: STT is done, and the agent or tools are running.
- speaking: TTS audio is playing.

### 4.2 Session (one voice request)

#### session.started
```
{ "trigger": "wake_word" }
```
trigger: wake_word · button · web

#### session.transcript
```
{ "role": "user", "text": "Order more paper towels on Amazon", "final": true }
```
- role: user (STT output) · assistant (text sent to TTS)
- final: false means a partial transcript that will be updated. The UI replaces it, it does not append it.

#### session.ended
```
{ "outcome": "completed", "duration_ms": 18420, "error": null }
```
outcome: completed · failed · cancelled

### 4.3 Agent

#### agent.step
This is sent at least twice per step: once when the step starts (running), and once when it finishes (succeeded or failed), with the same step_id. The UI should upsert by step_id.

```
{
  "step_id": "stp_04",
  "kind": "browser",
  "name": "browser.click",
  "summary": "Clicking 'Add to Cart' on amazon.com",
  "status": "running",
  "started_at": "2026-10-01T14:02:11.100Z",
  "duration_ms": null,
  "error": null
}
```

- kind: model · tool · mcp · browser
- name: Stable identifier, e.g. model.complete, gmail.list_messages, browser.navigate
- summary: A short, human-readable, redacted sentence. Raw tool arguments are not sent (see section 6).
- status: running · succeeded · failed · waiting_approval
- error: { "code": "...", "message": "..." } or null

waiting_approval means the step is blocked on an approval.requested.

### 4.4 Browser

#### browser.session
```
{
  "browser_session_id": "brs_01",
  "status": "open",
  "url": "https://www.amazon.com/cart",
  "title": "Amazon.com Shopping Cart"
}
```
status: open · closed. The backend emits this on open, close, and every navigation.

#### browser.frame
This is a notification that a new screenshot is ready. The image itself is not inside the event.

```
{ "browser_session_id": "brs_01", "frame_seq": 37, "url": "https://www.amazon.com/cart" }
```

The web app fetches the image with:
GET /api/browser/{browser_session_id}/frame.jpg?seq=37

- MVP rate: at most 1 frame per second, while the session is open.
- The frame is a JPEG, about 800px wide, quality around 60.
- Password and payment fields must be masked before capture. Use Playwright's screenshot({ mask: [...] }) on input[type=password] and card fields.

### 4.5 Approvals

#### approval.requested
```
{
  "approval_id": "apr_01",
  "kind": "action",
  "title": "Place order on amazon.com",
  "detail": "Bounty paper towels, 12 rolls — $28.49, card ending 4417",
  "risk": "high",
  "step_id": "stp_06",
  "credential_id": null,
  "expires_at": "2026-10-01T14:07:11.000Z"
}
```

- kind: action (purchase, send, submit, etc.) · credential (agent wants to use a saved login)
- risk: low · medium · high. The UI can use this for color or emphasis.
- credential_id: Set when kind = credential
- expires_at: After this time the request auto-resolves as expired

#### approval.resolved
```
{ "approval_id": "apr_01", "decision": "approved", "resolved_via": "web" }
```
- decision: approved · denied · expired · cancelled
- resolved_via: web · voice · timeout · system

The backend always emits this, even when the web app itself sent the decision. This keeps multiple open tabs in sync.

### 4.6 Credentials

These events contain metadata only. Secret values never appear in any event.

#### credential.changed
```
{
  "credential_id": "crd_01",
  "action": "created",
  "domain": "amazon.com",
  "label": "Personal Amazon",
  "username_hint": "mi****@gmail.com"
}
```
action: created · updated · deleted

#### credential.used
This is the "password activity" log.

```
{
  "credential_id": "crd_01",
  "domain": "amazon.com",
  "approval_id": "apr_02",
  "step_id": "stp_03",
  "result": "success"
}
```
result: success · failure · denied

### 4.7 System

- system.resync: {} - The client is too far behind. It should re-fetch GET /api/state.
- system.error: { "code": "...", "message": "..." } - A backend-level problem worth showing in the UI, e.g. stt_unavailable or model_timeout.

## 5. REST Endpoints

All endpoints require web app authentication. Errors use the format { "error": { "code": "...", "message": "..." } }.

- GET /api/state: Snapshot: device status, active session, open browser sessions, pending approvals, last 50 events
- GET /api/events?after={id}&limit=100: Backfill from the ring buffer
- POST /api/approvals/{approval_id}: Body { "decision": "approved" | "denied" }. Returns 409 if already resolved or expired.
- GET /api/credentials: List credential metadata (credential_id, domain, label, username_hint, created_at, last_used_at)
- POST /api/credentials: Body { "domain", "label", "username", "secret" }. Stored in Infisical. Returns metadata only.
- PUT /api/credentials/{credential_id}: Same body as create. Fields left out stay unchanged.
- DELETE /api/credentials/{credential_id}: Deletes the credential from Infisical
- GET /api/credentials/activity?limit=50: Recent credential.used records
- GET /api/browser/{id}/frame.jpg?seq={n}: Latest masked screenshot

There is no endpoint that returns a secret or username in plain form.

## 6. Redaction Rules (must-follow)

The backend applies these rules before an event is emitted or buffered:

1. No credential values, API keys, tokens, or session cookies in any event or log.
2. agent.step.summary is written for humans. Raw tool arguments and raw model output are not forwarded.
3. Email and message bodies are summarized, not copied. For example: "Read 3 new emails from Gmail."
4. Browser frames mask password, card number, CVV, and SSN fields.
5. username_hint is partially masked, e.g. mi\*\*\*\*@gmail.com.

## 7. Example: "Order paper towels on Amazon"

The events arrive in this order (envelopes omitted):

```
session.started        trigger=wake_word
device.status          state=listening
session.transcript     user "Order more paper towels on Amazon" (final)
device.status          state=thinking
agent.step             stp_01 model.complete           running → succeeded
agent.step             stp_02 browser.navigate         running   "Opening amazon.com"
browser.session        brs_01 open  https://www.amazon.com
agent.step             stp_02                          succeeded
approval.requested     apr_02 kind=credential  "Sign in to amazon.com as Personal Amazon"
agent.step             stp_03 browser.login            waiting_approval
approval.resolved      apr_02 approved via web
credential.used        crd_01 amazon.com success
agent.step             stp_03                          succeeded
browser.frame          brs_01 seq=1..n   (throughout)
agent.step             stp_04 browser.click            "Adding Bounty 12-pack to cart" → succeeded
approval.requested     apr_01 kind=action risk=high "Place order — $28.49"
agent.step             stp_06 browser.click            waiting_approval
approval.resolved      apr_01 approved via web
agent.step             stp_06                          succeeded
session.transcript     assistant "Done — your order is placed, arriving Friday." (final)
device.status          state=speaking
browser.session        brs_01 closed
session.ended          outcome=completed
device.status          state=idle
```

The mock event generator (next task) should replay this sequence on a loop so the frontend can be built before the real agent is ready.

## 8. TypeScript Types (for the web app)

```
type Envelope<T extends string, D> = {
  v: 1; id: string; type: T; ts: string;
  device_id: string | null; session_id: string | null; data: D;
};

type DeviceState = "offline" | "idle" | "listening" | "thinking" | "speaking" | "error";
type StepStatus = "running" | "succeeded" | "failed" | "waiting_approval";
type ErrorInfo = { code: string; message: string } | null;

export type SlateEvent =
  | Envelope<"device.status", { online: boolean; state: DeviceState; battery_pct: number | null;
      charging: boolean; wifi_rssi_dbm: number | null; firmware_version: string; last_seen: string }>
  | Envelope<"session.started", { trigger: "wake_word" | "button" | "web" }>
  | Envelope<"session.transcript", { role: "user" | "assistant"; text: string; final: boolean }>
  | Envelope<"session.ended", { outcome: "completed" | "failed" | "cancelled"; duration_ms: number; error: ErrorInfo }>
  | Envelope<"agent.step", { step_id: string; kind: "model" | "tool" | "mcp" | "browser"; name: string;
      summary: string; status: StepStatus; started_at: string; duration_ms: number | null; error: ErrorInfo }>
  | Envelope<"browser.session", { browser_session_id: string; status: "open" | "closed"; url: string; title: string }>
  | Envelope<"browser.frame", { browser_session_id: string; frame_seq: number; url: string }>
  | Envelope<"approval.requested", { approval_id: string; kind: "action" | "credential"; title: string;
      detail: string; risk: "low" | "medium" | "high"; step_id: string | null;
      credential_id: string | null; expires_at: string }>
  | Envelope<"approval.resolved", { approval_id: string; decision: "approved" | "denied" | "expired" | "cancelled";
      resolved_via: "web" | "voice" | "timeout" | "system" }>
  | Envelope<"credential.changed", { credential_id: string; action: "created" | "updated" | "deleted";
      domain: string; label: string; username_hint: string }>
  | Envelope<"credential.used", { credential_id: string; domain: string; approval_id: string | null;
      step_id: string | null; result: "success" | "failure" | "denied" }>
  | Envelope<"system.resync", Record<string, never>>
  | Envelope<"system.error", { code: string; message: string }>;
```

Suggestion: define these as Pydantic models in server/slate/api/ as the single source of truth, export JSON Schema from them, and generate the TS types from that schema, so the backend and frontend can't drift apart.
