# Resonance structure

Edge structural health monitoring: ESP32 + MPU6050 classifies vibration as normal / abnormal / faulty.
Targets: >=90% accuracy, <50 ms inference, model <100 KB.

## firmware/esp32/ (PlatformIO project, open this folder in VS Code for PlatformIO + Wokwi)
- `platformio.ini` – esp32dev, Arduino, electroniccats/MPU6050@^0.6.0, 115200 baud
- `wokwi.toml` – points Wokwi at `.pio/build/esp32dev/firmware.bin` / `.elf`
- `diagram.json` – ESP32 DevKit v1 + MPU6050 (SDA D21, SCL D22, 3V3, GND), UART0 -> Wokwi serial monitor
- `src/main.cpp` – prints `boot ok <build date time>`, header `ax,ay,az,gx,gy,gz`, then raw int16 CSV at ~100 Hz (CRLF); prints `mpu fail` if sensor not found

## Empty placeholders (not yet used)
- `firmware/wokwi/`
- `data/generator/`, `data/raw/`, `data/processed/` (raw/processed are gitignored)
- `models/training/`, `models/tflite/` (*.tflite gitignored)
- `docs/`
- `requirements.txt`, `README.md` (empty)

## Status
- Phase 1 (sensor streaming in Wokwi): done, branch `phase1-sensor-stream`
