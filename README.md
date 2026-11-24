# Resonance

Structural health monitoring with IoT and TinyML. Two ESP32 + MPU6050 sensor nodes each run a 110 B decision tree, an int8 autoencoder (942 B of weights, trained on healthy data only) and a natural-frequency tracker. An ESP32 master fuses the two nodes into AMBIENT / LOCAL / BUILDING events plus a DAMAGE alarm, and publishes over MQTT to a browser dashboard with console and browser-notification alerts.

**Status: the simulation stage is complete; hardware bring-up is pending.** Every result is from the project's building simulator, a PC test harness or the Wokwi emulator. **Nothing has been measured on real boards or structures.**

> **New to the project? Read [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md)**: the whole project in one document, from the problem to the results, in plain language.

> **Start here: [PROJECT_GUIDE.md](PROJECT_GUIDE.md)** (one-minute summary, architecture, repository map, setup, every run command, demo-day checklist and script, troubleshooting, honest limitations, glossary).

## Documents

- [Final project report](docs/project_report.md) (architecture, models, results, limitations, reproduction)
- [Viva preparation](docs/viva_prep.md) and [slide outline](docs/slides_outline.md)
- [Demo runbook](docs/demo_runbook.md) (offline dashboard demo: open `dashboard/index.html`, click "Damage demo (simulated)")
- [Hardware bring-up guide](docs/hardware_bringup.md)
- Earlier stage reports: [single node (Phases 1-5)](docs/simulation_validation.md), [hierarchy (R1-R6)](docs/hierarchy_validation.md)

## Headline results (simulation only; sources in the report)

| | Result |
|---|---|
| Decision tree | 11 nodes, **110 B**, about **0.93** accuracy on synthetic data (resampling estimate, no untouched test) |
| int8 autoencoder | 574 parameters, 942 B weights; C kernel bit-exact with TensorFlow Lite on the PC (0 mismatches on 10,000 vectors) |
| Master rule f (std ratio OR autoencoder), fresh pre-registered block, 240 buildings, 28 h | mean event recall **0.734** against 0.622 for the std ratio alone (paired +0.113); **1.64 false onsets per hour, the 1/h budget is not met** (2.05/h on healthy ambient alone) |
| DAMAGE (natural-frequency drop) | 30 % stiffness loss detected 1.00 / 1.00 / 0.91 at storeys 1 / 3 / 5, almost never at the top storeys (physics); coarse alarm only |
| Hierarchy vs one node | same false-alarm rate; adds the second floor, event typing and dead-slave handling (rule b comparison) |
| Storey localisation | failed its pre-stated gates, not claimed |
| Cloud | MQTT (TLS, retained state, last will), dashboard, alerts (console monitor, browser notifications); public broker is demo-only |
| Resources | slave build 772,825 B flash / 72,788 B RAM; ESP32 compute estimated 3-8 ms per window (not measured) |

Older sections below describe the stage at which each part was built; where numbers differ, the report and the latest result files win.

## Blocked on hardware (Phase 6)

- Real inference timing and boot-time sensor configuration check (`config ok dlpf=3 accel=0 gyro=0`).
- Accuracy on real structures, and the real sensor's noise level compared with the synthetic data.
- Sensor mounting orientation and axis: the simulator puts the lateral sway on `az`, while the bring-up guide mounts `az` as the vertical gravity axis, which would see little sway; the axis choice and threshold transfer must be settled on hardware.
- For the three-board system: radio link loss and timing, compute time against the 50 ms limit, the noise gate value, and the dashboard's live mode. The measurements are listed in [docs/hardware_bringup.md](docs/hardware_bringup.md).

## Project history

Branch `tinyml-iot` (current) adds Phase B (autoencoder), Phase A (f1 tracking, DAMAGE), Phase C (firmware integration, dashboard schema 3, demo), Phase D (MQTT cloud layer, alerts), Phase F (this documentation). `master` still holds the work up to the hierarchy merge.

Phases 1 to 5 were each built on their own branch, independently verified, then merged into `master` in order with `--no-ff`, so each phase appears as a merge commit. The hierarchy work (R1 to R6) was done on `hier2-protocol` and merged into `master` (merge commit `f58d8ad`).

| Phase | Branch | What it delivered |
|---|---|---|
| 1 | `phase1-sensor-stream` | ESP32 + MPU6050 circuit in Wokwi, raw sensor values streamed over serial |
| 2 | `phase2-dataset` | Synthetic vibration generator: 150 recordings, 6000 labelled windows with features |
| 3 | `phase3-model` | Model selection by recording-level cross-validation; depth-3 tree exported for C; comparison against 23 alternative configurations |
| 4 | `phase4-firmware` | On-device feature extraction and tree inference in C, verified against Python |
| 5 | `phase5-validation` | Sensor-filter sensitivity check, 2-of-3 prediction smoothing, boot-time config check, simulation validation report |
| R0 | `hier2-protocol` (commit `44b5a86`) | Building simulator, two storey-localisation gates (both failed), retraining on building data (kept old tree) |
| R1 | `hier2-protocol` (commit `c4d0912`) | Three-node protocol, master and slave state machines, PC network simulator |
| R2 to R6 | `hier2-protocol` | Master decision logic and its two selection rounds (R2, R2b), hierarchy against a single node (R3), dashboard (R4), ESP-NOW hardware firmware and bring-up guide (R5), this report (R6) |

A branch `hier1-building-sim` exists but points at the Phase 5 merge commit (`49de083`); the building-simulation commit `44b5a86` is on `hier2-protocol`.

## Repository layout

| Path | Contents |
|---|---|
| `data/generator/generate.py` | Synthetic dataset generator (output `data/processed/dataset.csv` is not committed; rerun to recreate it) |
| `data/generator/building_sim.py` | 10-storey shear-building simulator (floor acceleration with damage, impacts, strong excitation) |
| `models/training/` | Training and selection (`train.py`), model comparison, filter and smoothing checks; localisation gates, building retraining; fusion selection (`fusion_eval*.py`, R2, R2b, R3), single-node comparison (`compare_hierarchy.py`); autoencoder (`ae_*.py`, `ae_final.py`); f1 tracking (`freq_tracking_eval.py`, `shm.py`); ctypes bridges to the firmware C code |
| `models/selected/` | Deployed model, export for C, feature specification, reports and validation results (including gate, fusion and hierarchy results) |
| `models/selected_building/` | Building-data retraining candidate and its report (not deployed) |
| `firmware/esp32/` | PlatformIO project: live firmware, self-test firmware, network firmware (`master`, `slave1`, `slave2`; `include/net_proto.h`, `tiers.h`, `fusion.h`, `espnow_logic.h`), network simulator (`netsim`), PC tests, code generators (`tools/`), Wokwi configs (`wokwi/`: one folder per board of the 3-ESP32 system, a wiring view of all three, and the earlier single-board configs) |
| `dashboard/` | `index.html` single-file dashboard (replay, live USB, MQTT live, schema-3 damage demo embedded), simulated demo logs, validators and headless tests |
| `docs/` | `project_report.md` (final report), `viva_prep.md`, `slides_outline.md`, `demo_runbook.md`, `simulation_validation.md` (Phases 1-5), `hierarchy_validation.md` (R1-R6), `hardware_bringup.md`, `figures/` |

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

In VS Code with the Wokwi extension, open `firmware/esp32`, build the board you want (`pio run -e master`, `pio run -e slave1_wokwi` or `pio run -e slave2_wokwi`), run "Wokwi: Select Config File" and pick `wokwi/master/`, `wokwi/slave1/` or `wokwi/slave2/` `wokwi.toml`, then "Wokwi: Start Simulator". Wokwi simulates one board at a time; the 3-board network runs on the PC in `netsim`. The earlier single-board configs are in `wokwi/single_node/` (env `esp32dev`) and `wokwi/single_node_selftest/` (env `selftest`). See `firmware/esp32/wokwi/README.md`, which also has the wiring of all three boards.

Network firmware and tests (also from `firmware/esp32`):

```
pio test -e netsim            # protocol, state machines, simulator, master decision logic
pio test -e espnow_logic      # ESP-NOW receive logic
pio run -e master             # also slave1, slave2; flashing: docs/hardware_bringup.md
```

Hierarchy studies (from the repository root; some take minutes to an hour, see `docs/hierarchy_validation.md` section 12):

```
.venv\Scripts\python.exe models\training\test_gates.py
.venv\Scripts\python.exe models\training\localisation_gate_v2.py
.venv\Scripts\python.exe models\training\fusion_eval_r2b.py
.venv\Scripts\python.exe models\training\compare_hierarchy.py
```

Dashboard: open `dashboard/index.html` in Chrome or Edge.

The commands to reproduce Phases 1 to 5 are in [docs/simulation_validation.md](docs/simulation_validation.md#10-reproducing-the-results); those for the hierarchy stage are in [docs/hierarchy_validation.md](docs/hierarchy_validation.md#12-how-to-reproduce).
