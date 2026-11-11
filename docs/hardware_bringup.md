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
R,12345,1,8,7,0,0,1,..   (t_ms, slave, sf, window, raw, state, flags, std_az, kurt_az, dom_az, std_gx, dom_gx)
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
| 5.1 | Compute time (features + classify + smoothing) | slave `W` line, `compute_us` (8th field after `W`) | max well under 50 000 µs (target < 35 000) |
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
| Compute > ~45 ms | Make `features()` faster (radix-2 FFT instead of the direct DFT: ~10-20x fewer operations) | slave 1's first poll fails above ~50 ms compute; DEAD from ~68 ms. Do not relax timing instead. |
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

## 8. Labelled recording (real data)

On a slave type `r`. **The slave leaves the network** (no beacons processed, no replies; the master shows it DEAD after about 4 s; it rejoins when you press `r` again). Why: sampling and the schedule cannot be shared, and one window plus serial printing is longer than the poll gap. Windows are taken back-to-back, each printed as one CSV row after sampling:

```
D,<label>,<window>,az_0..az_127,gx_0..gx_127
```
Type `0` (still), `1` (shaken), `2` (tapped) to set the label for the following windows; discard the first window after a change. Suggested session: 5 min still, 5 min shaking by hand, 5 min repeated taps, per slave, at least twice on different days. Capture with `--filter log2file` and keep lines starting with `D,`. Stop with `r`.

## 9. Without hardware (Wokwi)

`firmware/esp32/slave_wokwi/` runs one slave with no radio (`-DWOKWI_STANDALONE`, env `slave_wokwi`): it feeds itself a synthetic beacon every superframe and prints `W,` lines, `r` and `n` work. Build `pio run -e slave_wokwi`, open `slave_wokwi/wokwi.toml` with the Wokwi extension (Wokwi: Select Config File). It checks the MPU6050 path and 64-bit timing only, not the radio.
