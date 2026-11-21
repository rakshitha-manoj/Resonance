# Resonance dashboard (R4)

A single page, `index.html`: inline CSS and JS, no build step, works offline. Nothing leaves your computer unless you click "MQTT live: connect" (the only source that talks to a network). The one vendored library, `vendor/mqtt.min.js` (MQTT.js 5.10.4, MIT, see `vendor/README.md`), is used only by that source; without it the rest of the page is unchanged.

## Open it

Double-click `dashboard/index.html`, or drag it into Chrome or Edge (works from `file://`). Light and dark mode follow the system; the Theme button overrides. Other browsers can use Replay, Simulated log and Demo, but not Live.

## Five ways to get data

1. **Demo** ("Run demo"). A short scripted scenario (calibration for 20 superframes, normal, local event on sensor 1, building-wide events, local event on sensor 2, sensor 2 offline = degraded, recovery, master restart). It is labelled "SYNTHETIC DEMO DATA": written by a script, not measured and not simulator output. The same stream is in `sample_events.jsonl` (schema 2) and embedded in the page.
2. **Simulated log** ("Load simulated log"). Loads `demo_events_sim.jsonl` from the same folder (a second real simulator log with a dead-and-revived sensor and a master restart, `demo_events_faults_sim.jsonl`, is opened with "Open JSONL file" or by dropping it on the page): the real output of the PC network simulator (master and slave firmware state machines, simulated building vibration), labelled "SIMULATED DATA". A page opened from disk (`file://`) is usually not allowed to read other files, so the button may fail; the page then says so and opens the file picker. Choose `dashboard/demo_events_sim.jsonl` there (or use "Open JSONL file", or drop the file on the page). Served over http (`python -m http.server` in `dashboard/`) the button works directly.
3. **Replay a file** ("Open JSONL file" or drag a file onto the page). Controls: play/pause, step back/forward by one event, speed 1x (real time on `t_us`), 10x, 60x, seek bar. Clicking the timeline or a time in the history table jumps there. Logs of 50,000+ lines load in chunks; the chart draws per-pixel min/max when dense.
4. **Live over USB** ("Connect to master (USB)"). Web Serial API, 115200 baud, Chrome/Edge desktop only. Close other serial monitors first. The master firmware (`src/net_main.cpp`, `master` env) prints the JSON lines on the same port as its CSV lines; the page uses the JSON lines and ignores everything else (`R,`, `S,`, `#` lines, boot text). "Save captured log" writes the received lines as a replay file. If the master reboots its clock starts again at 0; the `restart` line that follows its `meta` line makes the page continue the timeline after the last line (a notice says so).

   Not yet tried on real hardware or in a real browser (see Limits). If an old master firmware without the JSON lines is connected, only sensor alive/dead from the `E,t_ms,slave,ALIVE|DEAD` lines is shown (those are applied only when no sample lines exist).
5. **MQTT live (cloud)** ("MQTT live: connect"). The master (or `tools/sim_publisher.py`, or the Wokwi cloud demo) publishes the same JSON lines to an MQTT broker; the page subscribes over MQTT-over-WebSocket and feeds every message through the same `parseLine` path as USB and replay, so everything below (banner, chart, history, counters) works the same. Fields: broker URL (default `wss://broker.hivemq.com:8884/mqtt`, the free public HiveMQ broker), site id (default `rsn-822298ab`, the topic prefix `resonance/<site>/`), optional user / password under "MQTT options" (never stored), a connection pill (idle / connecting / connected / reconnecting / offline), and received / dropped counts (dropped = malformed or buffer full; duplicates redelivered by the broker are skipped and counted; the master's retained `status` message {"state":"online"|"offline","epoch":E} shows "master online/offline"; an "offline" whose epoch differs from the meta epoch is the stale last will of an earlier boot and is ignored). Settings can also come from the address: `index.html?site=rsn-xxxxxxxx&broker=wss://...&autoconnect=1`.
   - Topics: `resonance/<site>/<type>` per line type (`meta`, `restart`, `sample`, `begin`, `change`, `status_line` = the fusion `status` line, `node`, and any future type such as `damage` by its own name), retained `.../state` (latest decision), retained `.../status` (`{"state":"online","epoch":E}`, re-published every 60 s; last will = the same with `offline`), retained `.../damage_state` (the latest `damage` line, schema 3 next phase), retained `meta`. A page that joins late gets the meta (floors) and the current state at once.
   - MQTT orders messages per topic only, so the page holds lines for up to 2.5 s and releases them sorted by `t_us` (restart, node, sample, events as the tie-break), which restores the exact original order (verified, see below). Reboots are told apart without relying on arrival order where possible: a `restart` line with a DIFFERENT epoch and a small boot clock (<= 15 s) is a reboot at any previous run length (the buffer is flushed first, and a drop of the samples' `closed` counter, which travels on one ordered topic, flushes it too); a restart with the same epoch (the firmware's boot pattern) or a netsim-style restart whose clock simply continues never rebases the timeline, however late it arrives; a restart without an epoch falls back to the 30 s clock-drop rule. LIMIT: a reboot is still decided by comparing times, so under a slow-broker BURST (a recording published faster than real time, deliveries seconds apart across topics) a reboot's restart line can arrive after the new run's lines and the two runs get interleaved; real-time operation is not affected. `dashboard/tools/short_reboot.jsonl` (a reboot 19.5 s into a run) is therefore tested with `--speed 2` in `test_mqtt_dom.js`. The hold is wall-clock time (2.5 s), not `t_us`, so a burst or a backlog flush after a reconnect is held just as long; a line that still arrives later than that makes the page re-sort and rebuild its event/sample lists.
   - **Notifications.** "Enable notifications" asks the browser for permission (it must be a click); then a new `begin` event (LOCAL / BUILDING) and a sensor going DEAD raise a desktop notification, for the USB and MQTT live sources (never for replay or retained messages).
   - A public broker is world-readable: anybody who knows the site id can read the topics. Use it for simulator and demo data only. A private broker (HiveMQ Cloud free cluster, TLS + user/password) needs only the URL `wss://<cluster>.s1.eu.hivemq.cloud:8884/mqtt` and the credentials. If your network blocks 8884 (some do), the same broker offers `ws://broker.hivemq.com:8000/mqtt` (plain WebSocket, only from `file://` or http pages): on such pages the page falls back to it by itself when the default wss URL has not connected after 12 s, and the "Use ws:// (8000)" button selects it at once.

## What the page shows (schema 2)

- **Chart and "last report"** come from the per-superframe `sample` lines (a point every 1.4 s), not just from the sparse events. Sensor health shows "Last report N superframes ago" from `age`, and "No report received yet" when `age` is null.
- **Floor labels** come from `meta.floors` ("floor 5 (sensor 1)"); the lower floor is drawn at the bottom of the building picture. Without it (schema 1) the labels are "sensor 1/2" and sensor 1 is drawn lower by assumption.
- **Calibration progress** (`commission_n`, out of `meta.commission_windows`) is shown as a progress bar in the banner and per sensor while calibrating.
- **Sensor health** (`node` lines): when the master declares a sensor DEAD (3 missed superframes, about 4.2 s) or ALIVE again, the page shows it at once on that sensor's card and floor, with a note "from the sensor health line" and a banner chip, and adds a row (kind `node`, "Sensor N offline" / "alive again", duration of the outage) to the event history. A `node` line only overrides the sample / event `alive` flag if it is at least as new as the shown sample / event and newer than the last master restart; otherwise the flags work exactly as before, so logs without `node` lines (older logs, the embedded synthetic demo) and the live `E,` fallback behave as before. In logs with samples the sample after the `node` line carries the same flag at the same time stamp, so the gain is mainly for event-only logs (`--no-samples`), dropped samples on a busy serial line, and the history row.
- **Master restarts** (`restart` lines) are marked on the timeline (R) and the chart, listed in the Master row (count, last time, new epoch), and a banner chip appears for one minute after one.
- **Simulated flag.** `meta.source = "netsim"` shows the SIMULATED banner.

## Input format

JSON lines, schema 2 (schema 1 still works), exactly as described under "JSON-lines event log" in `.claude/STRUCTURE.md`:

- `meta` (`schema`, `source`, `rule`, `T`, `debounce`, `commission_windows`, `status_every_sf`, `superframe_s`, `floors`, `epoch`, `samples`);
- `sample` (`t_us`, `sf`, `class`, `slave`, `degraded_alive`, `r1`, `r2`, `alive`, `age`, `commission_n`, `closed`), one per superframe;
- `begin` / `change` / `status` (`t_us`, `sf`, `class`, `slave`, `degraded_alive`, `r1`, `r2`, `base1`, `base2`, `alive`, `closed`, `events`, `commission_n`);
- `restart` (`t_us`, `epoch`, `prev_epoch`);
- `node` (`t_us`, `sf`, `slave` 1|2, `state` `"DEAD"`|`"ALIVE"`), additive in schema 2: written when the master's status of a slave changes; an unknown-to-the-reader type is ignored, a malformed one is counted malformed.

Handling:

- The threshold line on the chart comes from `meta.T` (never hard-coded); without a meta line it is shown as not available.
- `meta.schema` other than 1 or 2: a warning is shown and parsing continues.
- `r1`/`r2` of `null` = no report (gap in the chart, "no report" in the table). Baseline `0` = not known yet.
- Malformed lines (bad JSON, missing/wrong-typed fields) are counted and skipped; valid JSON of an unknown `type` is counted as ignored. Counts are shown under the source controls.
- Out-of-order `t_us` is re-sorted with a warning (`node` lines are sorted too). A `restart` line whose `t_us` is earlier than the lines before it re-bases the following times (reboot of the board).
- Live mode keeps at most 200,000 events and 150,000 samples (about 58 hours of samples) in memory; further lines are counted as dropped.

## Cloud setup (MQTT + Telegram alerts)

Pieces: master firmware (`master_mqtt` env) or the Wokwi demo (`master_wokwi_cloud` env, `firmware/esp32/wokwi/master_cloud/`) or `tools/sim_publisher.py` -> broker -> this page, `tools/alerter.py` (Telegram), any MQTT client. Settings live in `firmware/esp32/include/mqtt_config.h`; Wi-Fi and broker credentials go into `firmware/esp32/include/secrets.h` (gitignored, copy `secrets.example.h`) or `-D` flags, never into git.

- **No hardware, no Wokwi:** `pip install -r requirements-iot.txt`, then `python tools/sim_publisher.py --speed 10` (runs netsim on the demo traces and publishes in real time x10; `--jsonl dashboard/demo_events_faults_sim.jsonl` for the fault log, `--speed 1` real time, `--loop -1` forever). Open the dashboard, MQTT live, site `rsn-822298ab`, connect. Add `--transport websockets --port 8000` if your network blocks port 1883.
- **Wokwi:** `pio run -e master_wokwi_cloud`, open `firmware/esp32/wokwi/master_cloud/wokwi.toml`. The simulated ESP32 joins `Wokwi-GUEST` and publishes the looping demo traces under the same site id (not run in Wokwi by the author, see the project report).
- **Hardware day:** put the Wi-Fi and (optionally) your own site id and broker into `secrets.h`, `pio run -e master_mqtt -t upload`. The master publishes the JSON lines it prints on USB; serial output is unchanged. Alternatively keep the stock `master` env and bridge it from a PC: `python tools/sim_publisher.py --serial COM5`. ESP-NOW shares the radio with Wi-Fi, so it runs on the router's channel; the master prints the channel and warns if it differs from `ESPNOW_CHANNEL`; slaves that are not synced scan channels 1-13 (1.5 s each) and lock on the first valid beacon.
- **Telegram alerts:** create a bot with @BotFather (`/newbot`, copy the token), send it any message, open `https://api.telegram.org/bot<token>/getUpdates` once to read your `chat.id`, then `set TELEGRAM_BOT_TOKEN=...` and `set TELEGRAM_CHAT_ID=...` (environment variables only; the token is never logged) and run `python tools/alerter.py --site rsn-822298ab`. Without those variables it prints the alerts instead. Alerts: event begins (LOCAL at floor X / BUILDING-WIDE), a change into one of them, sensor DEAD / ALIVE, master restart, master offline (last will), `damage` lines. Rate limit: the same alert at most once per `--cooldown` seconds (default 60) and at most `--max-per-hour` (default 30); suppressed ones are counted in the next message.
- **Tests:** `pio test -e mqtt_logic` (topics, queue policy, back-off, publishing), `pio test -e cloud_demo` (virtual slaves reproduce the netsim output of the same traces), `python tools/test_iot_tools.py`, `python tools/e2e_mqtt_test.py` (real broker: every line byte-identical, per-topic order, retained state/meta/status), and `NODE_PATH=<jsdom>/node_modules node dashboard/tools/test_mqtt_dom.js` (this page in jsdom against a real broker, compared with a replay of the same file).

## Make a replay file with the netsim CLI

From `firmware/esp32` (see `.claude/STRUCTURE.md`, "netsim fusion CLI"):

```
pio run -e netsim
.pio/build/netsim/program.exe --traces test/data/demo_traces.bin --events-jsonl FILE [--no-samples] [--floors A,B] [--quiet]
```

The bundled simulated log is made with:

```
.pio/build/netsim/program.exe --traces test/data/demo_traces.bin --events-jsonl ../../dashboard/demo_events_sim.jsonl --quiet
```

The fault demo (sensor 2 killed at superframe 30 and revived at 62, master restarted at 100; gives 2 `node` lines and 1 `restart` line):

```
.pio/build/netsim/program.exe --traces test/data/demo_traces.bin --superframes 170 --kill 2:30 --revive 2:62 --kill 0:100 --revive 0:101 --events-jsonl ../../dashboard/demo_events_faults_sim.jsonl --quiet
```

(floors 5 and 10 are read from `test/data/demo_traces.bin.floors`. `--kill NODE:SF` / `--revive NODE:SF`: node 0 is the master, 1 and 2 the sensors. `pio test -e netsim` overwrites `program.exe`, so run `pio run -e netsim` again first.)

## Validate a log

```
.venv/Scripts/python.exe dashboard/tools/validate_logs.py dashboard/demo_events_sim.jsonl dashboard/demo_events_faults_sim.jsonl dashboard/sample_events.jsonl
```

`dashboard/tools/validate_logs.py` checks the exact key set and types of every line type (meta, restart, sample, begin/change/status, node), enums and null rules, time order, sample/closed counters, sample-vs-event agreement, the restart epoch chain, and for `node` lines that DEAD / ALIVE alternate per slave and that the next sample's `alive` flag agrees. Exit code 1 if any file has an error. All three bundled logs pass.

## Limits

- The page reads local files, one fetch of `demo_events_sim.jsonl`, a USB serial port, or an MQTT broker (only after you click connect). The public HiveMQ broker is for simulator/demo data only; MQTT live was tested headlessly in jsdom against it, not yet in a real browser; the browser notifications were tested with a stub `Notification` object, not on a real desktop.
- Web Serial works only in Chrome and Edge (desktop).
- Schema 1 logs (events only): r1/r2 exist only at event points, "last report" is the last logged line with the sensor alive, calibration progress and floor numbers are not available.
- The master firmware prints samples only if the serial buffer has room; dropped lines (never blocked) are counted in its `S,fusion,...` line, so a live log can have gaps in the samples.
- Not tested in a real browser or with real hardware by the author of this file. The page's script was syntax-checked with `node --check` (extract the `<script>` block to a `.js` file first) and exercised headlessly against a stub DOM (parsing, playback, the text shown for each state, the file:// fallback, the `node` lines); layout, canvas drawing and Web Serial were not.
