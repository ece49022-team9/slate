.PHONY: setup check web cloud-setup cloud-deploy cloud-logs voice-deploy voice-check dashboard browser-view agent-setup agent-login agent agent-check browser browser-stop board firmware flash monitor provision pair bench sim-setup sim sim-check sim-pair-check demo-local sim-voice device-code-check profile eval

FIRMWARE_ENV ?= slate-s3-voice-test
MODAL_PROFILE ?= sudarshan-1
MODAL := env -u MODAL_TOKEN_ID -u MODAL_TOKEN_SECRET MODAL_PROFILE=$(MODAL_PROFILE)
INPUT ?= .local/voice-check.wav
ESP32_PORTS := $(wildcard /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.usbmodem* /dev/cu.SLAB_USBtoUART* /dev/ttyUSB* /dev/ttyACM*)
PORT ?= $(if $(filter 1,$(words $(ESP32_PORTS))),$(firstword $(ESP32_PORTS)))
NEED_PORT = @test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Pass PORT=<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
NEED_WIFI = @test -n "$(SSID)" || (printf 'Pass SSID=<wifi name> PASSWORD=<wifi password>\n' >&2; exit 1)
PIO := uv tool run --from pioarduino==6.2.0 pio
PIO_PYTHON := $(or $(PLATFORMIO_CORE_DIR),$(HOME)/.platformio)/penv/bin/python
BUILD := firmware/.pio/build/$(FIRMWARE_ENV)
SIM_CHECKS ?= breadboard mic display speaker device resources cloud_link ble_setup

setup:
	uv sync --locked
	npm --prefix web ci

check:
	uv run python -m slate.board --check
	uv run ruff check server scripts tests hermes
	uv run ruff format --check server scripts tests hermes
	uv run python -m unittest discover -s tests
	npm --prefix web run lint
	npm --prefix web run build
	node --experimental-strip-types --test web/tests/*.test.mjs

web:
	npm --prefix web run dev

cloud-setup:
	doppler run --only-secrets OPENAI_API_KEY -- $(MODAL) uv run python -m slate.cloud setup
	$(MODAL) uv run python -m slate.cloud deploy

cloud-deploy:
	$(MODAL) uv run python -m slate.cloud deploy

cloud-logs:
	$(MODAL) uv run modal app logs slate

voice-deploy:
	$(MODAL) SLATE_DEMO=$(DEMO) uv run modal deploy -m slate.voice.stt
	$(MODAL) SLATE_DEMO=$(DEMO) uv run modal deploy -m slate.voice.tts

voice-check:
	$(MODAL) uv run python -m slate.voice check

dashboard:
	uv run python -m slate.cloud dashboard

browser-view:
	$(MODAL) uv run python -m slate.browser.view

agent-setup:
	uv run python -m slate.agent.runtime setup

agent-login:
	uv run python -m slate.agent.runtime login

agent:
	$(MODAL) uv run python -m slate.agent.runtime start

browser:
	$(MODAL) uv run python -m slate.browser.modal run

browser-stop:
	$(MODAL) uv run python -m slate.browser.modal stop

agent-check:
	$(MODAL) uv run python scripts/check_agent.py $(ARGS)

board:
	uv run python -m slate.board

firmware: board
	$(PIO) run -d firmware -e $(FIRMWARE_ENV)

flash: firmware
	$(NEED_PORT)
ifneq ($(filter /dev/cu.usbmodem%,$(PORT)),)
	"$(PIO_PYTHON)" scripts/paced_flash.py -p "$(PORT)" -b 460800 write-flash 0x0 $(BUILD)/bootloader.bin 0x8000 $(BUILD)/partitions.bin 0x10000 $(BUILD)/firmware.bin
else
	$(PIO) run -d firmware -e $(FIRMWARE_ENV) -t upload --upload-port "$(PORT)"
endif

monitor:
	$(NEED_PORT)
	$(PIO) device monitor -d firmware -e $(FIRMWARE_ENV) -p "$(PORT)" -b 921600

provision:
	$(NEED_PORT)
	$(NEED_WIFI)
	uv run python scripts/provision.py "$(PORT)" --ssid "$(SSID)" --password "$(PASSWORD)"

pair:
	$(NEED_WIFI)
	uv run python scripts/ble_setup.py --ssid "$(SSID)" --password "$(PASSWORD)"

bench: flash
ifeq ($(CALIBRATE),1)
	uv run python scripts/check_resources.py --port "$(PORT)" --calibrate
else
	uv run python scripts/check_bench.py "$(PORT)"
	uv run python scripts/check_resources.py --port "$(PORT)"
endif

sim-setup:
	bash scripts/qemu.sh

sim:
	uv run python -m slate.display

sim-check:
	@for check in $(SIM_CHECKS); do echo "uv run python scripts/check_$$check.py"; uv run python scripts/check_$$check.py || exit 1; done

sim-pair-check:
	uv run python scripts/check_pairing.py

demo-local:
	uv run python scripts/demo_local.py

sim-voice:
	$(MODAL) uv run python -m slate.voice firmware "$(INPUT)"

device-code-check:
	$(MODAL) uv run python scripts/check_device_code.py

profile:
	$(MODAL) PYTHONPATH=scripts uv run python -u -m scripts.profile_$(or $(WHAT),voice) $(ARGS)

eval:
	doppler run -- uv run python scripts/eval_agent.py $(or $(ARGS),longmemeval --limit 3 --grade)
