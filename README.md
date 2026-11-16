# Resonance

Edge-based structural health monitoring. An ESP32 reads an MPU6050 accelerometer and gyroscope and classifies a structure's vibration as **normal**, **abnormal** or **faulty**, on the device, once per 1.28-second window (about every 1.3 s).

Targets: accuracy ≥ 90%, inference < 50 ms, model < 100 KB.

## Current status

The single-node simulation stage is complete apart from two Wokwi runs of the Phase 5 firmware (smoothing self-test and boot config check), which are still to be recorded. Physical hardware has not arrived, so all results so far come from synthetic data, a PC test harness and the Wokwi simulator.

| | Result |
|---|---|
| Model | Depth-3 decision tree, 11 nodes, **110 B**, using 5 features from 2 sensor axes |
| Accuracy | About **93%** on synthetic data (resampling estimate; real-structure accuracy is untested) |
| Inference | About **24 ms** per window in Wokwi (simulated time) |
| Footprint | On-device pipeline adds +2.6 KB flash and +0.6 KB RAM vs the Phase 1 sensor-streaming firmware |
| C vs Python | 6000/6000 windows match on PC, 606/606 on the simulated ESP32 |

Full details, including what simulation cannot prove: [docs/simulation_validation.md](docs/simulation_validation.md).

### Hierarchy stage (branch `hier2-protocol`)

The project now also has a three-board design: one master and two slaves, each slave running the classifier above, the master deciding AMBIENT, LOCAL (which slave) or BUILDING (both). Everything is simulation and host testing; **nothing has run on real hardware or in a browser.**

| | Result |
|---|---|
| Storey localisation | **Failed** both pre-stated gates (top-1 0.452 and 0.481 within-one for gate 1; alarm rate 0.770 against 0.80 for gate 2). Not claimed |
| Retraining on building data | **Keep the old tree**: the new one had a higher false-alarm rate (0.051 against 0.007) |
| Network protocol | Beacon/poll/report with retries, restart epochs and dead-slave detection; passed two rounds of independent verification |
| Master decision (rule b, T = 3) | Test block: LOCAL event recall 0.453, BUILDING 0.811, **2.04 false onsets per hour** (design budget was 1.0; probably not met) |
| Against one node | Same false-alarm rate as one ratio node; adds event type and a second floor, not sensitivity |
| Dashboard | Replay and live-USB viewer exist; no cloud broker |

Full details and caveats: [docs/hierarchy_validation.md](docs/hierarchy_validation.md).

## Blocked on hardware (Phase 6)

- Real inference timing and boot-time sensor configuration check (`config ok dlpf=3 accel=0 gyro=0`).
- Accuracy on real structures, and the real sensor's noise level compared with the synthetic data.
- Sensor mounting orientation (the model assumes `az` is the gravity axis).
- For the three-board system: radio link loss and timing, compute time against the 50 ms limit, the noise gate value, and the dashboard's live mode. The measurements are listed in [docs/hardware_bringup.md](docs/hardware_bringup.md).

## Project history

Phases 1 to 5 were each built on their own branch, independently verified, then merged into `master` in order with `--no-ff`, so each phase appears as a merge commit. The hierarchy work (R1 to R6) is on `hier2-protocol`, which is not merged into `master`.

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
| `models/training/` | Training and selection (`train.py`), model comparison, sensor-filter check, smoothing evaluation; localisation gates, building-data retraining (`train_building.py`), master-rule selection (`fusion_eval.py`, `fusion_eval_r2b.py`), single-node comparison (`compare_hierarchy.py`) |
| `models/selected/` | Deployed model, export for C, feature specification, reports and validation results (including gate, fusion and hierarchy results) |
| `models/selected_building/` | Building-data retraining candidate and its report (not deployed) |
| `firmware/esp32/` | PlatformIO project: live firmware, self-test firmware, network firmware (`master`, `slave1`, `slave2`; `include/net_proto.h`, `tiers.h`, `fusion.h`, `espnow_logic.h`), network simulator (`netsim`), PC tests, code generators (`tools/`), Wokwi configs (`wokwi/`: one folder per board of the 3-ESP32 system, a wiring view of all three, and the earlier single-board configs) |
| `dashboard/` | `index.html` event-log viewer (replay and live over USB) and sample logs |
| `docs/` | `simulation_validation.md` (Phases 1 to 5), `hierarchy_validation.md` (R1 to R6 report), `hardware_bringup.md` (flashing and measurements for the three boards) |

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

Hierarchy studies (from the repository root; some take minutes to an hour, see the report's section 12):

```
.venv\Scripts\python.exe models\training\test_gates.py
.venv\Scripts\python.exe models\training\localisation_gate_v2.py
.venv\Scripts\python.exe models\training\fusion_eval_r2b.py
.venv\Scripts\python.exe models\training\compare_hierarchy.py
```

Dashboard: open `dashboard/index.html` in Chrome or Edge.

The commands to reproduce Phases 1 to 5 are in [docs/simulation_validation.md](docs/simulation_validation.md#10-reproducing-the-results); those for the hierarchy stage are in [docs/hierarchy_validation.md](docs/hierarchy_validation.md#12-how-to-reproduce).
