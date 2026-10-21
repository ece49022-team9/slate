#include <Arduino.h>
#include <atomic>
#include <cstring>
#include "sdkconfig.h"
#ifndef SLATE_SPEAKER_TEST
#include "esp32-hal-alloc-ble-mem.h"
#endif
#if CONFIG_BT_CONTROLLER_ENABLED
#include "esp_bt.h"
#endif
#include "esp_attr.h"
#include "esp_system.h"
#include "esp_random.h"
#include "driver/uart.h"
#include "host/ble_hs.h"
#include "host/ble_sm.h"
#include "host/ble_store.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "ble_setup.h"
#include "cloud.h"
#include "display.h"

#if CONFIG_BT_CONTROLLER_DISABLED
extern "C" int __wrap_hal_uart_close(int port) {
  TaskHandle_t receiver = xTaskGetHandle("hci_uart_rx_tas");
  if (receiver) vTaskDelete(receiver);
  esp_err_t result = uart_driver_delete(static_cast<uart_port_t>(port));
  if (result != ESP_OK) Serial.printf("slate.ble: hci uart close failed %d\n", result);
  return result == ESP_OK ? 0 : -1;
}
#endif

static std::atomic<bool> running{false};
static bool released;
static RTC_NOINIT_ATTR uint32_t setup_request;
static std::atomic<uint16_t> connection{BLE_HS_CONN_HANDLE_NONE};
static std::atomic<bool> subscribed{false};
static uint16_t status_handle;
static uint8_t address_type;
static char name[11];
static char status_line[64] = "ready";
static portMUX_TYPE status_lock = portMUX_INITIALIZER_UNLOCKED;
static ble_uuid128_t service_uuid = BLE_UUID128_INIT(
    0x10, 0x4a, 0x0d, 0x1e, 0x2c, 0x8b, 0x5d, 0x9f,
    0x3e, 0x4c, 0x1b, 0x6a, 0x01, 0x00, 0x7e, 0x5a);
static ble_uuid128_t command_uuid = BLE_UUID128_INIT(
    0x10, 0x4a, 0x0d, 0x1e, 0x2c, 0x8b, 0x5d, 0x9f,
    0x3e, 0x4c, 0x1b, 0x6a, 0x02, 0x00, 0x7e, 0x5a);
static ble_uuid128_t status_uuid = BLE_UUID128_INIT(
    0x10, 0x4a, 0x0d, 0x1e, 0x2c, 0x8b, 0x5d, 0x9f,
    0x3e, 0x4c, 0x1b, 0x6a, 0x03, 0x00, 0x7e, 0x5a);

static void release_controller() {
#if CONFIG_BT_CONTROLLER_ENABLED
  esp_err_t result = esp_bt_controller_mem_release(ESP_BT_MODE_BTDM);
  if (result != ESP_OK) {
    Serial.printf("slate.ble: memory release failed %s\n", esp_err_to_name(result));
    return;
  }
  released = true;
#endif
}

static bool valid_line(const char* line, unsigned length) {
  unsigned remaining = 0, code = 0, minimum = 0;
  for (unsigned i = 0; i < length; ++i) {
    uint8_t byte = line[i];
    if (remaining) {
      if ((byte & 0xc0) != 0x80) return false;
      code = (code << 6) | (byte & 0x3f);
      if (!--remaining && (code < minimum || code > 0x10ffff ||
                          (code >= 0xd800 && code <= 0xdfff))) return false;
    } else if (byte < 0x80) {
      if (!byte || byte == '\n' || byte == '\r') return false;
    } else {
      remaining = byte >= 0xf0 && byte <= 0xf4 ? 3 : byte >= 0xe0 && byte <= 0xef ? 2
                  : byte >= 0xc2 && byte <= 0xdf ? 1 : 0;
      if (!remaining) return false;
      minimum = remaining == 3 ? 0x10000 : remaining == 2 ? 0x800 : 0x80;
      code = byte & (0x7f >> remaining);
    }
  }
  return !remaining;
}

static int access(uint16_t, uint16_t, ble_gatt_access_ctxt* context, void*) {
  if (context->op == BLE_GATT_ACCESS_OP_READ_CHR) {
    char line[sizeof(status_line)];
    portENTER_CRITICAL(&status_lock);
    memcpy(line, status_line, sizeof(line));
    portEXIT_CRITICAL(&status_lock);
    return os_mbuf_append(context->om, line, strlen(line)) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
  }
  unsigned length = OS_MBUF_PKTLEN(context->om);
  if (!length || length > 240) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  char line[241];
  if (ble_hs_mbuf_to_flat(context->om, line, length, nullptr)) return BLE_ATT_ERR_UNLIKELY;
  if (!valid_line(line, length)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  line[length] = 0;
  cloud_provision(line);
  memset(line, 0, sizeof(line));
  return 0;
}

static ble_gatt_chr_def characteristics[] = {
    {.uuid = &command_uuid.u, .access_cb = access,
     .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_WRITE_AUTHEN,
     .min_key_size = 16},
    {.uuid = &status_uuid.u, .access_cb = access,
     .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_READ_AUTHEN,
     .min_key_size = 16, .val_handle = &status_handle},
    {}};
static const ble_gatt_svc_def services[] = {
    {.type = BLE_GATT_SVC_TYPE_PRIMARY, .uuid = &service_uuid.u, .characteristics = characteristics},
    {}};

static void advertise();

static int gap_event(ble_gap_event* event, void*) {
  if (event->type == BLE_GAP_EVENT_CONNECT) {
    if (event->connect.status) {
      Serial.printf("slate.ble: connection failed %d\n", event->connect.status);
      advertise();
    } else {
      connection = event->connect.conn_handle;
      Serial.println("slate.ble: connected");
      int result = ble_gap_security_initiate(connection);
      if (result && result != BLE_HS_EALREADY) Serial.printf("slate.ble: security start failed %d\n", result);
    }
  } else if (event->type == BLE_GAP_EVENT_DISCONNECT) {
    connection = BLE_HS_CONN_HANDLE_NONE;
    subscribed.store(false);
    display_set_text("");
    Serial.println("slate.ble: disconnected");
    if (running.load()) advertise();
  } else if (event->type == BLE_GAP_EVENT_PASSKEY_ACTION) {
    if (event->passkey.params.action != BLE_SM_IOACT_DISP) return 0;
    ble_sm_io input{};
    input.action = BLE_SM_IOACT_DISP;
    input.passkey = esp_random() % 1000000;
    char text[32];
    snprintf(text, sizeof(text), "Pair code %06lu", static_cast<unsigned long>(input.passkey));
    display_set_text(text);
    Serial.printf("slate.ble: passkey %06lu\n", static_cast<unsigned long>(input.passkey));
    int result = ble_sm_inject_io(event->passkey.conn_handle, &input);
    if (result) Serial.printf("slate.ble: passkey failed %d\n", result);
  } else if (event->type == BLE_GAP_EVENT_ENC_CHANGE) {
    ble_gap_conn_desc descriptor{};
    int result = ble_gap_conn_find(event->enc_change.conn_handle, &descriptor);
    if (!event->enc_change.status && !result && descriptor.sec_state.encrypted &&
        descriptor.sec_state.authenticated) {
      Serial.println("slate.ble: paired");
      display_set_text("Paired");
    } else {
      Serial.printf("slate.ble: pairing failed %d\n", event->enc_change.status ? event->enc_change.status : result);
      ble_gap_terminate(event->enc_change.conn_handle, BLE_ERR_AUTH_FAIL);
    }
  } else if (event->type == BLE_GAP_EVENT_SUBSCRIBE) {
    if (event->subscribe.attr_handle == status_handle) subscribed.store(event->subscribe.cur_notify);
  } else if (event->type == BLE_GAP_EVENT_ADV_COMPLETE && running.load()) {
    advertise();
  }
  return 0;
}

static void advertise() {
  if (!running.load()) return;
  ble_hs_adv_fields fields{};
  fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
  fields.name = reinterpret_cast<const uint8_t*>(name);
  fields.name_len = strlen(name);
  fields.name_is_complete = 1;
  int result = ble_gap_adv_set_fields(&fields);
  ble_hs_adv_fields response{};
  response.uuids128 = &service_uuid;
  response.num_uuids128 = 1;
  response.uuids128_is_complete = 1;
  if (!result) result = ble_gap_adv_rsp_set_fields(&response);
  ble_gap_adv_params parameters{};
  parameters.conn_mode = BLE_GAP_CONN_MODE_UND;
  parameters.disc_mode = BLE_GAP_DISC_MODE_GEN;
  if (!result) result = ble_gap_adv_start(address_type, nullptr, BLE_HS_FOREVER, &parameters, gap_event, nullptr);
  if (result) Serial.printf("slate.ble: advertising failed %d\n", result);
  else Serial.printf("slate.ble: advertising %s\n", name);
}

static void synchronized() {
  uint8_t address[6];
  int result = ble_hs_id_infer_auto(0, &address_type);
  if (!result) result = ble_hs_id_copy_addr(BLE_ADDR_PUBLIC, address, nullptr);
  if (result) {
    Serial.printf("slate.ble: address failed %d\n", result);
    return;
  }
  snprintf(name, sizeof(name), "Slate-%02X%02X", address[1], address[0]);
  ble_svc_gap_device_name_set(name);
  advertise();
}

static void host_task(void*) {
  nimble_port_run();
  nimble_port_freertos_deinit();
}

void ble_setup_boot(bool requested) {
  requested = requested || setup_request == 0x5a7e0001;
  setup_request = 0;
  if (requested) ble_setup_start();
  else release_controller();
}

void ble_setup_start() {
  if (running.load()) return;
  if (released) {
    Serial.println("slate.ble: restarting for setup");
    setup_request = 0x5a7e0001;
    esp_restart();
  }
  esp_err_t result = nimble_port_init();
  if (result != ESP_OK) {
    Serial.printf("slate.ble: init failed %s\n", esp_err_to_name(result));
    return;
  }
  ble_hs_cfg.sync_cb = synchronized;
  ble_hs_cfg.sm_io_cap = BLE_HS_IO_DISPLAY_ONLY;
  ble_hs_cfg.sm_bonding = 0;
  ble_hs_cfg.sm_mitm = 1;
  ble_hs_cfg.sm_sc = 1;
  ble_hs_cfg.sm_sc_only = 1;
  ble_hs_cfg.sm_our_key_dist = 0;
  ble_hs_cfg.sm_their_key_dist = 0;
  ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
  ble_svc_gap_init();
  ble_svc_gatt_init();
  int error = ble_att_set_preferred_mtu(256);
  if (!error) error = ble_gatts_count_cfg(services);
  if (!error) error = ble_gatts_add_svcs(services);
  if (error) {
    Serial.printf("slate.ble: services failed %d\n", error);
    nimble_port_deinit();
    release_controller();
    return;
  }
  running.store(true);
  ble_setup_status("ready");
  nimble_port_freertos_init(host_task);
}

bool ble_setup_active() { return running.load(); }

void ble_setup_status(const char* line) {
  if (!running.load()) return;
  portENTER_CRITICAL(&status_lock);
  strlcpy(status_line, line, sizeof(status_line));
  portEXIT_CRITICAL(&status_lock);
  if (connection == BLE_HS_CONN_HANDLE_NONE || !subscribed.load()) return;
  ble_gap_conn_desc descriptor{};
  if (ble_gap_conn_find(connection, &descriptor) || !descriptor.sec_state.encrypted ||
      !descriptor.sec_state.authenticated) return;
  os_mbuf* value = ble_hs_mbuf_from_flat(line, strlen(line));
  int result = value ? ble_gatts_notify_custom(connection, status_handle, value) : BLE_HS_ENOMEM;
  if (result) Serial.printf("slate.ble: notify failed %d\n", result);
}

void ble_setup_finish() {
  if (!running.load()) return;
  ble_setup_status("done");
  vTaskDelay(pdMS_TO_TICKS(100));
  running.store(false);
  Serial.println("slate.ble: setup done");
  int result = nimble_port_stop();
  if (result) {
    Serial.printf("slate.ble: stop failed %d\n", result);
    running.store(true);
    return;
  }
  esp_err_t error = nimble_port_deinit();
  if (error != ESP_OK) {
    Serial.printf("slate.ble: deinit failed %s\n", esp_err_to_name(error));
    return;
  }
  connection = BLE_HS_CONN_HANDLE_NONE;
  release_controller();
  display_set_text("");
  Serial.println("slate.ble: stopped");
}
