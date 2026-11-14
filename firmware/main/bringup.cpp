#ifdef SLATE_BRINGUP
#include <Arduino.h>
#include <Network.h>
#include <atomic>
#include <cmath>
#include <cstring>
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/queue.h"
#include "board.h"
#include "ble_setup.h"
#include "display.h"
#include "mic.h"
#include "pdm.h"
#include "speaker.h"
#include "oled.h"

// Local state controller: no cloud, power, I2C, haptics or SD initialization.
static SlateState state = IDLE;
void slate_start() {}
SlateState slate_get_state() { return state; }
bool slate_request_state(SlateState next) {
  state = next;
  display_set_state(next);
  mic_set_state(next);
  return true;
}
static bool microphone, screen;
static bool continuous_demo;
static uint32_t last_tone;
static int16_t demo_tone[SPEAKER_RATE / 5];
static uint32_t deadline, last_report;
static uint64_t energy;
static uint32_t samples, peak;
static float min_rms = INFINITY, max_rms;

// The IDF event task only copies observations; the bring-up loop owns state and
// serial output. No driver or network initialization occurs before a command.
struct WifiEvent {
  enum Kind { SCAN, DISCONNECTED, GOT_IP, LOST_IP, STOPPED } kind;
  uint16_t reason;
  esp_ip4_addr_t ip;
};
static QueueHandle_t wifi_events;
static std::atomic<bool> wifi_event_overflow{false};
static esp_netif_t* wifi_netif;
static bool wifi_initialized, wifi_enabled, wifi_stopping;
static bool wifi_scanning, wifi_connecting, wifi_has_ip;
static uint32_t wifi_deadline;
static uint16_t wifi_last_reason;
struct BleCommand { char line[241]; };
static QueueHandle_t ble_commands;
static uint32_t ble_pings;
static bool mcu_heartbeat; // Quiet after the initial boot report; enable on demand.
static void mcu();

static void wipe(void* data, size_t length) {
  volatile uint8_t* bytes = static_cast<volatile uint8_t*>(data);
  while (length--) *bytes++ = 0;
}
static bool wifi_ok(esp_err_t error, const char* operation) {
  if (error == ESP_OK) return true;
  Serial.printf("slate.wifi: ERROR %s: %s (%d)\n", operation, esp_err_to_name(error), error);
  return false;
}
static const char* wifi_reason(uint16_t reason) {
  switch (reason) {
    case 0: return "none";
    case WIFI_REASON_AUTH_EXPIRE: return "authentication expired";
    case WIFI_REASON_ASSOC_LEAVE: return "station disconnected locally";
    case WIFI_REASON_BEACON_TIMEOUT: return "AP beacon timeout / signal lost";
    case WIFI_REASON_NO_AP_FOUND: return "AP not found; check SSID, range and 2.4 GHz";
    case WIFI_REASON_AUTH_FAIL: return "authentication failed; check password/security";
    case WIFI_REASON_ASSOC_FAIL: return "association rejected by AP";
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "handshake timeout; check password/signal";
    case WIFI_REASON_CONNECTION_FAIL: return "connection failed";
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY: return "no AP with compatible security";
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD: return "AP security below configured threshold";
    case WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD: return "AP signal below configured threshold";
    default: return "AP/driver reason (see numeric ESP-IDF Wi-Fi reason code)";
  }
}
static void wifi_event(void*, esp_event_base_t base, int32_t id, void* data) {
  WifiEvent event{};
  if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
    event.kind = WifiEvent::SCAN;
    event.reason = static_cast<wifi_event_sta_scan_done_t*>(data)->status;
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    event.kind = WifiEvent::DISCONNECTED;
    event.reason = static_cast<wifi_event_sta_disconnected_t*>(data)->reason;
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_STOP) {
    event.kind = WifiEvent::STOPPED;
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    event.kind = WifiEvent::GOT_IP;
    event.ip = static_cast<ip_event_got_ip_t*>(data)->ip_info.ip;
  } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
    event.kind = WifiEvent::LOST_IP;
  } else return;
  if (xQueueSend(wifi_events, &event, 0) != pdTRUE) wifi_event_overflow.store(true);
}
static bool wifi_enable() {
  if (!wifi_initialized) {
    // Network.begin supplies the shared netif/event loop, without starting Wi-Fi.
    if (!Network.begin()) {
      Serial.println("slate.wifi: ERROR network stack initialization failed");
      return false;
    }
    if (!wifi_events) wifi_events = xQueueCreate(16, sizeof(WifiEvent));
    if (!wifi_events) {
      Serial.println("slate.wifi: ERROR event queue allocation failed");
      return false;
    }
    wifi_netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    config.nvs_enable = false;
    if (!wifi_ok(esp_wifi_init(&config), "initialize")) {
      esp_netif_destroy_default_wifi(wifi_netif);
      wifi_netif = nullptr;
      return false;
    }
    esp_err_t result = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (result == ESP_OK) result = esp_wifi_set_mode(WIFI_MODE_STA);
    if (result == ESP_OK)
      result = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, nullptr);
    if (result == ESP_OK)
      result = esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, wifi_event, nullptr);
    if (!wifi_ok(result, "configure RAM-only station/events")) {
      esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event);
      esp_wifi_deinit();
      esp_netif_destroy_default_wifi(wifi_netif);
      wifi_netif = nullptr;
      return false;
    }
    wifi_initialized = true;
  }
  if (!wifi_enabled) {
    if (!wifi_ok(esp_wifi_start(), "start")) return false;
    wifi_enabled = true;
  }
  return true;
}
static void wifi_disconnect() {
  if (!wifi_enabled) {
    Serial.println(wifi_stopping ? "slate.wifi: stopping; wait before retry" : "slate.wifi: off");
    return;
  }
  if (wifi_scanning) wifi_ok(esp_wifi_scan_stop(), "stop scan");
  if (!wifi_ok(esp_wifi_stop(), "stop")) return;
  wifi_scanning = wifi_connecting = wifi_has_ip = wifi_enabled = false;
  wifi_stopping = true; // Wait for STOPPED before allowing another start.
  esp_wifi_clear_ap_list();
  wifi_config_t empty{};
  if (wifi_ok(esp_wifi_set_config(WIFI_IF_STA, &empty), "clear RAM credentials"))
    Serial.println("slate.wifi: radio off; RAM credentials cleared (driver allocation retained)");
  else Serial.println("slate.wifi: radio off; ERROR could not clear RAM credentials; reset to clear");
}
static void wifi_status() {
  Serial.printf("slate.wifi: radio=%s scan=%s connecting=%s ip=%s last_reason=%u (%s)\n",
    wifi_stopping ? "stopping" : wifi_enabled ? "on" : "off",
    wifi_scanning ? "running" : "idle", wifi_connecting ? "yes" : "no",
    wifi_has_ip ? "assigned" : "none", wifi_last_reason, wifi_reason(wifi_last_reason));
  wifi_ap_record_t ap{};
  if (wifi_enabled && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
    esp_netif_ip_info_t info{};
    esp_netif_get_ip_info(wifi_netif, &info);
    Serial.printf("slate.wifi: ssid=\"%.*s\" channel=%u rssi=%d dBm ip=" IPSTR "\n",
      32, ap.ssid, ap.primary, ap.rssi, IP2STR(&info.ip));
  }
}
static void ble_wifi_status() {
  char status[64];
  if (wifi_has_ip) {
    esp_netif_ip_info_t info{};
    esp_netif_get_ip_info(wifi_netif, &info);
    snprintf(status, sizeof(status), "net up " IPSTR, IP2STR(&info.ip));
  } else if (wifi_connecting) {
    strlcpy(status, "net connecting", sizeof(status));
  } else {
    snprintf(status, sizeof(status), "net %s reason=%u", wifi_enabled ? "down" : "off", wifi_last_reason);
  }
  ble_setup_status(status);
}
static void wifi_poll() {
  if (!wifi_events) return;
  WifiEvent event;
  while (xQueueReceive(wifi_events, &event, 0) == pdTRUE) {
    if (event.kind == WifiEvent::STOPPED) { wifi_stopping = false; continue; }
    if (!wifi_enabled) continue;
    if (event.kind == WifiEvent::SCAN && wifi_scanning) {
      wifi_scanning = false;
      if (event.reason) {
        Serial.printf("slate.wifi: ERROR scan status=%u\n", event.reason);
        esp_wifi_clear_ap_list();
        continue;
      }
      // Bound memory and serial work even in a crowded environment.
      wifi_ap_record_t records[24];
      uint16_t total = 0, count = 24;
      esp_wifi_scan_get_ap_num(&total);
      if (!wifi_ok(esp_wifi_scan_get_ap_records(&count, records), "read scan")) {
        esp_wifi_clear_ap_list();
        continue;
      }
      Serial.printf("slate.wifi: scan complete; %u networks, showing %u (strongest first)\n", total, count);
      for (unsigned i = 0; i < count; ++i)
        Serial.printf("slate.wifi: %u ssid=\"%.*s\" rssi=%d dBm channel=%u auth=%u\n",
          i + 1, 32, records[i].ssid, records[i].rssi, records[i].primary, records[i].authmode);
    } else if (event.kind == WifiEvent::DISCONNECTED) {
      wifi_has_ip = wifi_connecting = false;
      wifi_last_reason = event.reason;
      Serial.printf("slate.wifi: disconnected reason=%u (%s); no automatic retry\n",
        event.reason, wifi_reason(event.reason));
      ble_wifi_status();
    } else if (event.kind == WifiEvent::GOT_IP) {
      wifi_has_ip = true;
      wifi_connecting = false;
      Serial.printf("slate.wifi: connected; assigned IP=" IPSTR "\n", IP2STR(&event.ip));
      wifi_status();
      ble_wifi_status();
    } else if (event.kind == WifiEvent::LOST_IP) {
      wifi_has_ip = false;
      Serial.println("slate.wifi: IP lost; run !wifi disconnect then !wifi connect to retry");
      ble_wifi_status();
    }
  }
  if (wifi_event_overflow.exchange(false)) {
    Serial.println("slate.wifi: ERROR event queue overflow; stopping radio, retry command");
    bool already_stopped = !wifi_enabled;
    wifi_disconnect();
    // Only bypass the STOPPED barrier if that event was previously dropped.
    // A new stop above has queue space and must complete before another start.
    if (already_stopped) wifi_stopping = false;
  }
  if ((wifi_scanning || wifi_connecting) && int32_t(millis() - wifi_deadline) >= 0) {
    Serial.println(wifi_scanning ? "slate.wifi: ERROR scan timed out (15 seconds)" :
                                  "slate.wifi: ERROR connect/DHCP timed out (30 seconds)");
    wifi_disconnect();
  }
}

// Quoted fields support spaces and escaped quotes/backslashes. Parsed in place
// and wiped after dispatch; credentials are never printed or stored in NVS.
static char* wifi_field(char*& cursor, bool& valid) {
  while (*cursor == ' ' || *cursor == '\t') ++cursor;
  if (!*cursor) return nullptr;
  char quote = (*cursor == '"' || *cursor == '\'') ? *cursor++ : 0;
  char* start = cursor;
  char* out = cursor;
  while (*cursor) {
    char byte = *cursor++;
    if (quote && byte == quote) {
      if (*cursor && *cursor != ' ' && *cursor != '\t') { valid = false; return nullptr; }
      *out = 0;
      return start;
    }
    if (!quote && (byte == ' ' || byte == '\t')) { *out = 0; return start; }
    if (byte == '\\' && (*cursor == '\\' || (quote && *cursor == quote))) byte = *cursor++;
    *out++ = byte;
  }
  if (quote) { valid = false; return nullptr; }
  *out = 0;
  return start;
}
static void wifi_command(char* line) {
  char* cursor = line;
  bool valid = true;
  char* prefix = wifi_field(cursor, valid);
  char* command = wifi_field(cursor, valid);
  char* ssid = wifi_field(cursor, valid);
  char* password = wifi_field(cursor, valid);
  while (*cursor == ' ' || *cursor == '\t') ++cursor;
  if (!valid || !prefix || strcmp(prefix, "wifi") || !command) {
    Serial.println("slate.wifi: use !wifi scan|connect \"SSID\" \"PASSWORD\"|status|disconnect");
    return;
  }
  if (!strcmp(command, "connect")) {
    if (!ssid || !*ssid || strlen(ssid) > 32 || !password || strlen(password) > 64 || *cursor) {
      Serial.println("slate.wifi: ERROR use !wifi connect \"SSID\" \"PASSWORD\" (SSID 1-32 bytes, password 0-64 bytes)");
      return;
    }
    if (wifi_stopping || wifi_scanning || wifi_connecting || wifi_has_ip) {
      Serial.println("slate.wifi: busy/connected; use !wifi disconnect and wait, then retry");
      return;
    }
    if (!wifi_enable()) return;
    wifi_config_t config{};
    memcpy(config.sta.ssid, ssid, strlen(ssid));
    memcpy(config.sta.password, password, strlen(password));
    config.sta.threshold.authmode = *password ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    esp_err_t result = esp_wifi_set_config(WIFI_IF_STA, &config);
    wipe(&config, sizeof(config));
    if (!wifi_ok(result, "set RAM credentials")) return;
    if (!wifi_ok(esp_wifi_connect(), "connect")) return;
    wifi_connecting = true;
    wifi_last_reason = 0;
    wifi_deadline = millis() + 30000;
    Serial.println("slate.wifi: connecting (30-second limit; no automatic retry)");
  } else if (ssid || password || *cursor) {
    Serial.println("slate.wifi: ERROR unexpected arguments");
  } else if (!strcmp(command, "status")) {
    wifi_status();
  } else if (!strcmp(command, "disconnect")) {
    wifi_disconnect();
  } else if (!strcmp(command, "scan")) {
    if (wifi_stopping || wifi_scanning || wifi_connecting) {
      Serial.println("slate.wifi: busy; wait or use !wifi disconnect");
      return;
    }
    if (!wifi_enable()) return;
    wifi_scan_config_t config{};
    config.show_hidden = true;
    if (!wifi_ok(esp_wifi_scan_start(&config, false), "start scan")) return;
    wifi_scanning = true;
    wifi_deadline = millis() + 15000;
    Serial.println("slate.wifi: scanning asynchronously (15-second limit)");
  } else {
    Serial.println("slate.wifi: use !wifi scan|connect \"SSID\" \"PASSWORD\"|status|disconnect");
  }
}

// GATT writes run in NimBLE's host task. Only enqueue here; the bring-up loop
// owns Wi-Fi and serial state, so phone writes never block the BLE host.
static bool ble_receive(const char* line) {
  BleCommand command{};
  strlcpy(command.line, line, sizeof(command.line));
  bool accepted = ble_commands && xQueueSend(ble_commands, &command, 0) == pdTRUE;
  wipe(&command, sizeof(command));
  return accepted;
}
static void ble_poll() {
  if (!ble_commands) return;
  BleCommand command;
  // One command per iteration keeps mic and serial service responsive.
  if (xQueueReceive(ble_commands, &command, 0) != pdTRUE) return;
  if (ble_setup_active()) {
    if (!strcmp(command.line, "ping")) {
      ++ble_pings;
      ble_setup_status("pong");
      Serial.printf("slate.ble: ping received; pong sent/readable; count=%lu\n", static_cast<unsigned long>(ble_pings));
    } else if (!strcmp(command.line, "status")) {
      ble_wifi_status();
    } else if (!strncmp(command.line, "net wifi ", 9)) {
      char line[256];
      snprintf(line, sizeof(line), "wifi connect %s", command.line + 9);
      wifi_command(line);
      wipe(line, sizeof(line));
      ble_wifi_status();
    } else {
      ble_setup_status("error command; use ping, status, net wifi (RAM only)");
      Serial.println("slate.ble: bring-up accepts ping, status, net wifi; cloud/saved provisioning requires normal firmware");
    }
  }
  wipe(&command, sizeof(command));
}
static void bringup_command(char* line) {
  if (!strcmp(line, "mcu off")) {
    mcu_heartbeat = false;
    Serial.println("slate.test: MCU heartbeat off");
    return;
  }
  if (!strcmp(line, "mcu on")) {
    mcu_heartbeat = true;
    Serial.println("slate.test: MCU heartbeat on (every 2 seconds)");
    return;
  }
  if (!strcmp(line, "mcu status")) { mcu(); return; }
  // Leave the established Wi-Fi parser and its quoted credentials intact.
  if (strncmp(line, "ble", 3) || (line[3] && line[3] != ' ' && line[3] != '\t')) {
    wifi_command(line);
    return;
  }
  char* cursor = line;
  bool valid = true;
  wifi_field(cursor, valid);
  char* command = wifi_field(cursor, valid);
  char* extra = wifi_field(cursor, valid);
  if (!valid || !command || extra) {
    Serial.println("slate.ble: use !ble start | !ble status | !ble stop");
    return;
  }
  if (!strcmp(command, "start")) {
    if (!ble_commands) ble_commands = xQueueCreate(4, sizeof(BleCommand));
    if (!ble_commands) { Serial.println("slate.ble: ERROR command queue allocation failed"); return; }
    ble_setup_start(ble_receive);
    ble_setup_report();
  } else if (!strcmp(command, "status")) {
    ble_setup_report();
    Serial.printf("slate.ble: test pings=%lu; provisioning is RAM-only in bring-up\n", static_cast<unsigned long>(ble_pings));
  } else if (!strcmp(command, "stop")) {
    ble_setup_finish();
    // Drain and wipe pending credentials, rather than executing after stop.
    BleCommand pending;
    while (ble_commands && xQueueReceive(ble_commands, &pending, 0) == pdTRUE) wipe(&pending, sizeof(pending));
    ble_setup_report();
  } else Serial.println("slate.ble: use !ble start | !ble status | !ble stop");
}

static void mcu() {
  Serial.printf("slate.mcu: chip=%s rev=%u cores=%u flash=%u psram=%u free_heap=%u free_psram=%u\n",
    ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
    ESP.getFlashChipSize(), ESP.getPsramSize(), ESP.getFreeHeap(), ESP.getFreePsram());
  if (ESP.getFlashChipSize() != 16 * 1024 * 1024 || ESP.getPsramSize() != 8 * 1024 * 1024)
    Serial.println("slate.mcu: ERROR expected 16 MB flash and 8 MB initialized PSRAM");
}
static bool stop() {
  continuous_demo = false;
  bool ok = true;
  uint32_t started = millis();
  while (!pdm_stop()) {
    if (millis() - started > 2000) { ok = false; break; }
    delay(10);
  }
  if (screen) {
    started = millis();
    while (!oled_stop()) {
      if (millis() - started > 2000) { ok = false; break; }
      delay(10);
    }
  }
  ok = speaker_stop() && ok;
  if (ok) {
    if (screen) display_set_state(IDLE);
    microphone = screen = false;
    mic_set_state(IDLE);
    deadline = 0;
  }
  Serial.printf("slate.test: cleanup %s\n", ok ? "complete" : "ERROR timed out; reset before retry");
  return ok;
}
static void run(char key) {
  bool continuous = key == 'd';
  if (continuous) key = 'c';
  if (!stop()) return;
  energy = samples = peak = 0;
  min_rms = INFINITY; max_rms = 0;
  if (key == 'm') { mcu(); return; }
  if (key == 'x') return;
  if (key == 'o' || key == 'c') {
    display_set_state(IDLE);
    oled_start();
    screen = true;
  }
  if (key == 'i' || key == 'c') {
    mic_configure(MicChannel::LEFT);
    mic_set_state(SLATE_LISTEN);
    if (screen) display_set_state(SLATE_LISTEN);
    pdm_start();
    microphone = pdm_ready();
    Serial.printf("slate.test: I2S0 PDM 16000 Hz left %s\n", microphone ? "initialized" : "ERROR");
  }
  if (key == 's' || key == 'c') {
    speaker_start();
    Serial.printf("slate.test: I2S1 Philips stereo 16000 Hz %s\n", speaker_ready() ? "initialized" : "ERROR");
    if (speaker_ready()) speaker_test();
    // Combined mode retains I2S1 while capture and display continue.
    if (key == 's') { delay(50); stop(); return; }
  }
  deadline = continuous ? 0 : millis() + 10000;
  continuous_demo = continuous;
  last_tone = millis();
  last_report = millis();
  Serial.println(continuous ? "slate.test: continuous demo; x stops" : "slate.test: running for 10 seconds; x stops early");
}
void setup() {
  Serial.begin(921600);
  delay(1500);
  mic_start();
  for (unsigned i = 0; i < SPEAKER_RATE / 5; ++i)
    demo_tone[i] = int16_t(2000 * sinf(2.0f * PI * 440 * i / SPEAKER_RATE));
  display_set_state(IDLE);
  mcu();
  Serial.println("slate.test: m=MCU i=microphone s=speaker o=OLED c=combined d=continuous demo x=stop");
  Serial.println("slate.test: MCU heartbeat off; !mcu on | !mcu off | !mcu status (Enter required)");
  Serial.println("slate.wifi: off; commands (Enter required): !wifi scan | !wifi connect \"SSID\" \"PASSWORD\" | !wifi status | !wifi disconnect");
  Serial.println("slate.ble: off; commands (Enter required): !ble start | !ble status | !ble stop; after stop reset before start");
  Serial.println("slate.test: AMP_SD releases to external bias; verify shutdown wiring before speaker test");
}
void loop() {
  wifi_poll();
  ble_poll();
  static char command[256];
  static size_t command_length;
  static bool command_open, command_overflow;
  static uint32_t command_activity;
  // Bound serial work so a pasted line cannot starve capture or Wi-Fi polling.
  for (unsigned budget = 64; budget && Serial.available(); --budget) {
    char key = Serial.read();
    if (command_open) {
      command_activity = millis();
      if (key == '\n' || key == '\r') {
        if (!command_overflow) {
          command[command_length] = 0;
          bringup_command(command);
        } else Serial.println("slate.test: ERROR command too long or invalid; discarded");
        wipe(command, sizeof(command));
        command_length = 0;
        command_open = command_overflow = false;
      } else if (key == '\b' || key == 127) {
        if (command_length && !command_overflow) command[--command_length] = 0;
      } else if (static_cast<uint8_t>(key) < 32 && key != '\t') {
        command_overflow = true;
      } else if (!command_overflow && command_length < sizeof(command) - 1) {
        command[command_length++] = key;
      } else command_overflow = true;
    } else if (key == '!') {
      command_open = true;
      command_activity = millis();
    } else if (key == 'm' || key == 'i' || key == 's' || key == 'o' || key == 'c' || key == 'd' || key == 'x') run(key);
  }
  if (command_open && !command_overflow && millis() - command_activity >= 30000) {
    wipe(command, sizeof(command));
    command_length = 0;
    command_overflow = true; // Discard until newline; never interpret credential bytes as keys.
    Serial.println("slate.test: ERROR incomplete command timed out; send Enter to clear");
  }
  if (microphone) {
    int16_t block[MIC_FRAME_SAMPLES];
    size_t count;
    while ((count = mic_read(block, MIC_FRAME_SAMPLES))) {
      for (size_t i = 0; i < count; ++i) {
        int32_t value = block[i];
        energy += int64_t(value) * value;
        uint32_t magnitude = abs(value);
        if (magnitude > peak) peak = magnitude;
      }
      samples += count;
    }
    if (millis() - last_report >= 500) {
      float rms = samples ? sqrtf(float(energy) / samples) : 0;
      min_rms = fminf(min_rms, rms); max_rms = fmaxf(max_rms, rms);
      Serial.printf("slate.mic: %u samples rms=%.1f peak=%u rms_range=%.1f..%.1f%s\n",
        samples, rms, peak, min_rms, max_rms, samples ? "" : " ERROR no samples");
      samples = peak = 0; energy = 0; last_report = millis();
    }
  }
  if (continuous_demo && speaker_ready() && millis() - last_tone >= 3000) {
    speaker_play(reinterpret_cast<const uint8_t*>(demo_tone), sizeof(demo_tone));
    last_tone = millis();
    Serial.println("slate.test: headphone tone queued (440 Hz, 200 ms)");
  }
  if (deadline && int32_t(millis() - deadline) >= 0) {
    if (microphone) Serial.printf("slate.mic: level variation %s; speak/tap and inspect RMS, silence alone is inconclusive\n",
      max_rms - min_rms > 10 ? "observed" : "not observed");
    stop();
  }
  static uint32_t heartbeat;
  if (mcu_heartbeat && millis() - heartbeat >= 2000) { mcu(); heartbeat = millis(); }
  delay(5);
}
#endif
