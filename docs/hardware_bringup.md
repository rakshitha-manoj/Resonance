# Hardware bring-up and measurements (R5)

Three ESP32 DevKit v1 boards: **master** (id 0, no sensor), **slave 1**, **slave 2** (one MPU6050 each).
Everything below runs from `firmware/esp32`. `pio` = the PlatformIO CLI (on Windows usually `%USERPROFILE%\.platformio\penv\Scripts\pio.exe`).
Nothing here has been run on hardware yet: the pass criteria are the targets, and the "if it fails" notes say which constant to change.

## 1. Wiring (each slave)

| MPU6050 | ESP32 DevKit v1 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | GPIO 21 |
| SCL | GPIO 22 |

AD0 to GND (address 0x68). The master has nothing attached. Optional: GPIO 23 on each slave to a scope/logic analyser (build with `-DSYNC_PIN=23`) for the window-start comparison in 5.5. Mount the sensor with **az as the gravity axis**, rigidly on the structure.

## 2. Flash (one board at a time; find the COM port with `pio device list`)

```
pio run -e master  -t upload --upload-port COM5
pio run -e slave1  -t upload --upload-port COM6
pio run -e slave2  -t upload --upload-port COM7
pio device monitor -p COM5 -b 115200
```
If upload hangs at "Connecting...", hold the BOOT button until it starts.

Expected serial output after reset:

- Slave: `boot ok ... role=slave id=1`, then `config ok dlpf=3 accel=0 gyro=0`, then `espnow: ready channel=1 mac=AA:BB:.. ps=0 (none)` (`ps` is read back from the Wi-Fi driver; a `WARNING power save is ON` there adds receive latency), then the `# W,...` header. `mpu fail` or `config fail ...` halts the board: check wiring/power.
- Master: `boot ok ... role=master id=0`, `espnow: ready channel=1 mac=..`, `master epoch=xxxxxxxx` (different on every reset).

Write down the three MACs. Optional allowlist: build with `-DNODE0_MAC="{0x..,...}"` (and `NODE1_MAC`, `NODE2_MAC`). Without it the first valid frame from each id pins its MAC until reboot (trust on first use). This is a guard against accidents (two boards with the same id, a stray node), **not authentication**: ids and MACs can be forged. Replacing a board means rebooting the others.

## 3. First contact

Power master and both slaves (any order). Within about 3 s the master prints, every 1.4 s:

```
R,12345,1,8,7,0,0,1,..   (t_ms, slave, sf, window, raw, state, flags, std_az, kurt_az, dom_az, std_gx, dom_gx, ae_ratio, f1_est, f1_drop)
R,12361,2,8,7,0,0,1,..
```
Slaves print one `W,...` line per window. Pass: both slaves report every superframe, no `E,...,DEAD`, `flags` mostly 1 (LOW_SIGNAL: sensor still) and bit 0x2 (UNSYNCED) not set (flags = 3 is also UNSYNCED). If a slave prints nothing: check the channel is the same on all boards, and that a `S,rx_reject` line does not show `mac_mismatch` (a second master/slave with the same id nearby). Pull the power on one slave: master must print `E,..,DEAD` after about 4 s and `E,..,ALIVE` after it returns.

## 4. Log everything

```
pio device monitor -p COM5 -b 115200 --filter log2file
```
Keep the master and both slave terminals open (3 monitors, 3 log files). The master prints statistics every `STATS_EVERY` = 20 superframes (28 s): lines starting `S,`.

## 5. Measurements and pass criteria

Run 5.1 to 5.5 for 5 minutes still, then 5.6 for at least 2-3 hours (overnight if possible; see 5.6 for why 30 minutes is not enough).

| # | What | Where to read it | Pass |
|---|---|---|---|
| 5.1 | Compute time (features + classify + smoothing, and since Phase C2a the AE, the zoom and the f1 tracker: the whole slave pipeline, see 7b) | slave `W` line, `compute_us` (8th field after `W`) | max well under 50 000 µs (target < 35 000) |
| 5.2 | Poll round trip (timed from the first poll of the superframe) | master `S,rtt_ms_hist` and `S,rtt_late` lines (`max_us`, buckets, `after_retry`, `over_timeout`) | max round trip under about 9 ms and retries about 0 |
| 5.3 | Master tick lateness | master `S,max_tick_late_us` | < 5 000 µs (below `BEACON_GRACE_US`) |
| 5.4 | Sample span with radio on | slave `W`, `span_us` | 1 270 000 ± 200 µs every window; `max_miss_us` < 200 |
| 5.5 | Slave start offset | slave `W`, `bo_us` on both slaves; with `SYNC_PIN` the edge time difference on the scope | difference between slaves < 1 ms; ignore windows with the UNSYNCED bit (0x2) set in `flags` (free-run windows have no beacon anchor) |
| 5.6 | Link loss, 2-3 h or more (about 2570 superframes per hour) | master `S,..polls=`, `retries=`, `reports=`, `missed=`, `beacons=`, `E` and `R` lines | see below |
| 5.7 | Noise level | slave command `n` | sets the gate, see 6 |

Notes:
- `queued` (last `W` field) and `peak_queue` (slave `S` line) show how many frames waited during sampling; `queue_overflow` must stay 0 (queue depth 16: at most 9 legitimate frames per superframe, 1 early next beacon, 6 for foreign frames).
- `bo_us` is the delay between the beacon arriving in the receive callback and the first sample. It is only the software part of the offset; the scope comparison is what shows the real slave-to-slave difference.
- 5.2 reads the histogram from the first poll (attempt 0) of each superframe, so a late reply to attempt 0 is not hidden by the retry. `after_retry` = reports that arrived after the retry had been sent; `over_timeout` = reports whose round trip was at least `REPLY_TIMEOUT_US`. Replies arriving after polling closed are never timed: the master counts them as `rx_stale` (printed on the same line as `rx_stale_after_close`). Pass: `after_retry`, `over_timeout` and `rx_stale` all about 0.
- 5.6 has three different numbers, do not mix them up:
  1. `missed_total / beacons` per slave is the miss rate **after retries** (both attempts failed), not the link loss.
  2. Packet loss from the counters (per slave, `polls_tx` includes retries): the first-attempt failure rate is `q = retries / (polls_tx - retries)`, and a cross-check is `1 - reports / polls_tx`. Each exchange needs a poll and a reply, so per-packet (one-way) loss is `p = 1 - sqrt(1 - q)`.
  3. The false-DEAD estimate in `.claude/STRUCTURE.md` (about 0.1 per slave-hour at 10 % i.i.d. per-packet loss, about 5 per hour at 20 %) is a function of this `p`. Compute `p` from (2) and compare.
- Statistics: 30 minutes with 2 slaves is only about 1 slave-hour, so zero DEAD events bounds the rate below about 3 per slave-hour (95 %, rule of three). Run at least 2-3 hours, overnight if possible.
- Run lengths of consecutive misses, from the master log (`R` lines; segments restart when the superframe number goes backwards, i.e. after a master restart; misses before a slave's first report and after the last report of the segment are included, misses after the last `R` line of the whole log are not visible):

```python
import sys, collections
segs, cur, last = [], collections.defaultdict(set), -1
for l in open(sys.argv[1], errors="ignore"):
    p = l.strip().split(",")
    if p[0] != "R" or len(p) < 4: continue
    try: slave, sf = int(p[2]), int(p[3])
    except ValueError: continue
    if sf < last:                      # master restarted: new segment
        segs.append(cur); cur = collections.defaultdict(set)
    last = sf
    cur[slave].add(sf)
segs.append(cur)
runs, total, missed = collections.defaultdict(collections.Counter), collections.Counter(), collections.Counter()
for seg in segs:
    if not seg: continue
    lo, hi = min(min(v) for v in seg.values()), max(max(v) for v in seg.values())
    for s, got in seg.items():
        n = 0
        for k in range(lo, hi + 2):    # hi + 1 closes a run that reaches the end
            if k <= hi and k not in got: n += 1
            elif n: runs[s][n] += 1; missed[s] += n; n = 0
        total[s] += hi - lo + 1
for s in sorted(total):
    r = runs[s]
    print("slave", s, "superframes", total[s], "missed", missed[s], "longest run", max(r, default=0),
          "run-length distribution", dict(sorted(r.items())))
```
Pass: miss rate after retries < 2 % per slave, no run of `DEAD_AFTER_MISSES` (3) or more, no unexplained `E,..,DEAD`. A run of 3 or more is a false DEAD event (compare with the `E` lines).

Stats lines also show `rx_reject` (`bad_frame`, `mac_mismatch`, ...), `esp_now_send_fail`, `send_cb_fail`, `queue_overflow`: all should be 0 or near 0, `skipped` (beacons) 0.

## 6. If a criterion fails

| Failure | Change | Why |
|---|---|---|
| Compute > ~45 ms | Make `features()` faster (radix-2 FFT instead of the direct DFT: ~10-20x fewer operations); with the Phase C2a pipeline also the Goertzel zoom (every 2nd grid point, see 7b) | slave 1's first poll fails above ~50 ms compute; DEAD from ~68 ms. Do not relax timing instead. |
| Round trips long (max over about 9 ms, `after_retry` or `over_timeout` > 0) | Change the superframe and poll constants **together** (`SUPERFRAME_US`, `POLL_START_OFFSET_US`, `REPLY_TIMEOUT_US` in `net_proto.h`) and re-run `pio test -e netsim`. One constant alone does not work: with the current values `REPLY_TIMEOUT_US` must stay below 20 ms (1.32 s + 4 x T < 1.4 s, the static_assert on `POLL_WORST_END_US`), and `POLL_START_OFFSET_US` cannot come down (the sample span is 1.27 s plus up to 50 ms compute) | a reply later than the timeout is counted as a miss or `rx_stale`, and a longer timeout makes polling overrun the next beacon |
| Tick lateness > 5 ms | Find the stall (serial output blocking, other tasks). Only then raise `BEACON_GRACE_US` | lateness above the grace makes slaves free-run (flag UNSYNCED), harmless to classification but loses sync |
| Span off by > 200 µs, `max_miss` high | Span and miss are timestamped **before** the I2C read, so I2C duration does not show up in them. First replace the `vTaskDelay(1)` part of the wait in `sample_window` with a pure spin; then check interrupt load and printing during sampling. Only a single read taking over 10 ms (a bus fault) points to I2C. `Wire.setClock(400000)` is not the remedy | `vTaskDelay` wakes on the 1 ms RTOS tick and other tasks can delay the wake-up |
| Slave start difference > 1 ms | Compare `bo_us` between the slaves. If it varies, anchor both slaves at the beacon receive time plus a fixed lead (sample 0 at `beacon_rx + lead`, the same lead on both), instead of sampling as soon as the loop gets to it. Do not subtract `bo_us` from the window anchor: sample 0's deadline would already be in the past and the 10 ms spacing would be distorted | the fusion compares windows of the same superframe |
| Loss > 2 % | Move boards, check antennas away from metal, try another `ESPNOW_CHANNEL` (`node_config.h`) | busy 2.4 GHz channel |
| Longest run >= 3 | Raise `DEAD_AFTER_MISSES` in `net_proto.h` above the longest observed run (each +1 delays DEAD by 1.4 s) | otherwise false DEAD |
| `queue_overflow` > 0 | Raise `RX_QUEUE_DEPTH` in `espnow_logic.h` (item size is about 270 B) | foreign ESP-NOW traffic nearby |

The cloud link later: the master's Wi-Fi must be on the router's channel, so `ESPNOW_CHANNEL` of **all** nodes must be changed to that channel.

## 7. Noise estimate and the gate

Put slave 1 on the still structure, type `n` in its monitor. It samples 30 windows (about 40 s, network suspended) and prints:

```
N,windows=30,az_std_mean=..,az_std_sd=..,az_std_min=..,az_std_max=..,gx_std_mean=..,gx_std_max=..,gate_suggest_mean+5sd=..,current_gate=92.12
```
Repeat on slave 2. The gate must be above `az_std_max` of a still structure and below the weakest vibration you want classified. `NOISE_GATE_COUNTS` is a constexpr in `tiers.h`; a separate small change wires `-DNOISE_GATE_OVERRIDE=<value>` into it (until that lands the boot line prints a warning that the override is not applied, and you edit `tiers.h` and reflash). Set the flag **only in the slave envs' `build_flags`** in `platformio.ini` (`slave1`, `slave2`), never in the shared `[env]` section: the `native` and `netsim` envs inherit `[env]` and must keep the trained gate. The slave leaves the network state reset after `n`, and rejoins at the next beacon.

## 7b. AE and f1 on the slaves (Phase C2a: what to check on hardware)

From Phase C2a each slave also runs the int8 autoencoder and the first-natural-frequency tracker on every window, and the master runs the deployed rule f and the DAMAGE detector. All three boards must be flashed from the same build (wire version 3; a version-2 board is ignored). The report/`R,` line now ends with `ae_ratio,f1_est,f1_drop`, `flags` use six bits (1 low_signal, 2 unsynced, 4 ae_ready, 8 f1_valid, 16 f1_commissioned, 32 f1_not_commissioned) and the slave's `W,` line has the same three values appended. Nothing here has been measured on hardware; every simulation number is `building_sim`.

1. **Compute time first.** Run the `selftest` env on a board (or in Wokwi) and read `pipeline us (features+bands+AE+zoom+f1 tracker+tree) per window: ... -> FITS | EXCEEDS the 45 ms budget`; then on the real slave read `compute_us` of the `W,` line (it now times the FULL pipeline). Pass: max well under 50 000 us (slave 1's first poll fails above ~50 ms, DEAD from ~68 ms). Wokwi's timing is not silicon timing (instruction counts suggest roughly 3-8 ms on a real ESP32 for the whole pipeline, about 1.9 x the old feature time in Wokwi's model); only the board number counts. The zoom already runs four frequencies per pass (bit-identical). The host cost is dominated by the 181-point Goertzel zoom; if it does not fit, evaluating the zoom on every 2nd grid point (0.1 Hz) halves it with no measurable loss in the simulation study (`freq_tracking.md`), but that changes a selected quantity, so decide it consciously.
2. **Re-measure the noise gate (section 7) BEFORE trusting the AE.** The gate (92.12 counts, still a placeholder from the synthetic data) now also gates the AE flag: without it the AE rule gave 17.9 false onsets/h on the dev block, with it 0.89/h. A gate that is too low lets sensor noise reach the AE flag; a gate that is too high mutes both. The master applies the gate to the reported `std_az`, so a per-board `NOISE_GATE_OVERRIDE` on a slave does NOT change what the master does with the AE flag (it is the master's constant, `CHOSEN_AE_GATE` in `fusion.h`, checked against the selection JSON by `test_fusion`).
3. **Commissioning takes time and needs a quiet structure.** The AE commissions on the slave's first 20 windows after power-on (about 28 s; `ae_ratio` is 0 and AE_READY clear until window 21). The std baselines on the master use the first 20 reports too. f1 needs 32 windows to fill the ring, then 64 consistent valid estimates: **about 96 windows = 2.2 min** before `F1_COMMISSIONED` appears, longer if the estimates are inconsistent (spread of the last 64 above 10 %: the window keeps sliding). Any event or footfall in these minutes biases the baselines and the AE reference, and nothing detects that. Keep the structure quiet and do not reset a slave during a measurement: a slave reset restarts both commissionings (`reset()` resets the context), and a master reset restarts only the master's baselines. The slave's serial commands `n` (noise estimate) and `r` (record) end by resetting the slave, so they also recommission the AE and f1 (another 2.2 min). **All baselines are RAM-only** (f1_0, the AE normaliser and reference, the b baselines): a slave that reboots on a DAMAGED structure recommissions on the damaged state and reports drop 0 (the master logs a `damage` line with state BASELINE when it sees a slave's f1_0 reset); saving the baselines in NVS is future work for the hardware days.
4. **F1_NOT_COMMISSIONED (flag 32)** means commissioning is overdue after M + 4C = 288 windows (6.7 min): f1 outside 3-12 Hz, the first mode invisible from that floor, or an unstable estimate. Treat that slave as not contributing to DAMAGE (the master already does) and look at `f1_est` in the `R,` lines: a steady value is the first mode, a value jumping between two is mode 1 / mode 2.
5. **Slave placement.** Put the slaves on floors where mode 1 dominates the spectrum: on the lower storeys f1 moves most with damage (30 % stiffness loss at storey 1: f1 -3.9 %, at the top storey -0.09 %), and a LOW floor of a low-f1 building can lock onto mode 2. Avoid floors close to a node of mode 1; avoid mounting next to machinery with its own narrow-band line in 3-12 Hz. The DAMAGE channel is a coarse alarm for large damage in the lower storeys only (`freq_tracking.md`: 20 % stiffness loss 0.36 detection, 5-10 % about 0.01-0.03). The tracker compares each slave with its OWN commissioned f1_0 (the estimator has about +-0.1 Hz of per-structure bias), so only changes after commissioning are visible.
6. **What to log and compare.** `ae_ratio` of a still structure should stay around 1 (it is score / the median commissioning score) with occasional values of 3-5; sustained values >= 6 on one slave are an AE alarm (LOCAL), the master log's `ae` field shows the same numbers. In `f1_est` look for the 2-3 % window-to-window scatter that the simulation showed (about 0.025 Hz median error, p99 0.12 Hz at M = 32); a drifting f1 with temperature or load shows up as `drop` before any damage: DAMAGE needs the pooled drop of the slaves >= 2 % for 32 superframes (45 s). Expect false DAMAGE under strong environmental drift: the simulation's false rate was 0.41 per 24 h at 0.5 % drift and 2.24 per 24 h at 1 % (`freq_tracking.json`).
7. **Pass for an uneventful hour:** `S,rx_optional,ae_dropped=0,f1_dropped=0`, no `damage` line, `ae` and `f1` non-null in the `sample` lines from window 21 / 32 on, `F1_COMMISSIONED` on both slaves after about 2.2 min (or `F1_NOT_COMMISSIONED` with a reason), false LOCAL/BUILDING onsets near the simulated 1-2 per hour at most (the fresh-block rate of rule f was 1.64 [0.75, 2.79] per hour: the 1/h budget was NOT met in simulation, and real data will differ).

## 8. Labelled recording (real data)

On a slave type `r`. **The slave leaves the network** (no beacons processed, no replies; the master shows it DEAD after about 4 s; it rejoins when you press `r` again). Why: sampling and the schedule cannot be shared, and one window plus serial printing is longer than the poll gap. Windows are taken back-to-back, each printed as one CSV row after sampling:

```
D,<label>,<window>,az_0..az_127,gx_0..gx_127
```
Type `0` (still), `1` (shaken), `2` (tapped) to set the label for the following windows; discard the first window after a change. Suggested session: 5 min still, 5 min shaking by hand, 5 min repeated taps, per slave, at least twice on different days. Capture with `--filter log2file` and keep lines starting with `D,`. Stop with `r`.

## 8b. Cloud uplink and the local network (only when the master runs `master_mqtt`)

The master's Wi-Fi station shares the radio with ESP-NOW, so the local network must keep working when the cloud side misbehaves. Build with `secrets.h` (Wi-Fi credentials), flash the master (`pio run -e master_mqtt -t upload`) and both slaves, then check:
1. Boot: the master prints `wifi: connected ssid=... channel=N` (the join is done in the first seconds, before the first beacon) and `mqtt: connected`. If N is not `ESPNOW_CHANNEL` it prints a WARNING: build all three boards with `-DESPNOW_CHANNEL=N` or let the slaves scan (they cycle channels 1-13, 1.5 s each, and print `scan: locked on channel N`).
2. Router OFF test (the important one): with the system running (both slaves reporting, `alive=1/1` in the `S,sf=...` line), power the router off for 5 minutes. Watch the master's `S,` lines: `alive=`, `missed=` and the `E,` lines, `reports=`, `beacons_skipped`, and `S,mqtt,...` (`wifi=0`, rising `queued` / `q_drop_samples`, `joins=x/y` growing only every 30 s, 60 s, ... up to 5 min; `conn_fail/attempts`, `heap_free` / `heap_min` must stay stable). EXPECTATION: a re-join attempt with the saved channel + BSSID is only a hint; with the router absent the IDF still scans the other channels, taking the radio off the ESP-NOW channel for roughly 1.5-2 s per attempt, so an occasional missed poll or beacon (`missed=` rising by 1, a slave `free_runs`) is EXPECTED. What must NOT happen: a slave declared DEAD (3 consecutive missed superframes) or both slaves LOST. Switch the router on: `wifi: connected`, `mqtt: connected`, retained meta/status re-published, the dashboard shows online again.
3. If a slave goes DEAD during step 2, the join attempts are too disruptive for this deployment: lengthen the back-off (`Link(30000, 300000)` in `mqtt_uplink.h`), or build the master without the uplink (`master` env) and bridge it from a PC (`tools/sim_publisher.py --serial COMx`). Not verified on hardware, so record `joins=`, `missed=` and the times. If the router returns on a different channel the master follows it and the slaves need up to ~20 s (one lap of the channel scan) to find it again. TLS note: this build does not check certificate dates (the framework has no mbedtls date support); an unreachable broker shows `mqtt: connect attempt N ... failed: state=-2 ...` with the mbedtls reason, blocking at most 5 s (TCP) / 10 s (handshake) per attempt in the uplink task only.
4. Unclean reboot test: pull the master's USB power while the cloud connection is up, power it again within 10 s. The dashboard / `tools/alerter.py` must NOT report "master offline" about 45 s later (the old session's last will carries the old boot epoch and is ignored); a real outage (power off and leave) must show "master offline" about 45 s after the last message.

## 9. Without hardware (Wokwi)

`firmware/esp32/wokwi/slave1/` and `wokwi/slave2/` run one slave each with no radio (`-DWOKWI_STANDALONE`, envs `slave1_wokwi` and `slave2_wokwi`): the slave feeds itself a synthetic beacon every superframe and prints `W,` lines, `r` and `n` work. Build `pio run -e slave1_wokwi`, open `wokwi/slave1/wokwi.toml` with the Wokwi extension (Wokwi: Select Config File). `wokwi/master/` runs the real master firmware alone (`pio run -e master`); with no slaves it declares both DEAD after about 4 s. Wokwi cannot run the three boards together; `wokwi/all_three/diagram.json` is a wiring view only (see `firmware/esp32/wokwi/README.md`). It checks the MPU6050 path and 64-bit timing only, not the radio.
