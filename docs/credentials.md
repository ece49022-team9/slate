# Credential store

Part of the Security and Credential Management subsystem. Code:
`server/slate/access/vault.py` (encryption), `server/slate/access/credentials.py`
(store), `server/slate/api/credentials.py` (routes).

## The problem

To book, pay, and read email for the user, the agent needs the user's
passwords, tokens, and card details. Those must not sit in a file anyone can
read, must never show up in the web app, and must only be used for work the
user is actually doing.

## How it works

**Encrypted at rest.** Every secret is encrypted with AES-256-GCM before it is
written. Each one gets its own random nonce. GCM also detects changes, so an
edited byte makes decryption fail instead of returning garbage.

**Bound to its row.** The encryption includes the credential's id, service,
and kind as "associated data". Copying the card's ciphertext into the
password's row does not work: it fails to decrypt.

**Master key kept apart.** The key comes from `SLATE_MASTER_KEY` (64 hex chars)
or from `~/.config/slate/master.key` (mode 600, made on first start). The
databases are in `~/.slate/`. Someone who copies the database alone gets
nothing readable.

**Write-only API.** The web app can add, rename, replace, delete, and list.
The list shows only the service, label, username, and a hint (card last 4).
No route returns a secret. Bodies are parsed by hand so error messages never
echo what was sent (FastAPI's default errors would echo the password).

**Agent access only with the user's approval, only for that task.** The agent
calls `store.use(credential_id, task_id=..., purpose=...)`. It works only if
both are true:

1. the task is `running` (not queued, waiting for approval, or finished), and
2. the user **approved** an approval for this task that **named this exact
   credential**.

So a task paying the electric bill cannot open the Gmail password, even if a
web page tricks the agent (prompt injection) into trying. Every attempt,
allowed or refused, goes into an audit log with the approval that allowed it.
The log is kept even after the credential is deleted.

## How a credential gets granted

1. The agent asks: `manager.request_approval(task_id, "Pay $84.12 to Duke
   Energy?", credentials=[duke_id])`. Unknown ids are refused before anything
   is written.
2. The approval is saved together with the service and label of each named
   credential, in the same transaction. That is what the user is shown (the
   `approval.requested` event and `GET /api/tasks/{id}` include
   `credentials`), and it stays as shown even if the credential is renamed
   later.
3. The user approves, denies, or lets it time out.
   - Approved: the task runs again and may use exactly those credentials.
   - Denied, timed out, or cancelled: the task ends, nothing is granted.
4. When the task ends (succeeded, failed, cancelled, timed out), every grant
   ends with it. While the task waits on a second approval, earlier grants
   pause too.

| Refusal reason | Meaning |
|---|---|
| `not_found` | no such credential (or it was deleted) |
| `no_such_task` | no such task |
| `task_<state>` | the task is not running right now |
| `not_granted` | the task is running, but no approved approval named this credential |

The check (`TaskStore.credential_access`) reads the task state and the grants
from one database snapshot. It also only counts approvals whose status is
`approved`, as a second lock in case the task state were ever wrong.

**Rotation.** Every value records which key sealed it.
`uv run python -m slate.access rotate` (server stopped) makes a new key,
re-encrypts all credentials and device secrets, and removes the old key.

**Device secrets too.** The device signing keys from `docs/device-auth.md` are
now encrypted the same way. Older plain rows are encrypted the first time the
server starts.

| Kind | Required fields |
|---|---|
| `password` | `password` |
| `token` | `token` |
| `api_key` | `key` |
| `card` | `number`, `expiry`, `cvc` |

## Endpoints (local web app only)

| Endpoint | What |
|---|---|
| `POST /api/credentials` `{service, label, kind, username?, secret: {...}}` | add |
| `GET /api/credentials` | list (no secrets) |
| `GET /api/credentials/{id}` | details and recent uses |
| `PATCH /api/credentials/{id}` `{label?, username?}` | rename |
| `PUT /api/credentials/{id}/secret` `{secret: {...}}` | replace the secret |
| `DELETE /api/credentials/{id}` | delete (audit log stays) |

## Results

- 25 tests for the store and 21 for grants (all 140 pass). The grant tests
  include a model-based test: 150 random runs of starting tasks, asking,
  approving, denying, ending, and using credentials. A simple model says when
  each use must be allowed, and the real code must agree every time.
- For grants, I put 9 bugs into the code one at a time (any approval grants
  every credential, a grant works for another task, no running check, a
  pending approval counts, grants not saved, and others). The tests caught
  all 9.
- The store tests check that the database file never
  contains a plain secret, that every changed byte is detected, that swapped
  rows and a wrong master key read nothing, that tasks which are not running
  are refused and logged, that errors never show secret values, that rotation
  works, and that old plain device rows get encrypted.
- For the store, I put 7 bugs into the code one at a time (no task check, secret not bound to
  its row, same nonce every time, stored without encryption, refusals not
  logged, an error message showing the value, device secret kept plain). The
  tests caught all 7.
- On a running server, searching the database file for the saved password and
  card number found nothing.

## Limits and next steps

- The master key is on the same computer as the database. This protects
  against a copied database or backup, not against someone with full access to
  the account. Next: keep the key in the macOS Keychain or a cloud key service.
- Python cannot reliably wipe a secret from memory after use. The agent should
  not keep the value longer than the step that needs it.
- Not yet connected to the real agent: when browser tasks log in, they
  should call `use()`. The task demo (`SLATE_DEMO_TASKS=1`) already does: if a
  duke-energy.com credential is saved, its approval names it and, once
  approved, it uses it.
- A device approval grants credentials too. The device screen is small, so
  the summary text should say what will be used. We may later require the
  web app for approvals that name credentials.
- A Credentials page in the web app (add and list) is not built yet.
