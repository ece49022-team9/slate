# Slate

A portable voice assistant for everyday tasks. The ESP32-S3 handles audio and device feedback. Python handles speech, model calls, and browser tasks. React handles setup and approvals.

## Code

| Path | Work |
| --- | --- |
| `firmware/` | ESP32-S3 firmware |
| `server/slate/voice/` | Audio streaming, speech recognition and generation |
| `server/slate/agent/` | Model calls and task execution |
| `server/slate/browser/` | Browser sessions, tools and result checks |
| `server/slate/access/` | Accounts, pairing, permissions and credentials |
| `web/` | Setup, approvals and user control |
| `hardware/` | KiCad project and board sources |

One Python backend for now. Keep model calls with the agent and permission checks in access. The web app never gets provider secrets.

## Local setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node 24](https://nodejs.org/), then:

```sh
make setup
make server
```

In another terminal:

```sh
make web
```

Open [localhost:5173](http://127.0.0.1:5173). [API docs](http://127.0.0.1:8000/api/docs) and [OpenAPI](http://127.0.0.1:8000/api/openapi.json) come from the backend.

`make check` runs lint, formatting and the web build. CI also checks that the backend starts and responds.

## Firmware

With [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/index.html) loaded:

```sh
cd firmware
idf.py set-target esp32s3
idf.py build
```

After setting the target, you can use `make firmware` from the root. PlatformIO still needs a `platformio.ini` before `pio run` will work.

[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
