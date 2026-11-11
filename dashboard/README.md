# Resonance dashboard (R4)

A single file, `index.html`: inline CSS and JS, no external scripts, no build step, nothing leaves your computer.

## Open it

Double-click `dashboard/index.html`, or drag it into Chrome or Edge (works from `file://`). Light and dark mode follow the system; the Theme button overrides. Other browsers can use Replay, Simulated log and Demo, but not Live.

## Four ways to get data

1. **Demo** ("Run demo"). A short scripted scenario (calibration for 20 superframes, normal, local event on sensor 1, building-wide events, local event on sensor 2, sensor 2 offline = degraded, recovery, master restart). It is labelled "SYNTHETIC DEMO DATA": written by a script, not measured and not simulator output. The same stream is in `sample_events.jsonl` (schema 2) and embedded in the page.
2. **Simulated log** ("Load simulated log"). Loads `demo_events_sim.jsonl` from the same folder (a second real simulator log with a dead-and-revived sensor and a master restart, `demo_events_faults_sim.jsonl`, is opened with "Open JSONL file" or by dropping it on the page): the real output of the PC network simulator (master and slave firmware state machines, simulated building vibration), labelled "SIMULATED DATA". A page opened from disk (`file://`) is usually not allowed to read other files, so the button may fail; the page then says so and opens the file picker. Choose `dashboard/demo_events_sim.jsonl` there (or use "Open JSONL file", or drop the file on the page). Served over http (`python -m http.server` in `dashboard/`) the button works directly.
3. **Replay a file** ("Open JSONL file" or drag a file onto the page). Controls: play/pause, step back/forward by one event, speed 1x (real time on `t_us`), 10x, 60x, seek bar. Clicking the timeline or a time in the history table jumps there. Logs of 50,000+ lines load in chunks; the chart draws per-pixel min/max when dense.
4. **Live over USB** ("Connect to master (USB)"). Web Serial API, 115200 baud, Chrome/Edge desktop only. Close other serial monitors first. The master firmware (`src/net_main.cpp`, `master` env) prints the JSON lines on the same port as its CSV lines; the page uses the JSON lines and ignores everything else (`R,`, `S,`, `#` lines, boot text). "Save captured log" writes the received lines as a replay file. If the master reboots its clock starts again at 0; the `restart` line that follows its `meta` line makes the page continue the timeline after the last line (a notice says so).

   Not yet tried on real hardware or in a real browser (see Limits). If an old master firmware without the JSON lines is connected, only sensor alive/dead from the `E,t_ms,slave,ALIVE|DEAD` lines is shown (those are applied only when no sample lines exist).

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

- No cloud or broker integration yet; the page only reads local files, one fetch of `demo_events_sim.jsonl`, or a USB serial port.
- Web Serial works only in Chrome and Edge (desktop).
- Schema 1 logs (events only): r1/r2 exist only at event points, "last report" is the last logged line with the sensor alive, calibration progress and floor numbers are not available.
- The master firmware prints samples only if the serial buffer has room; dropped lines (never blocked) are counted in its `S,fusion,...` line, so a live log can have gaps in the samples.
- Not tested in a real browser or with real hardware by the author of this file. The page's script was syntax-checked with `node --check` (extract the `<script>` block to a `.js` file first) and exercised headlessly against a stub DOM (parsing, playback, the text shown for each state, the file:// fallback, the `node` lines); layout, canvas drawing and Web Serial were not.
