#pragma once
// MQTT uplink of the master (ESP32 Arduino only, -DMQTT_ENABLED): Wi-Fi station + PubSubClient on a FreeRTOS task
// pinned to core 0, fed by a FreeRTOS queue of complete JSON lines. The decisions (topics, queue policy, back-off,
// meta re-publish) are in mqtt_logic.h and unit tested on the PC; this file is only the I/O around them.
//   * enqueue() is what the poll loop (core 1) calls: a non-blocking xQueueSend of one line; a full queue drops the
//     line and counts it (handoff_dropped), exactly like SerialJson. It never waits for Wi-Fi or the broker.
//   * The task drains that queue into mqtt::Bridge (drop-oldest-sample-first while offline), joins Wi-Fi, connects
//     to the broker (exponential back-off), publishes, and re-publishes meta after every connect.
//   * ESP-NOW shares the Wi-Fi radio: it runs on whatever channel the access point uses. After the station
//     connects, the channel is read and reported by poll_events() (called from loop(), so all printing stays on the
//     poll loop's thread); a mismatch with ESPNOW_CHANNEL prints a warning (slaves scan the channels to find the master).
#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <PubSubClient.h>
#if MQTT_TLS
#include <WiFiClientSecure.h>
#endif
#include "node_config.h"
#include "mqtt_config.h"
#include "mqtt_logic.h"

namespace net {

class MqttUplink {
public:
    // Starts the task. Returns false (and the uplink stays off) without a Wi-Fi SSID or if allocation fails.
    bool begin() {
        if (!mqtt::site_ok(MQTT_SITE) || !bridge_.set_site(MQTT_SITE)) {
            Serial.println("mqtt: invalid MQTT_SITE (a-z 0-9 - _, max 32): uplink off");
            return false;
        }
        if (strlen(WIFI_SSID) == 0) {
            Serial.println("mqtt: no WIFI_SSID configured (include/secrets.h): uplink off");
            return false;
        }
        q_ = xQueueCreate(MQTT_HANDOFF_DEPTH, sizeof(Item));
        if (!q_) { Serial.println("mqtt: queue alloc failed"); return false; }
#if MQTT_TLS
#  ifdef MQTT_CA_PEM
        net_.setCACert(MQTT_CA_PEM);
#  else
        net_.setInsecure();
        Serial.println("mqtt: WARNING TLS without MQTT_CA_PEM: the server certificate is NOT verified");
#  endif
#endif
        client_.setServer(MQTT_HOST, MQTT_PORT);
        client_.setBufferSize(mqtt::LINE_CAP + 160);  // topic <= ~80 B + payload <= 448 B + header
        client_.setKeepAlive(30);
        client_.setSocketTimeout(5);
        Serial.printf("mqtt: uplink on core 0, broker %s:%d%s, site %s, topics resonance/%s/<type>\r\n", MQTT_HOST, (int)MQTT_PORT,
                      MQTT_TLS ? " (TLS)" : "", MQTT_SITE, MQTT_SITE);
        if (!MQTT_TLS && MQTT_PORT == 1883) Serial.println("mqtt: plain TCP, no authentication: simulator/demo data only");
        BaseType_t ok = xTaskCreatePinnedToCore(&MqttUplink::task_entry, "mqtt", MQTT_TLS ? 12288 : 8192, this, 1, nullptr, 0);
        if (ok != pdPASS) { Serial.println("mqtt: task create failed"); return false; }
        running_ = true;
        return true;
    }
    bool running() const { return running_; }

    // Poll loop side: never blocks. Takes one complete JSON line (with or without the trailing newline).
    bool enqueue(const char *line, size_t len) {
        if (!running_) return false;
        if (len == 0 || len > mqtt::LINE_CAP) { handoff_dropped_++; return false; }
        scratch_.len = (uint16_t)len;
        memcpy(scratch_.data, line, len);
        if (xQueueSend(q_, &scratch_, 0) != pdTRUE) { handoff_dropped_++; return false; }
        enqueued_++;
        return true;
    }

    // Prints connection changes (from loop(), poll thread). Cheap: a few volatile reads.
    void poll_events() {
        if (!running_) return;
        bool w = wifi_up_, m = mqtt_up_;
        if (w != last_wifi_) {
            last_wifi_ = w;
            if (w) {
                uint8_t ch = wifi_channel_;
                Serial.printf("wifi: connected ssid=%s channel=%u\r\n", WIFI_SSID, (unsigned)ch);
                if (ch != ESPNOW_CHANNEL)
                    Serial.printf("WARNING Wi-Fi channel %u != ESPNOW_CHANNEL %d: ESP-NOW now runs on the access point's channel %u; "
                                  "slaves must scan to find it (or build all boards with -DESPNOW_CHANNEL=%u)\r\n",
                                  (unsigned)ch, (int)ESPNOW_CHANNEL, (unsigned)ch, (unsigned)ch);
            } else {
                Serial.println("wifi: disconnected");
            }
        }
        if (m != last_mqtt_) {
            last_mqtt_ = m;
            Serial.println(m ? "mqtt: connected" : "mqtt: disconnected");
        }
    }

    bool wifi_up() const { return wifi_up_; }
    bool mqtt_up() const { return mqtt_up_; }
    uint8_t wifi_channel() const { return wifi_channel_; }
    uint32_t enqueued() const { return enqueued_; }
    uint32_t handoff_dropped() const { return handoff_dropped_; }
    const mqtt::BridgeStats &bridge_stats() const { return bridge_.stats(); }
    const mqtt::QueueStats &queue_stats() const { return bridge_.queue().stats(); }
    uint32_t queued() const { return (uint32_t)bridge_.queue().size(); }
    uint32_t connects() const { return link_.connects(); }
    uint32_t drops() const { return link_.drops(); }

private:
    struct Item {
        uint16_t len;
        char data[mqtt::LINE_CAP];
    };
    struct PsIo : mqtt::Io {
        PubSubClient *c = nullptr;
        bool publish(const char *t, const uint8_t *p, size_t n, bool retain) override { return c->publish(t, p, (unsigned int)n, retain); }
    };

    static void task_entry(void *self) { static_cast<MqttUplink *>(self)->run(); }

    void run() {
        PsIo io;
        io.c = &client_;
        char will_topic[mqtt::TOPIC_MAX];
        bridge_.status_topic(will_topic, sizeof will_topic);
        char client_id[48];
        snprintf(client_id, sizeof client_id, "resonance-%s-%04x", MQTT_SITE, (unsigned)(esp_random() & 0xFFFF));
        Item it;
        for (;;) {
            // 1. hand-off queue -> bridge queue (drop policy lives there); the first wait paces the loop
            TickType_t wait = pdMS_TO_TICKS(20);
            while (xQueueReceive(q_, &it, wait) == pdTRUE) {
                bridge_.push(it.data, it.len);
                wait = 0;
            }
            // 2. link supervision
            bool wifi = WiFi.status() == WL_CONNECTED;
            bool mq = wifi && client_.connected();
            uint32_t now = millis();
            mqtt::Action a = link_.poll(now, wifi, mq, (uint16_t)(esp_random() & 0xFFFF));
            if (a == mqtt::ACT_WIFI_BEGIN) {
                WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL_HINT);
            } else if (a == mqtt::ACT_MQTT_CONNECT) {
                const char *user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
                const char *pass = strlen(MQTT_USER) ? MQTT_PASSWORD : nullptr;
                bool ok = client_.connect(client_id, user, pass, will_topic, 1, true, "offline", true);
                link_.mqtt_result(millis(), ok, (uint16_t)(esp_random() & 0xFFFF));
                if (ok) {
                    mq = true;
                    if (!bridge_.on_connected(io)) { client_.disconnect(); mq = false; }
                }
            }
            if (wifi && !wifi_up_) {
                esp_wifi_set_ps(WIFI_PS_NONE);  // modem sleep would add ESP-NOW receive latency
                wifi_channel_ = (uint8_t)WiFi.channel();
            }
            wifi_up_ = wifi;
            mqtt_up_ = mq;
            // 3. publish
            if (mq) {
                client_.loop();
                if (!bridge_.pump(io, 8)) client_.disconnect();  // the failed line stays queued for the next connection
            }
        }
    }

#if MQTT_TLS
    WiFiClientSecure net_;
#else
    WiFiClient net_;
#endif
    PubSubClient client_{net_};
    mqtt::Bridge<MQTT_QUEUE_DEPTH> bridge_;
    mqtt::Link link_;
    QueueHandle_t q_ = nullptr;
    Item scratch_;
    volatile bool running_ = false, wifi_up_ = false, mqtt_up_ = false;
    volatile uint8_t wifi_channel_ = 0;
    bool last_wifi_ = false, last_mqtt_ = false;
    volatile uint32_t enqueued_ = 0, handoff_dropped_ = 0;
};

}  // namespace net
