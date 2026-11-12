# Signed device commands

Part of the Security and Credential Management subsystem. Code:
`server/slate/access/` (signing, device store) and `server/slate/api/device.py`.

## The problem

Until now only the web app on the same computer could approve a task. The
Slate device is on Wi-Fi, so it is on the network, not on localhost. We need it
to approve a payment or cancel a task, but no one else on the network should
be able to. Also, `POST /api/approvals` let the caller *say* `"source": "device"`,
so anyone who could reach it could pretend to be the device.

## How it works

1. **Pairing.** The web app calls `POST /api/devices`. The server makes a
   device id and a random 32-byte secret and shows the secret once. It goes on
   the device and is never sent again.
2. **Signing.** For every request, the device builds this text and signs it
   with HMAC-SHA256 using the secret:

   ```
   SLATE-HMAC-SHA256-V1
   POST
   /api/device/approvals/apr_1
   1800000000                         <- unix time
   00112233445566778899aabbccddeeff   <- random nonce, never reused
   dev_0123456789abcdef               <- device id
   <sha256 of the body, hex>
   ```

   It sends the device id, time, nonce, and signature in `X-Slate-*` headers.
3. **Checking.** The server rebuilds the same text and compares signatures. If
   any part was changed on the way, they do not match.

| Check | Stops | Reply |
|---|---|---|
| Headers present and well formed | junk requests | 401 `bad_format` |
| Time within 30 s of the server | old captured requests | 401 `clock_skew` |
| Time not before the server started | replays right after a restart | 401 `before_boot` |
| Device paired and not revoked | lost or stolen devices | 401 `unknown_device` / `revoked` |
| Signature matches (constant-time compare) | changed requests, wrong key, pretending to be another device | 401 `bad_signature` |
| Nonce not used before | sending a captured request again | 401 `replay` |
| At most 600 requests per device per 30 s | floods from a device | 429 `rate_limited` |

The nonce is saved only after the signature passes, so someone without the
key cannot use up a device's nonces. Saving it is locked, so two copies of a
request that arrive at the same moment cannot both pass. Every 401 includes
`server_time` so a device with a wrong clock can fix it and try again.

## Endpoints

| Who | Endpoint | What |
|---|---|---|
| Web app (local only) | `POST /api/devices` `{name}` | pair; returns the secret once |
| Web app (local only) | `GET /api/devices` | list (never shows secrets) |
| Web app (local only) | `DELETE /api/devices/{id}` | revoke |
| Web app (local only) | `GET /api/devices/auth-stats` | accepted / refused counts by reason |
| Device (signed) | `GET /api/device/ping` | check the link and the clock |
| Device (signed) | `POST /api/device/approvals/{id}` `{decision, expected_version?}` | approve or deny |
| Device (signed) | `POST /api/device/tasks/{id}/cancel` | cancel |

Approvals made this way are recorded as `decided_by: "device:<id>"`. The web
route now always records `"web"`, whatever `source` says.

## For the firmware

ESP-IDF has everything needed: `mbedtls_md_hmac` (HMAC-SHA256),
`esp_fill_random` (nonce), and SNTP (clock). Check the C version against this
known answer (also a unit test):

- secret: bytes `00 01 02 ... 1f` (32 bytes)
- method `POST`, path `/api/device/approvals/apr_1`, time `1800000000`
- nonce `00112233445566778899aabbccddeeff`, device `dev_0123456789abcdef`
- body `{"decision":"approve"}` (sha256 `2ddd116c830744f5435c7391895c584921c7e83c19313ba43a30582a5e94e4ea`)
- signature `aa3d79384a1d8d108e808ff6c912e3d50892b90b21ca202c86acd57c0d717cb3`

The Python result was also checked against `openssl dgst -sha256 -mac HMAC`.

## Try it in the web app

Run `SLATE_DEMO_TASKS=1 make server` and `make web`, open the **Device lab**
tab, and press **Pair a device**. The page plays the device: it shows the
lines it signs and the signature, sends the command, and shows the server's
answer. Red buttons try attacks (replay, changed body, wrong clock, wrong key,
unknown device). Server counts by reason update live.

## Try it from the terminal

```sh
make server                                           # other terminal
uv run python scripts/device_cmd.py pair "My Slate"   # saves .local/device.json
uv run python scripts/device_cmd.py ping              # 200
uv run python scripts/device_cmd.py ping --replay     # 200, then 401 replay
uv run python scripts/device_cmd.py ping --skew 120   # 401 clock_skew
uv run python scripts/device_cmd.py ping --wrong-key  # 401 bad_signature
uv run python scripts/device_cmd.py approve apr_... --tamper  # 401 bad_signature
uv run python scripts/bench_device_auth.py            # cost of one check
```

## Results

- 26 new tests (all 94 pass). One uses Hypothesis to change one random byte of
  the path, body, or signature 300 times; every change is refused. One sends
  16 copies of a request at the same moment; exactly one passes.
- To check the tests themselves, I put 7 bugs into the checker one at a time
  (body not signed, device id not signed, no restart check, no replay check,
  nonce saved before the signature check, `>=` instead of `>` for the time
  window, revoked devices allowed). The tests caught all 7.
- One check takes about 7 us (median) with an empty body and 13 us with a
  16 KB body on a 2-core Linux VM. That is tiny next to a network round trip.

## Limits and next steps

- HMAC proves *who sent it* and *that it was not changed*. It does not hide the
  content. We still need TLS for privacy.
- The secret is stored in plain form in `~/.slate/devices.sqlite3` (file mode
  600), because HMAC needs the real secret. Next: encrypt it at rest, together
  with the user's account credentials (credential store).
- Pairing means copying the secret by hand. Later: QR code or BLE pairing, and
  a Devices page in the web app.
- Nonces are kept in memory, which is fine for our one-process server. With
  more than one process they would need to be shared (for example, SQLite).
- `unknown_device` and `bad_signature` are different replies, which tells an
  attacker whether a device id exists. That helps debugging now; we can merge
  them before release.
