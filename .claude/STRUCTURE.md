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

## models/
- `training/train.py` – Phase 3 selection (~1 h runtime). 18 inputs `{axis}_{std,kurt,domfreq}` (rms excluded: DC/offset dependent). Fixed holdout 10 rec/class (SEED=42; the discarded `rng.permutation(dev)` call must stay, it keeps the holdout draw stable), 10x repeated grouped 5-fold on dev, grid DT/RF/MLP, RF scored by hard vote. Rule: smallest footprint (10 B/node) among configs <=5120 B with all 10 repeat means >=0.90. Also broad resampling, offset test, export.
- `training/compare.py`, `training/compare_svm.py` – post-selection comparison of 18 DT/RF + 6 SVM configs vs dt_d3 under the same broad resampling (200 holdout re-draws, 40x grouped 5-fold on all 150 recordings), pre-registered replacement rule. Outputs `selected/comparison/`.
- `selected/` – deployed model = dt_d3 retrained on all 150 recordings: `model.joblib`, `model_export.json` (11 nodes, float32 thresholds, go left if x <= t), `feature_spec.json` (inputs, used features, formulas, MPU6050 ±2 g / ±250 °/s, exact 100 Hz timer sampling), `report.json`/`report.md` (headline ~0.93 broad resampling; selection history v1→v4), `comparison.md` (no alternative qualified; fallback rf_n50_d4).

## Empty placeholders (not yet used)
- `firmware/wokwi/`
- `data/raw/` (gitignored)
- `models/tflite/` (unused: deployment is hand-written C, not TFLite)
- `docs/`
- `README.md` (empty)

## requirements.txt
- numpy, pandas, scikit-learn, joblib pinned to the versions in `.venv` (Python 3.12); model.joblib depends on the scikit-learn version

## Status
- Phase 1 (sensor streaming in Wokwi): done, branch `phase1-sensor-stream`
- Phase 2 (synthetic dataset + features): done, branch `phase2-dataset`
- Phase 3 (model selection): done, branch `phase3-model`. dt_d3 depth 3, trained on all 150 recordings: 11 nodes, 110 B, ~0.93 accuracy (procedure estimate, no untouched data). Band energy not needed. Fallback rf_n50_d4 (11.5 KB, all 18 features, 6 FFTs).
- Phase 4 notes: compute only az (std, kurt, domfreq) and gx (std, domfreq); FFT on az and gx only; variance must be two-pass (mean first) in float32 — one-pass E[x²]-mean² on az (~16384 DC) flips predictions; domfreq thresholds equal bin index k<=17 (az) and k<=20 (gx); set MPU6050 DLPF (~44 Hz) for anti-aliasing; timer-driven 100 Hz; define mounting orientation (az = gravity axis) and inference cadence.
