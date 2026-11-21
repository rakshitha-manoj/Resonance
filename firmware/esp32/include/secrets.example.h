#pragma once
// Copy this file to include/secrets.h (gitignored) and fill in YOUR values. Never commit secrets.h.
// Any macro of include/mqtt_config.h can be set here.

#define WIFI_SSID "my-router"
#define WIFI_PASSWORD "my-wifi-password"

// Private broker example (HiveMQ Cloud free cluster, TLS + user/password):
// #define MQTT_HOST "xxxxxxxx.s1.eu.hivemq.cloud"
// #define MQTT_PORT 8883
// #define MQTT_TLS 1
// #define MQTT_USER "resonance"
// #define MQTT_PASSWORD "a-long-random-password"
// #define MQTT_CA_PEM "-----BEGIN CERTIFICATE-----\n...ISRG Root X1...\n-----END CERTIFICATE-----\n"

// Use your own site id for real data (anybody who knows it can read a public broker's topics):
// #define MQTT_SITE "rsn-0123abcd"
