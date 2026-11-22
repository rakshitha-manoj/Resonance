# Viva preparation

Companion to `project_report.md` (section numbers below refer to it). Golden rule for the viva: **say "simulated" every time you quote a result, and never claim a hardware measurement.** Admitting a limit calmly scores better than defending an inflated claim.

## One-page elevator pitch

**What.** A three-tier IoT/TinyML system for structural health monitoring: two ESP32 + MPU6050 sensor nodes, an ESP32 master that fuses them, and a cloud layer (MQTT, dashboard, alerts).

**Why.** Raw vibration streaming is costly (8.64 MB/h for two nodes, simulated) and fails when the network fails. Doing the analysis on the node keeps the system working locally; the radio carries small reports (about 0.4 MB/h for the hierarchy) and an events-only uplink is about 10-14 kB/h (the default build also streams one sample line per superframe, about 0.7 MB/h, for the live dashboard).

**How.**
- Each node computes features, runs a 110 B decision tree, an int8 autoencoder (942 B of weights, trained on healthy data only) and a natural-frequency tracker.
- The master fuses both nodes (rule f = std-ratio OR autoencoder) into AMBIENT / LOCAL / BUILDING and runs a DAMAGE alarm on the pooled natural-frequency drop.
- ESP-NOW between boards, MQTT with TLS to the cloud.

**Results (all simulated, fresh pre-registered test block of 240 buildings, 28 h):**
- Tree about 93 % accuracy on synthetic data, 110 B (target 90 % / 100 KB).
- Adding the autoencoder: mean event recall 0.622 -> **0.734**, for +0.25 false onsets/h.
- False onsets 1.64/h, **budget of 1/h not met** (2.05/h on healthy ambient alone).
- DAMAGE: 30 % stiffness loss in storeys 1/3/5 detected 1.00/1.00/0.91; top storeys almost invisible (physics).
- C kernel bit-exact with TensorFlow Lite on the PC: 0 mismatches on 10,000 vectors.
- Not done: hardware measurement. Everything is simulation; bring-up plan is written.

## Likely questions and short answers

**1. What is TinyML?** Machine learning inference on microcontrollers with kilobytes of memory. Here: a decision tree (110 B) and an int8-quantised autoencoder (942 B weights, 574 parameters) in hand-written C.

**2. Is this really TinyML?** By the constraints, yes: a trained neural network, post-training quantised to int8, under 1 KB of weights, compiled into the ESP32 slave firmware. It has not run on a board yet (hardware pending); the C kernel was verified bit-exact against TFLite on the PC. It does not use the TFLite Micro runtime; a TFLM comparison is future work.

**3. Why an autoencoder and not a classifier?** A classifier needs labelled faults; for a real structure we have none. The autoencoder is trained on healthy data only and flags anything unfamiliar. It catches frequency shifts (0.80 vs 0.30) and sparse impacts (0.20 vs 0.06) that the plain amplitude rule misses.

**4. Why does the autoencoder not replace the amplitude rule?** Its training data included level changes, so it reconstructs a louder building well: building-wide events 0.36 vs 0.80. So we OR the two (rule f).

**5. What is int8 quantisation?** Weights and activations stored as 8-bit integers with a scale, instead of 32-bit floats. Smaller and integer arithmetic. Cost here: +4.2 % reconstruction error, mean recall 0.608 float vs 0.600 int8 (validation).

**6. How do you know the C code matches TensorFlow?** Same 10,000 int8 input vectors through the C kernel and `tf.lite.Interpreter` reference kernels: 0 mismatching outputs of 200,000. Two rounding details mattered: TFLite rounds once in its fixed-point multiply, and the input quantiser is `round_half_away(v / scale)`; the `v * (1/scale)` form differs at near-half values.

**7. Why did you not meet the false-alarm budget?** The budget (1/h) was our own target. On the fresh block rule f gave 1.64/h [0.75, 2.79] and the incumbent 1.39/h; on healthy ambient only 2.05/h. Dev gave 0.89/h, a lucky draw. We reported it as not met. Raising thresholds or debounce lowers it at a recall cost (dev: f at T=4, k=3 gives 0.21/h, mean 0.676).

**8. Then why deploy rule f?** A criterion fixed in advance (pre-registration): deploy f if the recall gain's interval excludes 0 and the extra false onsets are <= 0.5/h. Gain +0.113 [+0.103, +0.122]; extra false onsets +0.25/h [+0.04, +0.50]. Both held.

**9. What is pre-registration and why?** Writing the test, the candidate and the decision rule down and committing them before looking at the test data. It stops us from picking the result that looks best afterwards (selection bias). Ours is `r3_test_preregistration.md`, committed before the 6,400,000 block was simulated.

**10. What are dev and test blocks?** Disjoint sets of simulated buildings (different random seeds). All tuning is on dev; the chosen setting is scored once on test. The test block is then "used up". Seed table: report Appendix C.

**11. Did you ever get a wrong criterion?** Yes: round R2 maximised per-superframe accuracy and allowed 126.5 false events/h. Found on its test block; criterion replaced by an event-level one with a false-onsets-per-hour budget; fresh blocks used. It is in the report on purpose.

**12. Why can't you detect damage at the top storeys?** Physics: mode 1 has little strain energy in the top storeys, so stiffness loss there barely moves f1. 30 % loss: f1 drops 3.88 % at storey 1 but 0.09 % at storey 10. Any f1-based detector is limited this way. Multi-sensor mode-shape methods are future work.

**13. How well does DAMAGE work?** Coarse. Mean detection 0.217 over 25 storey/loss cells; 30 % loss at storeys 1/3/5: 1.00/1.00/0.91; 20 % at storey 1: 0.84; 5-10 %: 0.01-0.07 at best. Latency 1-2 minutes. False DAMAGE 0.41 per 24 h at 0.5 % stiffness drift, 2.24 at 1 %.

**14. What did the post-hoc fix change?** Independent verification found mode-2 locks on low floors of low-f1 buildings after the test was scored. We added a unimodality check and a +-20 % band gate without re-selecting parameters. Results on cached blocks are essentially unchanged; we disclose that the test numbers are therefore not a clean one-shot.

**15. Why do you track f1 relative to its own commissioned value?** A real structure's true f1 is unknown, so only the change from its own commissioned value is meaningful. In simulation the absolute error is small (median 0.025 Hz, p99 0.12 Hz on floors 5/10), but mode-2 locks on low floors and per-building offsets make absolute thresholds fragile. The price: it only sees changes after commissioning.

**16. What is the role of the master?** It keeps the 1.4 s clock (beacon), polls both slaves, learns each slave's baseline, fuses the two reports into AMBIENT/LOCAL/BUILDING, runs the DAMAGE detector, declares dead slaves, and publishes JSON to the cloud. It has no sensor.

**17. Why a hierarchy at all?** It was a mentor requirement, and two slaves are the minimum for separating local from building-wide. Honest result: same false-alarm rate as one ratio node (2.54 vs 2.41/h) and no gain on one floor; it adds the second floor (0.487 vs 0.022), event typing and survival of a dead slave.

**18. Why ESP-NOW?** Connectionless, no access point, low latency, so the sensor network keeps running with the router off. Frames are small (56 B report), CRC checked, with a master epoch to survive restarts.

**19. What happens if Wi-Fi/the internet goes down?** The master keeps polling and fusing; the uplink runs on core 0 behind a non-blocking queue that drops oldest samples first and keeps events; it reconnects with back-off, retains `meta`/`state`/`status`. Residual risk (unverified on hardware): re-join scans may take the radio off the ESP-NOW channel for about 1.5-2 s, so a missed poll is possible.

**20. What if a slave dies?** After 3 missed superframes (about 4.2 s) it is DEAD; master continues in DEGRADED mode (LOCAL or AMBIENT only, never BUILDING) and emits a `node` event. A single node would be silent.

**21. What if the master reboots?** A fresh random epoch is sent in every frame; slaves resync at once. Restart recovery in the simulator: first fresh report within about one superframe. The master loses its baselines (RAM) and recommissions.

**22. What if a slave reboots on a damaged structure?** It recommissions on the damaged state and reports drop 0, hiding the damage. Baselines are RAM-only; NVS persistence is future work. The master logs a BASELINE note when f1_0 resets.

**23. How much memory and compute?** Models: tree 110 B, autoencoder 942 B (528 int8 MACs). Slave build: 772,825 B flash, 72,788 B static RAM. Compute: PC-measured about 30-45 us per window for the whole slave pipeline; ESP32 **estimated** 3-8 ms (unmeasured); the older tree pipeline was 24 ms in Wokwi, which is a simulator figure. Target was < 50 ms.

**24. Do you meet the 50 ms target?** Not yet shown for the full pipeline. The original tree pipeline took about 24 ms in Wokwi (simulated time). The full pipeline has not run on a board or in Wokwi: scaled from Wokwi's tree timing it would be about 45 ms (right at the limit), while instruction counts suggest 3-8 ms on a real 240 MHz ESP32. The self-test prints FITS/EXCEEDS and is the first hardware-day check.

**25. Why simulation, not hardware?** The boards were not available during development. We therefore made the simulation as strict as possible: bit-exact parity C vs Python vs TFLite, fresh blocks, pre-registration, and a written bring-up plan with pass criteria. We do not claim hardware results.

**26. Why is the noise gate a risk?** 92.12 counts is derived from synthetic data. Without it the autoencoder rule gives about 17 false onsets/h (dev). It must be re-measured on the real sensor (`n` command, bring-up section 7).

**27. Is the public broker secure?** No: anyone knowing the site id can read the topics. It is for simulated demo data only. The master firmware connects with TLS on port 8883 using a committed root CA (Amazon Root CA 1), but this ESP32 build does not check certificate expiry. The demo path (`sim_publisher.py` over `ws://...:8000`) is unencrypted; only the master firmware uses TLS 8883. A private broker with TLS and authentication is a configuration change. ESP-NOW peers are trusted on first use (guards against accidents, not attackers).

**28. How was it verified?** 253 unit tests (host), bit-exact parity chains on the PC (tree 6000/6000, AE 0/10,000 mismatches, fusion 3157 decisions 0 mismatches), a network simulator with fault injection, and separate verification passes per phase (which found real bugs: restart blackout, quantiser rounding, mode-2 locks).

**29. What are the main limitations?** Simulation only; uniform shear building and synthetic excitation; provisional noise gate; RAM-only commissioning (nothing detects a bad one); false-onset budget not met; DAMAGE limited to large lower-storey loss; no temperature compensation; no hardware numbers.

**30. How accurate is the tree really?** About 0.93 on synthetic data (resampling estimate of the procedure, not an untouched test; set-aside recordings influenced a selection revision). Real-structure accuracy is unknown.

**31. Why a decision tree and not a bigger network?** A selection rule (revised twice during selection, documented) picked the smallest model with CV accuracy >= 0.90; the tree won against 23 alternatives (forest 0.94 at 11.5 KB, SVMs, MLP). Notably, the master's final rule does not use the tree at all: ratio and autoencoder rules did better at the event level.

**32. Why is the energy budget a problem?** Slaves listen for beacons and sample at 100 Hz continuously: roughly 100 mA (datasheet-based estimate), so a 2000 mAh battery lasts about a day. The design assumes USB/mains power. Light sleep between slots is future work.

**33. What would you do with three more months?** Hardware bring-up and the measurement list; record real labelled data and re-fit the gate; NVS baselines; temperature compensation of f1; a small lab frame with adjustable stiffness; mode-shape localisation; TFLM comparison; private broker and OTA.

**34. What is the single most important thing you learned?** That choosing the metric matters: per-superframe accuracy hid 126 false alarms per hour; fixing the criterion and the test in advance made the results trustworthy even where they are unflattering.

**35. Which axis does your sensor measure?** The simulator puts the lateral (sway) floor acceleration on `az`, on top of the 1 g gravity offset, but the bring-up guide mounts `az` as the vertical (gravity) axis, which would see little of a shear building's sway. Which axis to use, and whether the thresholds transfer, must be settled at hardware bring-up. This is an open issue, not a solved one.

## Numbers to remember

| Quantity | Value | Source / note |
|---|---|---|
| Window / superframe | 128 samples at 100 Hz = 1.28 s; 1.4 s cycle | design |
| Tree | 11 nodes, 110 B, about 0.93 accuracy | synthetic, resampling |
| Autoencoder | 20-12-2-12-20, 574 params, 942 B, 528 int8 MACs | |
| C vs TFLite | 0 mismatches / 10,000 vectors | bit-exact, on the PC |
| Report frame | 56 B (wire v3); beacon 17 B, poll 18 B | |
| DEAD rule | 3 missed superframes, about 4.2 s | simulated |
| R3 test: rule f | 1.64 false onsets/h [0.75, 2.79]; mean recall 0.734 | simulated, scored once |
| R3 test: rule b | 1.39/h; mean recall 0.622 | |
| Paired f - b | +0.113 recall; +0.25 false onsets/h | |
| Healthy-only false onsets | f 2.05/h, b 1.74/h | budget 1/h NOT met |
| Sparse impact / freq shift (f vs b) | 0.20 vs 0.06 / 0.80 vs 0.30 | |
| Hierarchy vs one node | 2.54 vs 2.41 false onsets/h; other floor 0.487 vs 0.022 | rule b |
| DAMAGE params | M=32, C=64, delta 2 %, P=32 (45 s) | |
| f1 drop for 30 % loss | 3.88 % (storey 1), 2.42 % (5), 0.09 % (10) | physics |
| DAMAGE detection, 30 % loss, storey 1/3/5/8/10 | 1.00 / 1.00 / 0.91 / 0.03 / 0.00 | test block |
| False DAMAGE per 24 h | 0.41 (0.5 % drift), 2.24 (1 % drift) | |
| Slave build | 772,825 B flash; 72,788 B RAM | pio run |
| Compute | PC about 30-45 us; ESP32 estimated 3-8 ms (unmeasured) | |
| Tests | 253 unit tests passing (listed in report section 9) | |
| Seeds | R3 test 6,400,000+240; f1 test 6,600,000+120 | Appendix C |
