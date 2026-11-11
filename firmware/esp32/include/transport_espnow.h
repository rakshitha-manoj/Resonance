#pragma once
// net::Transport over ESP-NOW broadcast (ESP32 Arduino core 2.x / IDF 4.4 callback signatures).
//   * Wi-Fi STA, not connected, fixed channel ESPNOW_CHANNEL, power save off.
//   * Receive callback runs in the Wi-Fi task: it only prefilters, copies frame + sender MAC +
//     esp_timer_get_time() into a FreeRTOS queue (never blocks), and counts overflows. The main loop
//     pops frames and calls on_packet (the state machines are not thread-safe).
//   * broadcast() returns false when esp_now_send fails; send-callback failures are counted too.
// Sender checking (allowlist / trust-on-first-use) is in espnow_logic.h (host::RxFilter).
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "node_config.h"
#include "transport.h"
#include "espnow_logic.h"

namespace net {

struct RxFrame {
    uint64_t t_us;          // esp_timer_get_time() at the receive callback
    uint8_t mac[6];         // sender MAC
    uint8_t len;
    uint8_t data[MAX_PAYLOAD];
};

class EspNowTransport : public Transport {
public:
    // Returns false (and prints why) if Wi-Fi / ESP-NOW could not be started.
    bool begin() {
        q_ = xQueueCreate(host::RX_QUEUE_DEPTH, sizeof(RxFrame));
        if (!q_) { Serial.println("espnow: queue alloc failed"); return false; }
        WiFi.persistent(false);
        WiFi.setSleep(false);  // before mode(): otherwise the core starts in modem-sleep for a moment
        WiFi.mode(WIFI_STA);
        WiFi.disconnect();
        esp_wifi_set_ps(WIFI_PS_NONE);  // modem sleep would add rx latency
        if (esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
            Serial.println("espnow: set_channel failed");
            return false;
        }
        if (esp_now_init() != ESP_OK) { Serial.println("espnow: init failed"); return false; }
        self() = this;
        esp_now_register_recv_cb(&EspNowTransport::on_recv);
        esp_now_register_send_cb(&EspNowTransport::on_sent);
        esp_now_peer_info_t peer;
        memset(&peer, 0, sizeof(peer));
        memset(peer.peer_addr, 0xFF, 6);
        peer.channel = 0;  // 0 = use the current channel
        peer.ifidx = WIFI_IF_STA;
        peer.encrypt = false;
        if (esp_now_add_peer(&peer) != ESP_OK) { Serial.println("espnow: add_peer failed"); return false; }
        uint8_t ch;
        wifi_second_chan_t sc;
        esp_wifi_get_channel(&ch, &sc);
        wifi_ps_type_t ps = WIFI_PS_MAX_MODEM;
        esp_err_t ps_err = esp_wifi_get_ps(&ps);
        Serial.printf("espnow: ready channel=%u mac=%s ps=%d%s\r\n", (unsigned)ch, WiFi.macAddress().c_str(), (int)ps,
                      (ps_err == ESP_OK && ps == WIFI_PS_NONE) ? " (none)" : " WARNING power save is ON");
        return ch == ESPNOW_CHANNEL;
    }

    bool broadcast(const uint8_t *buf, size_t len, uint64_t /*now_us*/) override {
        static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
        if (len == 0 || len > MAX_PAYLOAD) { send_fail_++; return false; }
        if (esp_now_send(bcast, buf, len) != ESP_OK) { send_fail_++; return false; }
        return true;
    }

    // Pop one frame, waiting up to wait_ticks (1 tick = 1 ms on this core). 0 = poll.
    bool pop(RxFrame *out, uint32_t wait_ticks) { return xQueueReceive(q_, out, wait_ticks) == pdTRUE; }
    uint32_t pending() const { return (uint32_t)uxQueueMessagesWaiting(q_); }

    uint32_t rx_overflow() const { return rx_overflow_; }          // queue full, frame dropped
    uint32_t rx_prefiltered() const { return rx_prefiltered_; }    // foreign / malformed, dropped in callback
    uint32_t send_fail() const { return send_fail_; }              // esp_now_send returned an error
    uint32_t send_cb_fail() const { return send_cb_fail_; }        // send callback reported failure
    uint32_t send_cb_ok() const { return send_cb_ok_; }

private:
    static EspNowTransport *&self() { static EspNowTransport *p = nullptr; return p; }

    // Wi-Fi task context: no printing, no blocking.
    static void on_recv(const uint8_t *mac, const uint8_t *data, int len) {
        EspNowTransport *t = self();
        if (!t) return;
        if (!host::prefilter(data, len) || mac == nullptr) { t->rx_prefiltered_++; return; }
        RxFrame f;
        f.t_us = (uint64_t)esp_timer_get_time();
        memcpy(f.mac, mac, 6);
        f.len = (uint8_t)len;
        memcpy(f.data, data, len);
        if (xQueueSend(t->q_, &f, 0) != pdTRUE) t->rx_overflow_++;
    }
    static void on_sent(const uint8_t * /*mac*/, esp_now_send_status_t st) {
        EspNowTransport *t = self();
        if (!t) return;
        if (st == ESP_NOW_SEND_SUCCESS) t->send_cb_ok_++;
        else t->send_cb_fail_++;
    }

    QueueHandle_t q_ = nullptr;
    volatile uint32_t rx_overflow_ = 0, rx_prefiltered_ = 0, send_cb_fail_ = 0, send_cb_ok_ = 0;
    volatile uint32_t send_fail_ = 0;
};

}  // namespace net
