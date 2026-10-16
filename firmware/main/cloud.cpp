#include <Arduino.h>
#include <ArduinoJson.h>
#include <Network.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <atomic>
#include <time.h>
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "cloud.h"
#include "cloud_ca.h"
#include "device_command.h"
#include "mic.h"
#include "perf.h"

struct CloudEvent {
  SlateState state;
  bool toggle_live;
  uint16_t count;
  int16_t samples[MIC_FRAME_SAMPLES];
};

struct Provision {
  char line[768];
};

class CloudSocket : public WebSocketsClient {
 public:
  void poll() {
    unsigned long previous_failure = _lastConnectionFail;
    bool was_disconnected = _client.status == WSC_NOT_CONNECTED;
    WebSocketsClient::loop();
    if (was_disconnected && _lastConnectionFail && _lastConnectionFail != previous_failure) {
      uint8_t reason[] = "transport connect failed";
      runCbEvent(WStype_DISCONNECTED, reason, sizeof(reason) - 1);
    }
    if (_client.isSSL && _client.tcp && _client.tcp->connected() && !tls_reported) {
      Serial.println("slate.cloud: TLS verified");
      tls_reported = true;
    }
    if (!_client.tcp || !_client.tcp->connected()) tls_reported = false;
  }
 private:
  bool tls_reported = false;
};

static CloudSocket socket;
static Preferences preferences;
static bool preferences_open;
static QueueHandle_t events, provisions;
static std::atomic<bool> ready{false};
static std::atomic<bool> network_up{false};
static std::atomic<bool> use_ethernet{false};
static esp_eth_handle_t ethernet;
static esp_netif_t* ethernet_netif;
static bool ethernet_started = false;
static String mode, ssid, password, url, token;
static bool configured, connected, active_turn, recording;
static String turn_id;
static std::atomic<bool> live{false}, audio_tap{false};
static unsigned long retry_ms = 1000;
static unsigned long finish_at, error_at;
static size_t reply_bytes;

static void send_json(JsonDocument& document) {
  String json;
  serializeJson(document, json);
  if (!socket.sendTXT(json)) Serial.println("slate.cloud: text send failed");
}

static void send_type(const char* type) {
  JsonDocument document;
  document["type"] = type;
  send_json(document);
}

static void finish_reply() {
  Serial.printf("slate.reply.audio: %u bytes\n", unsigned(reply_bytes));
  reply_bytes = 0;
  finish_at = 0;
  active_turn = false;
  turn_id = "";
  slate_request_state(IDLE);
}

static void receive_text(uint8_t* payload, size_t length) {
  if (length > 8192) {
    Serial.println("slate.cloud.error: text frame too large");
    return;
  }
  JsonDocument document;
  if (deserializeJson(document, payload, length)) {
    Serial.println("slate.cloud.error: invalid JSON");
    return;
  }
  const char* type = document["type"] | "";
  if (!strcmp(type, "command")) {
    JsonDocument receipt;
    device_command(receipt, document["request_id"] | "", document["operation"] | "",
                   document["arguments"].as<JsonVariantConst>());
    send_json(receipt);
    return;
  }
  if (!strcmp(type, "live")) {
    const char* state = document["state"] | "";
    if (!strcmp(state, "started")) {
      Serial.printf("slate.live: started %s\n", document["call_id"] | "");
    } else if (!strcmp(state, "ended")) {
      String line = "slate.live: ended " + String(document["seconds"] | 0.0) + "s";
      const char* error = document["error"] | "";
      if (*error) line += " error=" + String(error);
      Serial.println(line);
      live.store(false);
      slate_request_state(IDLE);
    }
    return;
  }
  if (!strcmp(type, "heard") || !strcmp(type, "said")) {
    Serial.printf("slate.live.%s: %s\n", type, document["text"] | "");
    return;
  }
  if (live.load()) {
    if (!strcmp(type, "error")) {
      Serial.printf("slate.cloud.error: %s\n", document["message"] | "unknown error");
      return;
    }
    if (!strcmp(type, "reply") || !strcmp(type, "transcript") ||
        !strcmp(type, "turn") || !strcmp(type, "cancelled")) return;
  }
  const char* incoming_turn = document["turn_id"] | "";
  if (!strcmp(type, "turn")) {
    if (active_turn) turn_id = incoming_turn;
    return;
  }
  bool announcement = document["announcement"] | false;
  bool global_error = !strcmp(type, "error") && !incoming_turn[0];
  if (!live.load() && !announcement && !global_error && (!active_turn || (turn_id.length() && turn_id != incoming_turn))) {
    Serial.println("slate.cloud: ignored inactive turn");
    return;
  }
  if (!strcmp(type, "transcript")) {
    if (document["final"] | false) Serial.printf("slate.transcript: %s\n", document["text"] | "");
  } else if (!strcmp(type, "reply")) {
    active_turn = true;
    slate_request_state(SLATE_RESPOND);
    if (document["final"] | false) {
      Serial.printf("slate.reply: %s\n", document["text"] | "");
      finish_at = millis() + 1000;
    }
  } else if (!strcmp(type, "tool")) {
    Serial.printf("slate.cloud.tool: %s\n", document["tool"] | "");
  } else if (!strcmp(type, "approval")) {
    Serial.println("slate.cloud.approval: requested");
  } else if (!strcmp(type, "error")) {
    Serial.printf("slate.cloud.error: %s\n", document["message"] | "unknown error");
    active_turn = recording = false;
    finish_at = 0;
    slate_request_state(SLATE_ERROR);
    error_at = millis() + 1000;
  } else if (!strcmp(type, "cancelled")) {
    active_turn = false;
    finish_at = 0;
    reply_bytes = 0;
    slate_request_state(IDLE);
  }
}

static void socket_event(WStype_t type, uint8_t* payload, size_t length) {
  if (type == WStype_CONNECTED) {
    connected = true;
    ready.store(true);
    retry_ms = 1000;
    socket.setReconnectInterval(retry_ms);
    Serial.println("slate.cloud: connected");
    JsonDocument hello;
    hello["type"] = "hello";
    hello["mcu"] = "esp32-wroom-32";
    hello["rate"] = MIC_SAMPLE_HZ;
    send_json(hello);
    if (slate_get_state() == SLATE_LISTEN) {
      active_turn = recording = true;
      send_type("start");
    }
  } else if (type == WStype_DISCONNECTED) {
    ready.store(false);
    bool interrupted = active_turn || live.load();
    if (live.exchange(false)) Serial.println("slate.live: ended disconnected");
    connected = active_turn = recording = false;
    turn_id = "";
    finish_at = 0;
    reply_bytes = 0;
    Serial.printf("slate.cloud: disconnected %.*s\n", int(length), payload ? reinterpret_cast<char*>(payload) : "");
    socket.setReconnectInterval(retry_ms);
    retry_ms = min(retry_ms * 2, 30000UL);
    if (interrupted) slate_request_state(IDLE);
  } else if (type == WStype_TEXT) {
    receive_text(payload, length);
  } else if (type == WStype_BIN) {
    cloud_tap(1, payload, length);
    if (active_turn || slate_get_state() == SLATE_RESPOND) {
      reply_bytes += length;
      if (finish_at) finish_at = millis() + 1000;
    }
  } else if (type == WStype_ERROR) {
    Serial.println("slate.cloud.error: websocket transport error");
  }
}

static void ethernet_event(void*, esp_event_base_t base, int32_t event, void* data) {
  if (!use_ethernet.load()) return;
  if (base == IP_EVENT && event == IP_EVENT_ETH_GOT_IP) {
    auto* address = static_cast<ip_event_got_ip_t*>(data);
    network_up.store(true);
    Serial.printf("slate.net: up " IPSTR "\n", IP2STR(&address->ip_info.ip));
  } else if (base == ETH_EVENT && (event == ETHERNET_EVENT_DISCONNECTED || event == ETHERNET_EVENT_STOP)) {
    network_up.store(false);
    Serial.println("slate.net: down");
  }
}

static bool start_ethernet() {
  if (!ethernet) {
    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    ethernet_netif = esp_netif_new(&netif_config);
    eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
    mac_config.flags |= ETH_MAC_FLAG_PIN_TO_CORE;
    eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
    phy_config.phy_addr = 1;
    phy_config.reset_gpio_num = -1;
    esp_eth_mac_t* mac = esp_eth_mac_new_openeth(&mac_config);
    esp_eth_phy_t* phy = esp_eth_phy_new_dp83848(&phy_config);
    if (!ethernet_netif || !mac || !phy) {
      Serial.println("slate.net: ethernet allocation failed");
      if (mac) mac->del(mac);
      if (phy) phy->del(phy);
      if (ethernet_netif) esp_netif_destroy(ethernet_netif);
      ethernet_netif = nullptr;
      return false;
    }
    esp_eth_config_t config = ETH_DEFAULT_CONFIG(mac, phy);
    esp_err_t result = esp_eth_driver_install(&config, &ethernet);
    if (result != ESP_OK) {
      Serial.printf("slate.net: ethernet install failed %s\n", esp_err_to_name(result));
      mac->del(mac);
      phy->del(phy);
      esp_netif_destroy(ethernet_netif);
      ethernet_netif = nullptr;
      return false;
    }
    ESP_ERROR_CHECK(esp_netif_attach(ethernet_netif, esp_eth_new_netif_glue(ethernet)));
    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, ethernet_event, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, ethernet_event, nullptr));
  }
  esp_err_t result = esp_eth_start(ethernet);
  ethernet_started = result == ESP_OK;
  if (!ethernet_started) Serial.printf("slate.net: ethernet start failed %s\n", esp_err_to_name(result));
  return ethernet_started;
}

static void connect_network() {
  use_ethernet.store(mode == "eth");
  socket.disconnect();
  configured = false;
  ready.store(false);
  network_up.store(false);
  if (WiFi.getMode() != WIFI_OFF) WiFi.disconnect(true);
  if (ethernet_started) {
    esp_eth_stop(ethernet);
    ethernet_started = false;
  }
  Serial.println("slate.net: down");
  if (mode == "eth") {
    start_ethernet();
  } else if (mode == "wifi" && ssid.length()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(ssid.c_str(), password.c_str());
  }
}

static char* provision_field(char*& cursor) {
  while (*cursor == ' ') ++cursor;
  if (!*cursor) return nullptr;
  char quote = *cursor == '\"' || *cursor == '\'' ? *cursor++ : 0;
  char* field = cursor;
  if (quote) {
    char* end = strchr(cursor, quote);
    if (!end) {
      cursor += strlen(cursor);
      return nullptr;
    }
    cursor = end;
  } else {
    cursor = strchr(cursor, ' ');
    if (!cursor) {
      cursor = field + strlen(field);
      return field;
    }
  }
  *cursor++ = 0;
  return field;
}

static bool save_preference(const char* key, const String& value) {
  if (!preferences_open) preferences_open = preferences.begin("slate-cloud", false);
  if (!preferences_open) {
    Serial.println("slate.net: provisioning storage unavailable");
    return false;
  }
  if (preferences.putString(key, value) == value.length()) return true;
  Serial.printf("slate.net: provisioning save failed %s\n", key);
  return false;
}

static void provision(char* line) {
  if (!strcmp(line, "net eth")) {
    if (mode == "eth" && ethernet_started) return;
    mode = "eth";
    if (!save_preference("mode", mode)) return;
    connect_network();
  } else if (!strncmp(line, "net wifi ", 9)) {
    char* cursor = line + 9;
    char* network = provision_field(cursor);
    char* secret = provision_field(cursor);
    while (*cursor == ' ') ++cursor;
    if (!network || !*network || !secret || *cursor || strlen(network) > 32 || strlen(secret) > 64) {
      Serial.println("slate.net: invalid wifi provisioning");
      return;
    }
    ssid = network;
    password = secret;
    mode = "wifi";
    if (!save_preference("mode", mode)) return;
    if (!save_preference("ssid", ssid)) return;
    if (!save_preference("password", password)) return;
    connect_network();
  } else if (!strncmp(line, "cloud ", 6)) {
    char* secret = strchr(line + 6, ' ');
    if (!secret || !(String(line + 6).startsWith("ws://") || String(line + 6).startsWith("wss://"))) {
      Serial.println("slate.cloud: invalid provisioning");
      return;
    }
    *secret++ = 0;
    url = line + 6;
    token = secret;
    if (!url.length() || !token.length() || token.indexOf('\r') >= 0 || token.indexOf('\n') >= 0) {
      Serial.println("slate.cloud: invalid provisioning");
      return;
    }
    if (!save_preference("url", url)) return;
    if (!save_preference("token", token)) return;
    socket.disconnect();
    ready.store(false);
    configured = false;
    retry_ms = 1000;
    Serial.println("slate.cloud: provisioned");
  } else {
    Serial.println("slate.net: invalid provisioning command");
  }
}

static bool configure_socket() {
  bool secure = url.startsWith("wss://");
  size_t offset = secure ? 6 : 5;
  int slash = url.indexOf('/', offset);
  String host = slash < 0 ? url.substring(offset) : url.substring(offset, slash);
  String path = slash < 0 ? "/" : url.substring(slash);
  int colon = host.indexOf(':');
  uint16_t port = secure ? 443 : 80;
  if (colon >= 0) {
    int number = host.substring(colon + 1).toInt();
    if (number <= 0 || number > 65535) return false;
    port = number;
    host = host.substring(0, colon);
  }
  if (!host.length()) return false;
  if (secure) socket.beginSslWithCA(host.c_str(), port, path.c_str(), CLOUD_CA, "");
  else socket.begin(host.c_str(), port, path.c_str(), "");
  socket.setExtraHeaders("");
  socket.setAuthorization(("Bearer " + token).c_str());
  socket.setReconnectInterval(retry_ms);
  socket.enableHeartbeat(15000, 3000, 2);
  return true;
}

static void cloud_task(void* starter) {
  if (!Network.begin()) {
    Serial.println("slate.net: stack initialization failed");
    xTaskNotifyGive(static_cast<TaskHandle_t>(starter));
    vTaskDelete(nullptr);
  }
  nvs_handle_t stored;
  esp_err_t result = nvs_open("slate-cloud", NVS_READONLY, &stored);
  if (result == ESP_OK) {
    nvs_close(stored);
    preferences_open = preferences.begin("slate-cloud", false);
    if (preferences_open) {
      mode = preferences.isKey("mode") ? preferences.getString("mode") : "";
      ssid = preferences.isKey("ssid") ? preferences.getString("ssid") : "";
      password = preferences.isKey("password") ? preferences.getString("password") : "";
      url = preferences.isKey("url") ? preferences.getString("url") : "";
      token = preferences.isKey("token") ? preferences.getString("token") : "";
    }
  } else if (result != ESP_ERR_NVS_NOT_FOUND) {
    Serial.printf("slate.net: provisioning load failed %s\n", esp_err_to_name(result));
  }
  WiFi.onEvent([](arduino_event_id_t event) {
    if (use_ethernet.load()) return;
    if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
      network_up.store(true);
      Serial.printf("slate.net: up %s\n", WiFi.localIP().toString().c_str());
    } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      network_up.store(false);
      Serial.println("slate.net: down");
    }
  });
  socket.onEvent(socket_event);
  connect_network();
  xTaskNotifyGive(static_cast<TaskHandle_t>(starter));
  Provision command;
  CloudEvent event;
  bool timed = false;
  for (;;) {
    while (xQueueReceive(provisions, &command, 0) == pdTRUE) {
      provision(command.line);
      memset(&command, 0, sizeof(command));
    }
    if (network_up.load()) {
      if (!timed) {
        configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
        timed = true;
      }
      if (url.length() && token.length() && !configured) {
        configured = configure_socket();
        if (!configured) Serial.println("slate.cloud: invalid URL");
      }
      if (configured) socket.poll();
    } else if (connected) {
      socket.disconnect();
    }
    while (xQueueReceive(events, &event, 0) == pdTRUE) {
      if (!connected) continue;
      if (event.toggle_live) {
        if (live.exchange(!live.load())) {
          send_type("hangup");
          slate_request_state(IDLE);
          Serial.println("slate.live: hangup");
        } else {
          if (active_turn) send_type("cancel");
          active_turn = recording = false;
          turn_id = "";
          finish_at = error_at = 0;
          reply_bytes = 0;
          slate_request_state(SLATE_LISTEN);
          send_type("live");
          Serial.println("slate.live: requested");
        }
      } else if (live.load() && !event.count) {
        if (event.state == IDLE) {
          send_type("hangup");
          live.store(false);
          slate_request_state(IDLE);
          Serial.println("slate.live: hangup");
        }
      } else if (event.count) {
        if ((recording || live.load()) && !socket.sendBIN(reinterpret_cast<uint8_t*>(event.samples), event.count * sizeof(int16_t))) {
          Serial.println("slate.cloud: audio send failed");
        }
      } else if (event.state == SLATE_LISTEN) {
        finish_at = error_at = 0;
        reply_bytes = 0;
        turn_id = "";
        active_turn = recording = true;
        send_type("start");
      } else if (event.state == SLATE_TRANSCRIBE && recording) {
        recording = false;
        send_type("end");
      } else if (event.state == IDLE && active_turn) {
        send_type("cancel");
        active_turn = recording = false;
        turn_id = "";
        finish_at = 0;
        reply_bytes = 0;
      }
    }
    if (finish_at && int32_t(millis() - finish_at) >= 0) finish_reply();
    if (error_at && int32_t(millis() - error_at) >= 0) {
      error_at = 0;
      slate_request_state(IDLE);
    }
    perf_stack(PerfTask::CLOUD);
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

void cloud_start() {
  events = xQueueCreate(10, sizeof(CloudEvent));
  provisions = xQueueCreate(2, sizeof(Provision));
  configASSERT(events && provisions);
  configASSERT(xTaskCreatePinnedToCore(cloud_task, "Cloud", 8192, xTaskGetCurrentTaskHandle(), 1, nullptr, 0) == pdPASS);
  ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
}

void cloud_provision(const char* line) {
  Provision command = {};
  if (strlen(line) >= sizeof(command.line)) {
    Serial.println("slate.net: provisioning command too long");
    return;
  }
  strcpy(command.line, line);
  if (xQueueSend(provisions, &command, 0) != pdTRUE) Serial.println("slate.net: provisioning queue full");
}

void cloud_state(SlateState state) {
  if (!events || !ready.load()) return;
  CloudEvent event = {};
  event.state = state;
  if (xQueueSend(events, &event, 0) != pdTRUE) Serial.println("slate.cloud: state queue full");
}

void cloud_audio(const int16_t* samples, size_t count) {
  if (!ready.load() || (!live.load() && slate_get_state() != SLATE_LISTEN) || !count || count > MIC_FRAME_SAMPLES) return;
  if (uxQueueSpacesAvailable(events) < 3) {
    static uint32_t last_drop;
    if (millis() - last_drop >= 1000) {
      Serial.println("slate.cloud: microphone backpressure");
      last_drop = millis();
    }
    return;
  }
  CloudEvent event = {};
  event.count = count;
  memcpy(event.samples, samples, count * sizeof(int16_t));
  if (xQueueSend(events, &event, 0) != pdTRUE) Serial.println("slate.cloud: audio queue full");
}

void cloud_toggle_live() {
  if (!ready.load()) {
    Serial.println("slate.live: cloud disconnected");
    return;
  }
  CloudEvent event = {};
  event.toggle_live = true;
  if (xQueueSend(events, &event, 0) != pdTRUE) Serial.println("slate.live: control queue full");
}

bool cloud_live() { return live.load(); }

void cloud_audio_tap(bool enabled) { audio_tap.store(enabled); }

void cloud_tap(uint8_t marker, const uint8_t* payload, size_t length) {
  // One buffer per marker: the loop task taps the mic (0), the cloud task the speaker (1).
  static uint8_t frames[2][3 + 2048];
  if (!audio_tap.load() || marker > 1) return;
  if (length > sizeof(frames[0]) - 3) {
    Serial.println("slate.audio: tap frame too large");
    return;
  }
  uint8_t* frame = frames[marker];
  frame[0] = marker;
  frame[1] = length;
  frame[2] = length >> 8;
  memcpy(frame + 3, payload, length);
  Serial.write(frame, length + 3);
}
