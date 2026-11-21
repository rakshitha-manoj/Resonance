# Vendored library

- `mqtt.min.js`: MQTT.js 5.10.4 browser bundle (`dist/mqtt.min.js`, global `mqtt`), from https://cdn.jsdelivr.net/npm/mqtt@5.10.4/dist/mqtt.min.js
- License: MIT, `mqtt.LICENSE.md` (copyright MQTT.js contributors). Project: https://github.com/mqttjs/MQTT.js
- SHA-256 of `mqtt.min.js`: 0d5d035815d2a462e6edf0f86ab91f7a5d563a36b40e9760e4ee452618f7c45e
- Used only by the "MQTT live" source of `../index.html`. Without this file the page still opens and works offline (replay, demo, USB); MQTT live then reports that the library is missing.
