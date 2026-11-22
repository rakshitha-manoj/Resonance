# Vendored library

- `mqtt.min.js`: MQTT.js 5.10.4 browser bundle (`dist/mqtt.min.js`, global `mqtt`), from https://cdn.jsdelivr.net/npm/mqtt@5.10.4/dist/mqtt.min.js
- License: MIT, `mqtt.LICENSE.md` (copyright MQTT.js contributors). Project: https://github.com/mqttjs/MQTT.js
- SHA-256 of `mqtt.min.js`: 0d5d035815d2a462e6edf0f86ab91f7a5d563a36b40e9760e4ee452618f7c45e
- Used only by the "MQTT live" source of `../index.html`. Without this file the page still opens and works offline (replay, demo, USB); MQTT live then reports that the library is missing.
- `qrcode.js`: qrcode-generator 1.4.4 by Kazuhiko Arase (browser file `qrcode.js` of the npm package `qrcode-generator@1.4.4`, unmodified, global `qrcode`), from https://registry.npmjs.org/qrcode-generator/-/qrcode-generator-1.4.4.tgz
- License: MIT, `qrcode.LICENSE.md` (copyright 2009 Kazuhiko Arase; the licence text is the file header of `qrcode.js` plus the standard MIT text). Project: https://github.com/kazuhikoarase/qrcode-generator
- SHA-256 of `qrcode.js`: 18ae399f81182bc9de916e9c77b195df20cc58d6f2d55a62b085a299f1bf1780
- Used only by the "Phone view" button of `../index.html` (draws the QR code to an inline SVG). Without this file the button still shows the link as text.
