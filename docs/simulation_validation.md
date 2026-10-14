# Resonance: Simulation Validation Report (Phase 5)

Status as of 14 October 2026. This report covers the simulation stage of Resonance: everything that can be checked without the physical ESP32 and MPU6050. Real hardware (Phase 6) has not arrived yet. Two Wokwi runs of the Phase 5 firmware are still to be recorded (§8).

Sizes use 1 KB = 1000 B.

## 1. Summary

Resonance classifies a structure's vibration as **normal**, **abnormal** or **faulty** on an ESP32 using MPU6050 accelerometer and gyroscope readings.

| Target | Result in simulation | Status |
|---|---|---|
| Accuracy ≥ 90% | About **93%** (procedure estimate on synthetic data, see §3) | Met on synthetic data; real-structure accuracy is untested |
| Inference latency < 50 ms | About **24 ms** per window (Wokwi simulated time) | Met in simulation; real timing is untested |
| Model footprint < 100 KB | **110 B** model; whole on-device pipeline adds **+2.6 KB flash, +0.6 KB RAM** | Met |
| C firmware matches the Python model | **6000/6000** windows on PC, **606/606** on the simulated ESP32 | Confirmed |

**Hard requirement found in this phase:** the MPU6050's internal low-pass filter (DLPF, 44 Hz accel / 42 Hz gyro) must be enabled. In an independent check with simulated wideband sensor noise, turning it off flipped about 16% of predictions (measured once in independent verification, not yet reproducible from the repository, §8). The firmware now refuses to classify if the filter or sensor ranges read back wrong at boot (§5).

## 2. What runs on the device

1. Read the MPU6050 every 10 ms (100 Hz), on a fixed schedule.
2. Buffer 128 samples (1.28 s) of the `az` and `gx` axes only, in non-overlapping windows.
3. Compute 5 features: `az` standard deviation, kurtosis and dominant frequency; `gx` standard deviation and dominant frequency.
4. Run a depth-3 decision tree (11 nodes, at most 3 comparisons), compiled to C as nested if/else.
5. Smooth the output: the reported state changes only when 2 of the last 3 windows agree (§6).
6. Print `window,class,state,feat_us,tree_us,span_us` over serial.

| Item | Value |
|---|---|
| Sensor settings | accelerometer ±2 g (16384 counts/g), gyroscope ±250 °/s (131 counts/(°/s)), DLPF CFG=3 (accel 44 Hz, gyro 42 Hz) |
| Sampling | 100 Hz, 128-sample windows, dominant-frequency resolution 0.78125 Hz |
| Features | std: population, mean removed, two-pass in float32. kurtosis: excess, population moments. dominant frequency: largest DFT bin 1–64 of the mean-removed window |
| Model | `DecisionTreeClassifier(max_depth=3)`, trained on all 150 synthetic recordings |
| Tree splits | `az_std ≤ 354.255`, `az_domfreq ≤ 13.671875`, `gx_std ≤ 59.1288`, `az_kurt ≤ −1.1916`, `gx_domfreq ≤ 15.625` |

The C tree (`firmware/esp32/include/model.h`) is generated from `models/selected/model_export.json` by `firmware/esp32/tools/gen_model.py`, not written by hand.

## 3. Model accuracy and how it was chosen

**Data.** 150 synthetic recordings (50 per class), each cut into 40 windows of 128 samples, 6000 windows in total (`data/generator/generate.py`). Train/test splits are always by recording, never by window, because windows from the same recording are near-duplicates.

**Accuracy estimate: about 0.93.**

| Estimate | Mean | Spread |
|---|---|---|
| 200 re-drawn test sets (10 recordings per class) | 0.9315 | sd 0.028, min 0.842; 176/200 draws ≥ 0.90 |
| 40 repeats of 5-fold CV over all 150 recordings | 0.9315 | sd 0.0075, min 0.906 |

This is an estimate of the training procedure, not of one held-out test. The set-aside test recordings were used as evidence when the selection rule was revised, so no untouched test data remains (`models/selected/report.md` records the full selection history). It is also accuracy on **synthetic** data. Hardware data in Phase 6 is the real test.

**Selection rule:** among models of at most 5,120 B, the smallest whose 5-fold CV accuracy on the 120 development recordings is ≥ 0.90 on all 10 fold assignments. Features exclude raw RMS, because RMS includes gravity and sensor offset, which depend on mounting and calibration.

**Alternatives compared** (`models/selected/comparison.md`). Under a replacement rule fixed before seeing results, none of 23 alternative configurations beat the depth-3 tree by enough to justify its cost:

| Model | Accuracy (re-draws / all-data CV) | Size | Outcome |
|---|---|---|---|
| Decision tree, depth 3 (selected) | 0.932 / 0.932 | 110 B | Selected |
| Random forest, 50 trees, depth 4 | 0.938 / 0.940 | 11.5 KB | Documented fallback; needs all 6 axes and 6 FFTs |
| SVM, RBF (best) | 0.911 / 0.912 | up to 93 KB | Worse |
| SVM, linear (best) | 0.907 / 0.907 | 372 B | Worse |
| Small neural network (best) | 0.87 on dev CV (selected tree: 0.922 on the same measure) | 364 B | Worse |

1D CNNs and LSTM/GRU networks were not tried: they need a larger runtime on the ESP32, and learning from raw synthetic signals risks learning artefacts of the generator. A CNN is worth revisiting in Phase 6 if the tree falls short on real data.

## 4. C firmware against the Python model

| Check | Result |
|---|---|
| PC test: C feature and tree code on all 6000 windows (`pio test -e native`) | 6000/6000 classes match; dominant frequency identical on every window |
| Simulated ESP32 (Wokwi self-test, Phase 4 build): 606 windows, made of 516 hard cases (503 near a split threshold, 13 where the two largest DFT bins are within 0.1%) plus 90 random | 606/606 classes match |
| Largest feature difference on the ESP32 vs Python float64 | `az_std` 5.5e-4, `az_kurt` 1.8e-5, `gx_std` 1.2e-4, dominant frequency 0 |
| Smallest distance of any window to a split threshold vs largest float error | ≥ 14.8× |
| Smoothing C code vs Python (PC test) | 2592 sequences, 86,015 steps, 0 mismatches |

Floating-point details that matter:
- All builds use `-ffp-contract=off`. Without it the ESP32 compiler emits fused multiply-add instructions in the project's code, which round differently from a PC. The disassembly of the project's own code was checked: none remain. The only fused instructions in the firmware image are inside the toolchain's float division and square-root routines (`__divsf3`, `__ieee754_sqrtf`); the 6000/6000 and 606/606 matches include their effect.
- Variance must be computed in two passes (mean first). A one-pass float32 formula flips predictions on `az`, whose readings sit near 16384.

## 5. Latency, memory and sampling

| Measure | Value | Target |
|---|---|---|
| Feature computation | 24.1 ms per window (self-test avg 24,103 µs, max 24,142 µs; live run 24,002–24,048 µs) | — |
| Tree evaluation | about 6 µs (self-test avg 6 µs, max 31 µs; live run 0–8 µs) | — |
| **Total inference** | **about 24 ms** | < 50 ms |
| Flash added over the Phase 1 sensor-streaming firmware | +2,560 B (296,829 B total) | < 100 KB |
| RAM added over the Phase 1 sensor-streaming firmware | +556 B (22,364 B total) | < 100 KB |
| Window span (first to last sample) | 1,270,000 ± 5 µs over 29 consecutive windows | 1,270,000 µs |

Wokwi figures are from the Phase 4 builds (self-test 5 Oct, live run 7 Oct). The feature and tree code (`features.h`, `model.h`) and the 606-window set are unchanged in Phase 5, and smoothing runs outside the timed region. Build sizes are from the Phase 5 build.

- By instruction count, about 99% of feature time is the two 128-point DFTs. A radix-2 FFT would need roughly 10–20× fewer operations (estimate, not measured) if hardware timing turns out tight. It would need re-verification.
- Samples within each window are 10 ms apart. Between windows there is an extra pause of about 24 ms (in Wokwi) while features are computed, so about 2% of the time is not sampled.
- The 2.6 KB / 0.6 KB covers the tree, the feature code, the DFT tables, the two sample buffers, smoothing, scheduling and the boot configuration check. The rest of the firmware is the Arduino core and MPU6050 library.

### Sensor filter (DLPF): a hard requirement

The training data never went through the MPU6050's filter, so its effect was emulated (`models/training/dlpf_check.py`): each recording was upsampled to the chip's internal 1 kHz rate, filtered, and read back every 10 ms.

| Filter emulation | Predictions changed (of 6000) | 5-fold CV accuracy |
|---|---|---|
| None (as trained) | — | 0.933 |
| 2-pole Butterworth, 44/42 Hz (main emulation of CFG=3) | 10 (0.17%) | 0.935 |
| 1-pole, 44/42 Hz | 35 (0.58%) | 0.937 |
| 2-pole Butterworth, 21/20 Hz (CFG=4, stress case) | 54 (0.90%) | 0.936 |

The CV column comes from its own run (10 repeats of grouped 5-fold, model trained on unfiltered data, tested on filtered), so its baseline 0.933 differs slightly from §3's 0.9315.

- The real filter design is unpublished. An independent verifier tried other plausible 44/42 Hz shapes (4-pole, Bessel, moving average, different read timing, unsynchronised clocks): **3–28 changed predictions (0.05–0.47%)**. Accuracy does not drop, and retraining on filtered data gave no gain.
- **Without the filter, the model is not reliable.** The same verifier regenerated the data at 1 kHz with content above 50 Hz and wideband noise. With the filter on, 10–13 predictions changed; with it off, 961–964 changed (about 16%) and accuracy fell to 0.82. The `gx_std` threshold (59 counts) is sensitive to gyro noise. *Provenance: this was measured in the independent verification on 7 October 2026; that scratch script was not preserved, so this figure is not yet reproducible from the repository (see §8).*
- **Consequence in firmware:** at boot, the ESP32 reads back the DLPF setting and both sensor ranges. If any is not DLPF 44/42 Hz, ±2 g, ±250 °/s, it prints `config fail …` and does not classify. All thresholds are in raw counts at these ranges: other ranges change the count scale, and other DLPF settings change the signal.

## 6. Prediction smoothing

Rule: the reported state becomes a class once 2 of the last 3 windows agree on it; otherwise it holds. It is symmetric, so a single normal window cannot clear a real alarm either.

Measured with out-of-fold predictions (models that never saw the recording being tested), over 50 normal recordings and 100 sequences of 10 normal → 40 fault → 10 normal windows, averaged over 10 repeats (`models/training/smoothing_eval.py`):

| Measure | Raw | Smoothed |
|---|---|---|
| False-alarm episodes per normal recording | 0.50 | **0.17** (−65%) |
| Spurious state changes per fault sequence | 3.42 | **1.01** (−70%) |
| Faults detected (true class) within 5 windows | 93.9% | 92.6% |
| Extra detection delay from smoothing | — | +1 window (1.3 s) in 87% of faults; 0.1% missed only due to smoothing |
| Return to normal after a fault ends (median) | 0 windows | 1 window |
| Accuracy, all windows | 0.938 | 0.927 |
| Accuracy, excluding the first window after each change of condition | 0.939 | **0.949** |

- Raw accuracy here (0.938) is above the 0.93 headline because half of this test's windows are normal, the easiest class.
- The overall accuracy drop is entirely the one-window lag at the start and end of each test fault, and the test has a change of condition every 30 windows. On this test's error rates, smoothing gives higher overall accuracy when changes of condition are on average more than about 84 windows (about 110 s) apart.
- Smoothing cannot fix sustained misclassification. The false alarms that remain all come from 4 of the 50 normal recordings, which the tree misclassifies in runs (25–89% of their windows wrong, out-of-fold).

## 7. What simulation can and cannot prove

| Wokwi / PC simulation proves | It does not prove |
|---|---|
| The firmware logic runs continuously and prints a result every window (Phase 4 firmware; Phase 5 run pending, §8) | That classifications are correct on real vibration: Wokwi's MPU6050 returns static slider values, so the live run always reports `normal` |
| The compiled ESP32 code gives the same answers as the Python model on actual windows from the synthetic dataset (self-test) | Real inference speed: 24 ms is a simulator figure, not a prediction of real timing |
| Sampling is evenly spaced in simulated time | Real I2C timing, sensor register update timing, or electrical noise |
| The feature and tree maths is numerically faithful in float32 | Accuracy on real structures: all training and test data is synthetic |
| The model tolerates the emulated DLPF | The real DLPF's exact response, the real sensor's noise level, mounting orientation, axis rotation, gain errors or temperature drift |

## 8. Status of this phase's own checks

| Check | Status |
|---|---|
| DLPF sensitivity script (`dlpf_check.py`) | Built, independently re-implemented and reproduced |
| Smoothing code and evaluation | PC tests pass; evaluation independently reproduced |
| Wokwi run of the Phase 5 self-test (120-window smoothing sequence) | **Not yet recorded.** Expected output: `seq raw match 120/120, state match 120/120`, `seq raw class changes 6, state changes 4`, `seq done PASS` (from a PC emulation of the same code) |
| Wokwi run of the Phase 5 live firmware (`config ok dlpf=3 accel=0 gyro=0`) | **Not yet recorded.** Wokwi's support for reading these registers back is unknown; a `config fail` in simulation would be a simulator limitation to handle separately |
| Wideband-noise / no-DLPF figure (§5) | Measured once in independent verification; needs a committed, reproducible script |

## 9. Carried into Phase 6 (hardware)

- Confirm `config ok dlpf=3 accel=0 gyro=0` at boot on the real sensor.
- Measure real `feat_us`, `tree_us` and `span_us`; switch to a radix-2 FFT if latency is tight.
- Mount the sensor so `az` is the gravity axis; record the orientation used.
- Log raw data from real structures to measure sensor noise against the synthetic noise level. `gx_std ≤ 59` is the most noise-sensitive split.
- Measure accuracy on real normal, abnormal and faulty conditions. That will replace the synthetic ~0.93 estimate.
- If the tree falls short on real data: try the documented fallback (50-tree forest, 11.5 KB), then a 1D CNN.

## 10. Reproducing the results

Run from the repository root with the project venv (`.venv`, Python 3.12, `requirements.txt`):

```
.venv\Scripts\python.exe data\generator\generate.py
.venv\Scripts\python.exe models\training\train.py
.venv\Scripts\python.exe firmware\esp32\tools\gen_model.py
.venv\Scripts\python.exe models\training\compare.py
.venv\Scripts\python.exe models\training\compare_svm.py
.venv\Scripts\python.exe models\training\dlpf_check.py
.venv\Scripts\python.exe models\training\smoothing_eval.py
.venv\Scripts\python.exe firmware\esp32\tools\gen_testdata.py
```

`train.py` takes about an hour. From `firmware/esp32`, with WinLibs GCC on `PATH` for the PC test:

```
pio test -e native
pio run -e esp32dev
pio run -e selftest
```

Wokwi: open `firmware/esp32` in VS Code, run "Wokwi: Select Config File" (`wokwi.toml` for live, `selftest/wokwi.toml` for the self-test), then "Wokwi: Start Simulator".
