#pragma once
// Cloud uplink configuration (master only, -DMQTT_ENABLED). Every value can come from, in this order:
//   1. -D build flags (platformio.ini / pio run -e ... with build_flags),
//   2. include/secrets.h (GITIGNORED; copy include/secrets.example.h), included first so it wins over the defaults,
//   3. the defaults below (public demo broker, simulator data only).
// NEVER commit Wi-Fi or broker credentials. A private broker (e.g. a HiveMQ Cloud cluster with TLS + user/password)
// needs only config: MQTT_HOST=<cluster>.s1.eu.hivemq.cloud, MQTT_PORT=8883, MQTT_TLS=1, MQTT_USER, MQTT_PASSWORD.
#if defined(__has_include) && !defined(WOKWI_CLOUD_DEMO)   // the Wokwi demo is always Wokwi-GUEST + the demo defaults
#  if __has_include("secrets.h")
#    include "secrets.h"
#  endif
#endif

#ifndef WIFI_SSID
#define WIFI_SSID ""            // empty = no Wi-Fi: the master runs as before, the uplink stays idle
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""        // empty = open network
#endif
#ifndef WIFI_CHANNEL_HINT
#define WIFI_CHANNEL_HINT 0     // 0 = scan; Wokwi-GUEST is on channel 6 (a hint only makes joining faster)
#endif

// Default broker = the free public HiveMQ one, over TLS (port 8883, certificate verified with the committed root CA in
// hivemq_public_ca.h; needs the wall clock, the uplink gets it from SNTP before connecting). Wokwi's gateway does not pass plain
// MQTT on 1883 reliably, 8883 works. -DMQTT_PUBLIC_TLS=0 falls back to plain 1883 (no encryption, no server check).
// Any MQTT_HOST of your own is used as given: set MQTT_PORT / MQTT_TLS / MQTT_CA_PEM with it (secrets.h).
#ifndef MQTT_HOST
#define MQTT_HOST "broker.hivemq.com"   // anybody can read every topic of this broker. Demo/simulator data only!
#  ifndef MQTT_PUBLIC_TLS
#  define MQTT_PUBLIC_TLS 1
#  endif
#  if MQTT_PUBLIC_TLS
#    ifndef MQTT_PORT
#    define MQTT_PORT 8883
#    endif
#    ifndef MQTT_TLS
#    define MQTT_TLS 1
#    endif
#    ifndef MQTT_CA_PEM
#    include "hivemq_public_ca.h"
#    define MQTT_CA_PEM HIVEMQ_PUBLIC_CA_PEM
#    endif
#  endif
#endif
#ifndef MQTT_PORT
#define MQTT_PORT 1883                  // 1883 plain, 8883 TLS
#endif
#ifndef MQTT_USER
#define MQTT_USER ""                    // empty = anonymous
#endif
#ifndef MQTT_PASSWORD
#define MQTT_PASSWORD ""
#endif
#ifndef MQTT_TLS
#define MQTT_TLS 0                      // 1 = TLS (WiFiClientSecure); with MQTT_CA_PEM the server is verified, without it NOT (warned at boot)
#endif
// #define MQTT_CA_PEM "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"   // root CA, in secrets.h

// Topic prefix: resonance/<site>/<type>. A random id, so the public broker's topic space is not shared by accident.
// Anybody who knows the id can read the data: that is acceptable only for simulator/demo data.
#ifndef MQTT_SITE
#define MQTT_SITE "rsn-822298ab"
#endif

// Lines waiting for a (re)connect in the uplink task (mqtt_logic.h LineQueue, ~450 B each); samples are dropped first.
#ifndef MQTT_QUEUE_DEPTH
#define MQTT_QUEUE_DEPTH 24
#endif
// Hand-off queue from the poll loop to the uplink task (FreeRTOS queue of complete lines).
#ifndef MQTT_HANDOFF_DEPTH
#define MQTT_HANDOFF_DEPTH 12
#endif
// Of those slots this many are kept for non-sample lines (events, node, meta, restart, damage): samples are refused first.
#ifndef MQTT_HANDOFF_RESERVE
#define MQTT_HANDOFF_RESERVE 4
#endif
#if MQTT_HANDOFF_RESERVE >= MQTT_HANDOFF_DEPTH
#error "MQTT_HANDOFF_RESERVE must be below MQTT_HANDOFF_DEPTH"
#endif
