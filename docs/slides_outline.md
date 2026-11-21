# Slide outline (14 slides, about 15 minutes)

Say "simulated" on every results slide. Figures are in `docs/figures/`.

**1. Title.** Structural Health Monitoring using IoT and TinyML. Names, guide, date. Figure: `dashboard_overview.png` as a faded background.

**2. Problem and objectives.**
- Structures age and get damaged; early detection saves cost and lives.
- Edge processing: no raw streaming (8.64 MB/h vs about 10.5 kB/h of events, simulated).
- Targets: accuracy >= 90 %, < 50 ms, model < 100 KB; hierarchy requirement.
- Figure: none (or a simple building sketch).

**3. System architecture.**
- 2 sensor nodes (ESP32 + MPU6050), 1 master (ESP32), cloud (MQTT, dashboard, Telegram).
- ESP-NOW between boards, MQTT over TLS to the cloud.
- Figure: the Mermaid diagram of report section 3.1.

**4. One 1.4 s superframe and the protocol.**
- Beacon, 128 samples at 100 Hz, poll at 1.32 s, 56 B report.
- Epoch for restart recovery; DEAD after 3 missed superframes (4.2 s).
- Figure: timeline sketch.

**5. Sensing and features.**
- Window 128 samples; DLPF mandatory; 5 tree features; 16 band powers; Hann Goertzel zoom 3-12 Hz.
- Noise gate (provisional, 92.12 counts).
- Figure: none, or a spectrum sketch.

**6. TinyML models.**
- Decision tree: 110 B, about 0.93 accuracy (synthetic).
- int8 autoencoder 20-12-2-12-20, 574 parameters, 942 B, healthy data only, per-node commissioning.
- Mahalanobis baseline considered.
- Table: report section 5.4 benchmark.

**7. Making the C code trustworthy.**
- Bit-exact vs TFLite: 0 mismatches / 10,000 vectors; the rounding gotchas.
- Tree 6000/6000; fusion 3157 decisions 0 mismatches.
- Python evaluations run the firmware C code.

**8. Master: fusion rule f.**
- Rule b (std ratio >= 3) OR autoencoder (ratio >= 6), per slave, smoothed 2-of-3.
- LOCAL / BUILDING / AMBIENT; DEGRADED if a slave dies.
- Figure: `dashboard_overview.png` (event history with detector tags).

**9. Honest selection method.**
- Dev block, pre-registered criteria, fresh test block scored once; the wrong first criterion (126.5 false events/h) kept in the record.
- Figure: none; a small flow diagram of dev -> pre-register -> test.

**10. Results: rule f vs b (simulated, fresh block).**
- Mean recall 0.734 vs 0.622 (+0.113 [+0.103, +0.122]); false onsets 1.64 vs 1.39/h.
- 1/h budget NOT met (healthy-only 2.05/h); sparse impact 0.20 vs 0.06, freq shift 0.80 vs 0.30.
- Table: report section 6.4.

**11. Damage detection by natural frequency.**
- Damage lowers f1; mode-1 strain energy sits in the lower storeys.
- 30 % loss: storeys 1/3/5 detected 1.00/1.00/0.91; top storeys nearly invisible.
- Figures: `f1_drop_vs_damage.png`, `detection_vs_damage.png`.

**12. Live demo / dashboard.**
- Simulated 7.5-min scenario: impact, building-wide shaking, frequency shift (AE only), DAMAGE alarm at superframe 235.
- MQTT live + Telegram message.
- Figures: `dashboard_damage.png`, `f1_timeseries_example.png`. Runbook: `demo_runbook.md`.

**13. Resources and limitations.**
- Slave 772,825 B flash, 72,788 B RAM; ESP32 compute estimated 3-8 ms (unmeasured).
- Power: about 100 mA slave (estimate), USB-powered; BOM about INR 2,300-4,000 (approximate).
- Limits: simulation only, uniform shear building, provisional gate, RAM-only commissioning, budget not met.

**14. Conclusion and future work.**
- Complete, verified edge-IoT pipeline in simulation; rigorous, honest evaluation.
- Next: hardware bring-up, real data, NVS baselines, temperature compensation, lab frame, mode-shape localisation.
- Thank you / questions. Backup slides: physics table (storey x loss), packet-loss table, seed blocks.
