# Resonance

Edge-based structural health monitoring. An ESP32 reads an MPU6050 accelerometer and gyroscope and classifies a structure's vibration as **normal**, **abnormal** or **faulty**, on the device, once per 1.28-second window (about every 1.3 s).

Targets: accuracy ≥ 90%, inference < 50 ms, model < 100 KB.

## Current status

The simulation stage is complete apart from two Wokwi runs of the Phase 5 firmware (smoothing self-test and boot config check), which are still to be recorded. Physical hardware has not arrived, so all results so far come from synthetic data, a PC test harness and the Wokwi simulator.

| | Result |
|---|---|
| Model | Depth-3 decision tree, 11 nodes, **110 B**, using 5 features from 2 sensor axes |
| Accuracy | About **93%** on synthetic data (resampling estimate; real-structure accuracy is untested) |
| Inference | About **24 ms** per window in Wokwi (simulated time) |
| Footprint | On-device pipeline adds +2.6 KB flash and +0.6 KB RAM vs the Phase 1 sensor-streaming firmware |
| C vs Python | 6000/6000 windows match on PC, 606/606 on the simulated ESP32 |

Full details, including what simulation cannot prove: [docs/simulation_validation.md](docs/simulation_validation.md).

## Blocked on hardware (Phase 6)

- Real inference timing and boot-time sensor configuration check (`config ok dlpf=3 accel=0 gyro=0`).
- Accuracy on real structures, and the real sensor's noise level compared with the synthetic data.
- Sensor mounting orientation (the model assumes `az` is the gravity axis).

## Project history

Each phase was built on its own branch, independently verified, then merged into `master` in order with `--no-ff`, so each phase appears as a merge commit.

| Phase | Branch | What it delivered |
|---|---|---|
| 1 | `phase1-sensor-stream` | ESP32 + MPU6050 circuit in Wokwi, raw sensor values streamed over serial |
| 2 | `phase2-dataset` | Synthetic vibration generator: 150 recordings, 6000 labelled windows with features |
| 3 | `phase3-model` | Model selection by recording-level cross-validation; depth-3 tree exported for C; comparison against 23 alternative configurations |
| 4 | `phase4-firmware` | On-device feature extraction and tree inference in C, verified against Python |
| 5 | `phase5-validation` | Sensor-filter sensitivity check, 2-of-3 prediction smoothing, boot-time config check, simulation validation report |

## Repository layout

| Path | Contents |
|---|---|
| `data/generator/generate.py` | Synthetic dataset generator (output `data/processed/dataset.csv` is not committed; rerun to recreate it) |
| `models/training/` | Training and selection (`train.py`), model comparison, sensor-filter check, smoothing evaluation |
| `models/selected/` | Deployed model, export for C, feature specification, reports and validation results |
| `firmware/esp32/` | PlatformIO project: live firmware, self-test firmware, PC tests, code generators, Wokwi configs |
| `docs/` | Simulation validation report |

## Running it

Python 3.12 virtual environment with `requirements.txt` installed:

```
.venv\Scripts\python.exe data\generator\generate.py
.venv\Scripts\python.exe firmware\esp32\tools\gen_testdata.py
```

Then, from `firmware/esp32` (PlatformIO; a host GCC is needed for the PC test):

```
pio test -e native
pio run -e esp32dev
pio run -e selftest
```

In VS Code with the Wokwi extension, open `firmware/esp32`, run "Wokwi: Select Config File" (`wokwi.toml` for the live firmware, `selftest/wokwi.toml` for the self-test), then "Wokwi: Start Simulator". The live firmware prints `window,class,state,feat_us,tree_us,span_us` once per window.

The full set of commands to reproduce every result is in [docs/simulation_validation.md](docs/simulation_validation.md#10-reproducing-the-results).
