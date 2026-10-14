# Resonance structure

Edge structural health monitoring: ESP32 + MPU6050 classifies vibration as normal / abnormal / faulty.
Targets: >=90% accuracy, <50 ms inference, model <100 KB.

## firmware/esp32/ (PlatformIO project, open this folder in VS Code for PlatformIO + Wokwi)
- `platformio.ini` – esp32dev, Arduino, electroniccats/MPU6050@^0.6.0, 115200 baud
- `wokwi.toml` – points Wokwi at `.pio/build/esp32dev/firmware.bin` / `.elf`
- `diagram.json` – ESP32 DevKit v1 + MPU6050 (SDA D21, SCL D22, 3V3, GND), UART0 -> Wokwi serial monitor
- `src/main.cpp` – prints `boot ok <build date time>`, header `ax,ay,az,gx,gy,gz`, then raw int16 CSV at ~100 Hz (CRLF); prints `mpu fail` if sensor not found

## data/
- `generator/generate.py` – synthetic dataset (numpy+pandas, seed 42, FS=100, N=128). 50 recordings/class x 40 non-overlapping windows -> 6000 windows of raw int16 counts. normal: f0 4-12 Hz, small amp; abnormal: higher amp + envelope growth/drift; faulty: f0 shift to 14-30 Hz and/or damped-sinusoid impact spikes. Features per axis: rms (raw, incl. DC), std (mean-removed), excess kurt, domfreq (rfft argmax, DC excluded). Prints class balance + stats.
- `processed/dataset.csv` (gitignored, regenerate with `.venv/Scripts/python data/generator/generate.py`): recording_id, label, ax_0..gz_127 (768), 24 features `{axis}_{rms,std,kurt,domfreq}`. Train/test must split by recording_id.

## Empty placeholders (not yet used)
- `firmware/wokwi/`
- `data/raw/` (gitignored)
- `models/training/`, `models/tflite/` (*.tflite gitignored)
- `docs/`
- `README.md` (empty)

## requirements.txt
- numpy, pandas (installed in `.venv`)

## Status
- Phase 1 (sensor streaming in Wokwi): done, branch `phase1-sensor-stream`
- Phase 2 (synthetic dataset + features): generator verified; band-energy features deferred as a Phase 3 option
