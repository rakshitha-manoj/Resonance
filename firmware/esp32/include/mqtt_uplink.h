#pragma once
// MQTT uplink of the master (ESP32 Arduino only, -DMQTT_ENABLED): Wi-Fi station + PubSubClient on a FreeRTOS task
// pinned to core 0, fed by a FreeRTOS queue of complete JSON lines. The decisions (topics, queue policy, back-off,
// meta re-publish, status payloads) are in mqtt_logic.h and unit tested on the PC; this file is only the I/O around them.
//   * enqueue() is what the poll loop (core 1) calls: a non-blocking xQueueSend of one line; a full queue drops the
//     line and counts it, exactly like SerialJson. Samples are refused first (MQTT_HANDOFF_RESERVE slots stay free for
//     events / node / meta / restart / damage lines). It never waits for Wi-Fi or the broker.
//   * The task drains that queue into mqtt::Bridge (drop-oldest-sample-first while offline), joins Wi-Fi, connects to the
//     broker (exponential back-off), publishes, and re-publishes meta after every connect.
//   * Status: retained {"state":"online","epoch":E} after every connect and every 60 s; the last will is the same text with
//     "offline". E = the master's boot epoch (the same number as meta.epoch), so a consumer can tell the will of an OLD
//     half-open session (it fires ~45 s after an unclean reboot, after the new boot already said "online") from a real
//     outage: an "offline" whose epoch differs from the latest meta epoch is stale.
//   * ESP-NOW shares the Wi-Fi radio: it runs on whatever channel the access point uses. The radio is protected as far as the
//     stack allows: the Arduino auto-reconnect is switched off (it rescans forever when the router is gone), joins are driven by
//     mqtt::Link with a slow back-off (30 s .. 5 min), and the boot-time join (wait_wifi()) happens BEFORE the master beacons.
//     After the first join each re-join passes the remembered channel + BSSID, but that is only a HINT: the IDF starts its scan on
//     that channel and, when the AP is absent, still scans the others (fast-scan stops early only when the AP is found). So with
//     the router DOWN every join attempt takes the radio off the ESP-NOW channel for roughly 1.5-2 s: expect an occasional missed
//     poll or beacon (one DEAD needs 3 consecutive missed superframes, so a single attempt every >= 30 s should not kill a slave,
//     but this is NOT verified on hardware: step 8b of docs/hardware_bringup.md measures it). If the router returns on another
//     channel the master follows it and the slaves rescan (ChannelScan, up to ~20 s).
//   * Blocking calls: connect() runs in the task, never in the poll loop. TCP connect 5 s and TLS handshake 10 s at most
//     (WiFiClientSecure defaults are 30 s / 120 s); the hand-off queue keeps taking lines meanwhile (12 slots, 4 reserved for
//     non-samples): in the worst case (15 s, ~11 lines) samples are refused first, events/node lines survive, all counted.
//   * TLS certificate DATES are NOT checked by this build (the framework's mbedtls has CONFIG_MBEDTLS_HAVE_TIME_DATE off): the
//     chain is verified against the committed root CA, expiry is ignored. SNTP is therefore best-effort only (timestamps), and a
//     blocked UDP 123 does not stop the uplink.
#include <Arduino.h>
#include <WiFi.h>
#include <time.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include "node_config.h"
#include "mqtt_config.h"   // before the first use of MQTT_TLS
#include <PubSubClient.h>
#if MQTT_TLS
#include <WiFiClientSecure.h>
#endif
#include "mqtt_logic.h"

namespace net {

class MqttUplink {
public:
    // Starts the task. Returns false (and the uplink stays off) without a Wi-Fi SSID or if allocation fails.
    // epoch = the master's boot epoch (non-zero), carried in the status payloads.
    bool begin(uint32_t epoch) {
        if (!mqtt::site_ok(MQTT_SITE) || !bridge_.set_site(MQTT_SITE)) {
            Serial.println("mqtt: invalid MQTT_SITE (a-z 0-9 - _, max 32): uplink off");
            return false;
        }
        if (strlen(WIFI_SSID) == 0) {
            Serial.println("mqtt: no WIFI_SSID configured (include/secrets.h): uplink off");
            return false;
        }
        bridge_.set_epoch(epoch);
        q_ = xQueueCreate(MQTT_HANDOFF_DEPTH, sizeof(Item));
        if (!q_) { Serial.println("mqtt: queue alloc failed"); return false; }
        WiFi.persistent(false);
        WiFi.setSleep(false);
        WiFi.setAutoReconnect(false);   // joins are ours (mqtt::Link): the stack's own retries scan every channel, see above
        WiFi.mode(WIFI_STA);
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
        net_.setTimeout(5);               // seconds: TCP connect and socket reads (WiFiClientSecure default 30 s)
#if MQTT_TLS
        net_.setHandshakeTimeout(10);     // seconds (default 120 s)
#endif
        Serial.printf("mqtt: uplink on core 0, broker %s:%d%s, site %s, topics resonance/%s/<type>\r\n", MQTT_HOST, (int)MQTT_PORT,
                      MQTT_TLS ? " (TLS)" : "", MQTT_SITE, MQTT_SITE);
        if (!MQTT_TLS) Serial.println("mqtt: plain TCP, no encryption or authentication: simulator/demo data only");
        BaseType_t ok = xTaskCreatePinnedToCore(&MqttUplink::task_entry, "mqtt", MQTT_TLS ? 12288 : 8192, this, 1, nullptr, 0);
        if (ok != pdPASS) { Serial.println("mqtt: task create failed"); return false; }
        running_ = true;
        return true;
    }
    bool running() const { return running_; }

    // Boot-time join: waits up to timeout_ms for the first Wi-Fi connection, so that the radio sits on the access point's
    // channel BEFORE the master sends its first beacon (a scan that overlaps the first beacons would lose them). Returns
    // true if joined. Not joined = carry on; the task retries slowly in the background.
    bool wait_wifi(uint32_t timeout_ms) {
        uint32_t t0 = millis();
        while (running_ && !wifi_up_ && millis() - t0 < timeout_ms) delay(50);
        return wifi_up_;
    }

    // Poll loop side: never blocks. Takes one complete JSON line (with or without the trailing newline).
    bool enqueue(const char *line, size_t len) {
        if (!running_) return false;
        if (len == 0 || len > mqtt::LINE_CAP) { handoff_dropped_++; return false; }
        mqtt::Kind k = mqtt::route_of(line, len).kind;
        if (!mqtt::handoff_accepts(k, (size_t)uxQueueSpacesAvailable(q_), MQTT_HANDOFF_RESERVE)) {
            if (k == mqtt::K_SAMPLE) handoff_dropped_samples_++;
            else handoff_dropped_++;
            return false;
        }
        scratch_.len = (uint16_t)len;
        memcpy(scratch_.data, line, len);
        if (xQueueSend(q_, &scratch_, 0) != pdTRUE) {
            if (k == mqtt::K_SAMPLE) handoff_dropped_samples_++;
            else handoff_dropped_++;
            return false;
        }
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
                Serial.println("wifi: disconnected (no automatic retry by the stack; slow re-join by the uplink)");
            }
        }
        if (m != last_mqtt_) {
            last_mqtt_ = m;
            Serial.println(m ? "mqtt: connected" : "mqtt: disconnected");
        }
        if (time_ok_ != last_time_ok_) {
            last_time_ok_ = time_ok_;
            if (time_ok_) Serial.println("mqtt: wall clock set (SNTP; informational, certificate dates are not checked by this build)");
        }
        uint32_t att = connect_attempts_, fl = connect_fails_;
        if (fl != last_fails_) {   // one clear line per failed attempt: reason from PubSubClient::state() and, for TLS, mbedtls
            last_fails_ = fl;
            int st = last_state_;
            const char *why = st == -2 ? "TCP/TLS connect failed (unreachable, blocked port, handshake or certificate)" :
                              st == -4 ? "timeout" : st == -3 ? "connection lost" : st == -1 ? "disconnected" :
                              st == 4 ? "bad credentials" : st == 5 ? "not authorized" : "refused by the broker";
            Serial.printf("mqtt: connect attempt %u to %s:%d failed: state=%d %s%s%s (next try after back-off)\r\n", (unsigned)att,
                          MQTT_HOST, (int)MQTT_PORT, st, why, last_err_[0] ? ", tls: " : "", last_err_);
        }
    }

    bool wifi_up() const { return wifi_up_; }
    bool mqtt_up() const { return mqtt_up_; }
    uint8_t wifi_channel() const { return wifi_channel_; }
    uint32_t enqueued() const { return enqueued_; }
    uint32_t handoff_dropped() const { return handoff_dropped_; }                 // non-sample lines refused (hand-off queue full)
    uint32_t handoff_dropped_samples() const { return handoff_dropped_samples_; } // samples refused (reserve or full)
    const mqtt::BridgeStats &bridge_stats() const { return bridge_.stats(); }
    const mqtt::QueueStats &queue_stats() const { return bridge_.queue().stats(); }
    uint32_t queued() const { return (uint32_t)bridge_.queue().size(); }
    uint32_t connects() const { return link_.connects(); }
    uint32_t drops() const { return link_.drops(); }
    uint32_t wifi_joins() const { return wifi_joins_; }
    uint32_t fast_joins() const { return fast_joins_; }
    uint32_t connect_attempts() const { return connect_attempts_; }
    uint32_t connect_fails() const { return connect_fails_; }

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

    // Drops the connection WITHOUT an MQTT DISCONNECT packet (PubSubClient::disconnect() would send one, and a clean disconnect
    // cancels the last will): the broker then publishes the will, the same as for a real network failure.
    void drop_connection() { net_.stop(); }

    void join_wifi() {
        WiFi.disconnect(false, false);  // end any attempt still running
        wifi_joins_++;
        if (have_ap_ && fast_fails_ < 3) {
            if (wifi_attempt_open_) fast_fails_++;
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD, saved_channel_, saved_bssid_, true);   // known channel + BSSID: a hint, NOT a guarantee of no scan
            fast_joins_++;
        } else {
            if (have_ap_) { have_ap_ = false; fast_fails_ = 0; }                         // the AP moved? allow a full scan again
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL_HINT);
        }
        wifi_attempt_open_ = true;
    }

    void run() {
        PsIo io;
        io.c = &client_;
        char will_topic[mqtt::TOPIC_MAX], will_payload[64];
        bridge_.status_topic(will_topic, sizeof will_topic);
        bridge_.status_payload(will_payload, sizeof will_payload, false);   // {"state":"offline","epoch":E}
        char client_id[48];
        snprintf(client_id, sizeof client_id, "resonance-%s-%04x", MQTT_SITE, (unsigned)(esp_random() & 0xFFFF));
        Item it;
        uint32_t last_status_ms = 0;
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
                join_wifi();
            } else if (a == mqtt::ACT_MQTT_CONNECT) {
                const char *user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
                const char *pass = strlen(MQTT_USER) ? MQTT_PASSWORD : nullptr;
                connect_attempts_++;
                bool ok = client_.connect(client_id, user, pass, will_topic, 1, true, will_payload, true);
                last_state_ = client_.state();
                if (!ok) {
                    connect_fails_++;
                    last_err_[0] = 0;
#if MQTT_TLS
                    net_.lastError(last_err_, sizeof last_err_);
#endif
                }
                link_.mqtt_result(millis(), ok, (uint16_t)(esp_random() & 0xFFFF));
                if (ok) {
                    mq = true;
                    last_status_ms = millis();
                    if (!bridge_.on_connected(io)) { drop_connection(); mq = false; }
                }
            }
            if (!time_ok_ && time(nullptr) > 1700000000) time_ok_ = true;   // informational only
            if (wifi && !wifi_up_) {
                esp_wifi_set_ps(WIFI_PS_NONE);  // modem sleep would add ESP-NOW receive latency
                wifi_channel_ = (uint8_t)WiFi.channel();
                const uint8_t *b = WiFi.BSSID();
                if (b) { memcpy(saved_bssid_, b, 6); saved_channel_ = wifi_channel_; have_ap_ = true; }
                fast_fails_ = 0;
                wifi_attempt_open_ = false;
                configTime(0, 0, "pool.ntp.org", "time.google.com");   // best-effort SNTP (timestamps); nothing waits for it
            }
            wifi_up_ = wifi;
            mqtt_up_ = mq;
            // 3. publish
            if (mq) {
                client_.loop();
                if (!bridge_.pump(io, 8)) { drop_connection(); }   // the failed line stays queued for the next connection
                else if (millis() - last_status_ms >= 60000) {     // heal a lost retained status / restarted broker
                    last_status_ms = millis();
                    if (!bridge_.publish_status(io)) drop_connection();
                }
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
    mqtt::Link link_{30000, 300000};   // Wi-Fi join attempts: first at once, then 30 s, 60 s, ... 5 min apart
    QueueHandle_t q_ = nullptr;
    Item scratch_;
    volatile bool running_ = false, wifi_up_ = false, mqtt_up_ = false, time_ok_ = !MQTT_TLS;
    volatile uint8_t wifi_channel_ = 0;
    bool last_wifi_ = false, last_mqtt_ = false, last_time_ok_ = !MQTT_TLS;
    bool have_ap_ = false, wifi_attempt_open_ = false;
    uint8_t saved_bssid_[6] = {0}, saved_channel_ = 0, fast_fails_ = 0;
    volatile uint32_t connect_attempts_ = 0, connect_fails_ = 0;
    uint32_t last_fails_ = 0;
    volatile int last_state_ = 0;
    char last_err_[80] = {0};
    volatile uint32_t enqueued_ = 0, handoff_dropped_ = 0, handoff_dropped_samples_ = 0, wifi_joins_ = 0, fast_joins_ = 0;
};

}  // namespace net
