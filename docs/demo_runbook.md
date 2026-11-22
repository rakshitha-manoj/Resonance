# Viva-day demo runbook

Everything here is SIMULATED data (a simulated 10-storey building, sensors on floors 5 and 10). Say so at the start. Nothing was measured on hardware.

The scenario is hand-picked: the excitation realisations of the building-wide event and the frequency shift (exc ids 16 and 18) were chosen by scanning for clean, unambiguous events. On the fresh test block the deployed rule's mean event recall (LOCAL and BUILDING recall averaged) is 0.734, about 73 %, at 1.64 false onsets per hour over healthy and noise-only hours, which does NOT meet the 1/h target (`models/selected/validation/fusion_eval_r3_test.md`, README). Do not present the demo as typical.

**Telegram warning:** if `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are already set permanently (User environment variables), `tools/alerter.py` and `--selftest-message` send REAL messages. Check without printing the secret: `if ($env:TELEGRAM_BOT_TOKEN -and $env:TELEGRAM_CHAT_ID) { "Telegram is ON in this window" } else { "dry run" }` (never `echo` the token in front of an audience) and remove them from a window with `Remove-Item Env:TELEGRAM_BOT_TOKEN, Env:TELEGRAM_CHAT_ID` if you only want a dry run.

The demo story (about 7.5 minutes of log time; times are minutes:seconds into the demo):

| Time | What happens | What you point at |
|---|---|---|
| 0:00 - 0:28 | Calibration (20 windows): each sensor learns its own normal level | grey "Calibrating" banner |
| 0:28 | Normal monitoring | green banner |
| about 2:10 | Each sensor has commissioned its natural frequency f1 (about 7.03 Hz) | the shaded band appears on the f1 chart |
| 2:50 | Impact on floor 5 -> LOCAL event, sensor 1. Both detectors see it; at the begin superframe only the autoencoder's smoothed flag is on (tag AE), the std ratio confirms one superframe later | event row with the AE tag, AE chart spike on floor 5 |
| 3:27 | Shaking of the whole building -> BUILDING-WIDE event | red square on the timeline |
| 3:58 | Frequency shift on floor 10 -> LOCAL event, sensor 2, detected ONLY by the autoencoder (the std ratio stays below 3) | AE chart over T_ae = 6 while the std chart is flat |
| 4:29 | A permanent 30 % stiffness loss in storey 1 starts (no event is visible: the building just vibrates a bit slower) | f1 drifting down |
| 5:29 | DAMAGE alarm (f1 about 4 % below commissioning for 32 superframes) | red banner, DAMAGE history row |

## (a) Offline replay demo (no network, safest, do this first)

1. Open `dashboard/index.html` by double-click (Chrome or Edge). No server needed.
2. Click **Damage demo (simulated)**: it starts playing at 10x by itself; switch to 60x if short of time (the Play button toggles pause). The page uses its own embedded copy of the log, so it works from `file://`.
3. Or use a link: `file:///D:/Resonance/dashboard/index.html?replay=demo_events_sim.jsonl&speed=20` (adjust the path). Add `&at=sf236&play=0` to open paused right after the alarm.
4. Back-up: the same data as pictures, `docs/figures/dashboard_overview.png` (end state) and `docs/figures/dashboard_damage.png` (moment of the alarm).

## (b) Live cloud demo (simulator -> MQTT -> dashboard -> Telegram)

Needs internet. The broker is the free public HiveMQ one: anybody who knows the site id can read the topics, which is fine for simulated data.

1. Build the simulator once (from `firmware/esp32`), in the VS Code PlatformIO terminal, or in plain PowerShell with `~\.platformio\penv\Scripts\pio.exe run -e netsim` (pio is not on the PATH there; WinLibs gcc must be on the PATH). Run it again after any `pio test -e netsim`, which overwrites the exe.
2. Publish (from the repository root, PowerShell):
   ```
   .venv\Scripts\python.exe -m pip install -r requirements-iot.txt      # once
   .venv\Scripts\python.exe tools/sim_publisher.py --speed 5 --transport websockets --port 8000
   ```
   `--speed 5` plays the 7.5 minutes in about 90 s (`--speed 1` = real time; `--loop -1` repeats forever). The websocket options are for networks that block port 1883 (`ws://broker.hivemq.com:8000/mqtt`); on a free network drop them.
3. Dashboard on another laptop or phone: open **MQTT live**, broker `wss://broker.hivemq.com:8884/mqtt`, site `rsn-822298ab`, press "MQTT live: connect". The page falls back to `ws://...:8000` by itself if 8884 is blocked. A ready link (the page must be served over http for a phone: in `dashboard/` run `python -m http.server 8080`, then open `http://<your-laptop-ip>:8080/index.html?site=rsn-822298ab&broker=ws://broker.hivemq.com:8000/mqtt&autoconnect=1`).
   Start the dashboard BEFORE the publisher, or it still catches up from the retained meta and state.
4. Telegram alerts (once, 5 minutes):
   - In Telegram, open **@BotFather**, send `/newbot`, give a name and a username ending in `bot`. It replies with a **token** (looks like `123456:ABC...`). Treat it like a password and never commit it.
   - Open a chat with your new bot and send it any message ("hi").
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser: find `"chat":{"id":<NUMBER>` = your **chat id**.
   - In PowerShell, in the same window you start the alerter from:
     ```
     $env:TELEGRAM_BOT_TOKEN = "<TOKEN>"
     $env:TELEGRAM_CHAT_ID = "<NUMBER>"
     .venv\Scripts\python.exe tools/alerter.py --site rsn-822298ab --transport websockets --port 8000 --cooldown 5
     ```
   - Test it: `.venv\Scripts\python.exe tools/alerter.py --selftest-message`. Without the two variables it only prints what it would send ("dry run").
   - Expect messages like `LOCAL vibration event at floor 10 (sensor 2) [AE 6.2 >= 6]` and `DAMAGE ALARM: natural frequency down 4.3 % (f1 6.73/6.74 Hz vs 7.03/7.05 Hz at commissioning)`.
5. Start the alerter, then the publisher; show the phone when the DAMAGE message arrives.

## (c) Wokwi cloud demo (the master firmware in a simulated ESP32)

1. From `firmware/esp32`, in the VS Code PlatformIO terminal (or `~\.platformio\penv\Scripts\pio.exe run -e master_wokwi_cloud` in PowerShell): `pio run -e master_wokwi_cloud` (flash use 96 % of the default app partition, it fits; if it ever does not, see "Partition table" in `firmware/esp32/wokwi/README.md`).
2. In VS Code: "Wokwi: Select Config File" -> `firmware/esp32/wokwi/master_cloud/wokwi.toml`, then "Wokwi: Start Simulator".
3. The serial monitor should show `wifi: connected ssid=Wokwi-GUEST channel=6`, `mqtt: connected`, then one JSON line per superframe and `S,mqtt,...` statistics. On the simulated board, the blue LED pulses every superframe, the yellow EVENT LED lights during each vibration event (one chirp), and at the DAMAGE alarm (about 5:30) the red LED blinks and the buzzer beeps three times, then the red LED stays on with a reminder beep every 30 s. Say: "the master raises a local alarm by itself, without the cloud". The buzzer is quiet in the diagram (volume 0.2); unmute the browser tab or turn the volume up in `diagram.json` if the room needs to hear it. The demo loops every 7.5 minutes (meta `source` = `wokwi_demo`), so the DAMAGE alarm comes back each lap (after the lap restarts the frequency is back to normal and the alarm ends first).
4. View it exactly as in (b) step 3 (same site id; do not run `sim_publisher.py` at the same time on the same site).
5. Notes: Wokwi reaches the internet through its Private IoT Gateway (the gateway needs the Wokwi extension and a licence for the VS Code mode). The firmware uses TLS port 8883 by default, because plain 1883 sometimes opens the TCP connection but gets no answer there. This path was verified on the PC (`pio test -e cloud_demo`), NOT yet run in Wokwi itself: try it before the viva, and use (b) if it misbehaves.

## (d) What to say for each screen

- **Header line**: "The rule is: raise an event if the vibration level OR the TinyML autoencoder says so; these are the thresholds, and the DAMAGE rule: 2 % frequency drop held for 32 superframes."
- **State banner + building picture**: "Per-floor status of the two instrumented floors; the master fuses both sensors into normal, local or building-wide."
- **DAMAGE banner**: "The structure-level answer: green while the natural frequency is steady, red when it has dropped for long enough to be real."
- **Timeline**: "The whole run at a glance; the red bar is the DAMAGE alarm."
- **Std ratio chart**: "Classic detector: vibration level over its own baseline. It sees strong shaking but misses small frequency changes."
- **AE chart**: "TinyML: an int8 autoencoder running on each sensor node. It catches the frequency shift and the sparse impact that the std ratio misses."
- **f1 chart**: "Each sensor's natural frequency against its own commissioned value; a stiffness loss lowers it."
- **Pooled drop chart**: "Both sensors averaged; crossing 2 % for 32 superframes triggers DAMAGE."
- **Event history, Detector column**: "Which detector fired: std, AE, or both."
- **DAMAGE history**: "The alarm with its frequencies; BASELINE rows would mean a sensor re-commissioned its reference."
- **MQTT live + Telegram**: "Same data, live, anywhere: the master publishes over MQTT, a second laptop or phone shows it and Telegram pushes the alert."
- Be ready to say: damage is detected for large losses in the lower storeys only (30 % at storey 1: yes; upper storeys and small losses: weak), and all numbers are simulation, not measured.

## (e) Fallbacks

- **MQTT blocked** (page says "MQTT offline"/no data): use the port-8000 websocket options shown above on both sides, or a phone hotspot. Still nothing? Run (a): it needs no network.
- **Telegram blocked**: the alerter prints the same messages in the terminal (dry run); show those.
- **Wokwi will not connect**: use (b); same dashboard, same data.
- **Dashboard misbehaves**: `docs/figures/dashboard_overview.png` and `dashboard_damage.png`, or reload with the `?replay=` link.
- **`program.exe` missing**: `pio run -e netsim` in `firmware/esp32` (VS Code PlatformIO terminal, or `~\.platformio\penv\Scripts\pio.exe run -e netsim`); or publish the stored log without the simulator: `.venv\Scripts\python.exe tools/sim_publisher.py --jsonl dashboard/demo_events_sim.jsonl --speed 5 --transport websockets --port 8000`.

## (f) Interactive digital twin

An examiner clicks buttons and the live stream shows the consequence within seconds. A simulated building (the demo building, f1 7.09 Hz, sensors on floors 5 and 10) is monitored by the REAL firmware logic (netsim: master, two slaves, rule f, f1 tracker, DAMAGE); `tools/twin_server.py` only generates the vibration signals from the buttons and replays netsim's log. Every alarm, DEAD and degraded-mode decision comes from the firmware C++ code.

1. Start (from `D:\Resonance`, `pio run -e netsim` must have been run once; this laptop: only the WebSocket port 8000 works):
   `.venv\Scripts\python.exe tools\twin_server.py --speed 2 --transport websockets --port 8000`
   It prints `SITE=twin-xxxxxxxx` (a NEW random site id every start, so it never mixes with another demo; `--site` fixes it). Do NOT start `sim_publisher.py` on the same site.
2. Dashboard: open `dashboard/index.html`, broker `ws://broker.hivemq.com:8000/mqtt`, site = the printed id, "MQTT live: connect". The "Digital twin" panel appears at once; its buttons stay disabled while the system commissions (about 100 superframes = 2.3 min at 1x, 70 s at `--speed 2`; the f1 reference of both sensors needs that long). Optional: `.venv\Scripts\python.exe tools\alerter.py --site <id> --transport websockets --port 8000` (with the Telegram variables set) for the phone alerts.
3. Stop with Ctrl+C (it publishes `running:false`; SIGTERM / Ctrl+Break do too). A server that is KILLED (power loss, window closed hard) cannot say so and leaves a stale retained `twin/state`: the server republishes it every 5 s with a heartbeat (`updated_t`, `period_s`), and the dashboard treats a state older than 3 periods (or `running:false`) as a dead twin: it shows "twin stopped" / "twin not responding", disables the buttons and hides the "simulator truth" band. The panel is also hidden when the MQTT source is disconnected or a file is loaded.

**Suggested 5-minute script** (speed 2: the numbers below are wall-clock seconds)
1. Commissioning (about 70 s): "The two sensors learn their own normal level and natural frequency; until then the buttons are off, because DAMAGE needs a reference."
2. Impact floor 5 (then 10): a LOCAL event at that sensor in about 10 s: "Local, not building-wide: only the sensor at that floor reacts."
3. Building shake: BUILDING event: "Both sensors see it."
4. Damage storey 10 by 30 %: the dotted ground-truth line moves by only 0.1 %; NO alarm. "Honest limitation: damage near the top barely changes f1, so a frequency-based alarm cannot see it. The twin log says so." Repair.
5. Damage storey 1 by 30 %: truth drops 3.9 %, the sensors' estimates follow after about 20-30 s and the red DAMAGE alarm appears about 40 s after the click (Telegram message). "Storey-1 damage lowers f1 most; the alarm needs the drop for 32 superframes to avoid false alarms, that is the delay."
6. Kill sensor 2: sensor 2 DEAD after about 7 s, the system continues in degraded mode (one-sensor decisions, no BUILDING). Revive 2: it recommissions for about 2 min on the current structure.
7. Repair (DAMAGE ends after about 70 s) or Reset (healthy, all sensors alive).

**Pacing of clicks.** Commands need at least 6 superframes (about 4 s at `--speed 2`) between them: a quicker click gets a "too soon ... wait N more superframes" line in the twin log and is ignored; only one command can be pending at a time, and a rate-limit / busy line appears if clicks are faster than 1 per 2 s. After a command the stream can pause for 1-3 s while netsim is re-run: that is normal, wait for the twin log line.

Facts to keep in mind: all of it is simulation; the dotted line is the simulator's ground truth, the solid lines the firmware's estimates; commands act from 4 superframes (5.6 s sim time) after the one being shown, never on the past; one command per 2 s; "Damage" replaces the current damage (one damaged storey at a time); a sensor revived while the structure is damaged recommissions ON the damaged state, so its own reference moves (known limitation: baselines are RAM-only). Storeys 8-10 and losses of 10 % in storey 1 stay below the 2 % threshold: no alarm is the correct answer. Anybody who knows the site id on the public broker can send commands (simulated demo only); commands are strictly validated and rate limited.

Measured on this PC (public HiveMQ over WebSocket 8000, jsdom page): click to twin log line 0.6-0.8 s (the log goes out before netsim is re-run), kill click to sensor 2 DEAD 7.2 s at `--speed 2` (3 missed superframes plus the reorder hold of 2.5 s), damage click to DAMAGE BEGIN on the page 41 s at `--speed 2` (6.6 s at `--speed 20`), i.e. 82 s of simulated time = 4 superframes guard + about 43 superframes (f1 averaging ring + P = 32). Use `--speed 4` if the audience is waiting.

## (g) Phone view over Wi-Fi

Examiners (or you) can open the live dashboard on a phone without any internet hosting: this laptop serves the `dashboard/` folder on the local network and the page shows a QR code. Needs the laptop and the phone on the same Wi-Fi.

1. From `D:\Resonance` (stop it with Ctrl+C when done):
   `.venv\Scripts\python.exe tools\serve_dashboard.py`
   It prints the addresses, e.g. `http://192.168.1.4:8080/` (one per network adapter; `--port 8081` if 8080 is busy). It serves only the `dashboard/` folder, read-only, no directory listing.
2. **Firewall and network profile.** The listening program is the BASE interpreter, not the `.venv` launcher: the script prints its full path at start (on this laptop `C:\Users\raksh\AppData\Local\Programs\Python\Python312\python.exe`), and the Windows Firewall rule has to name that file. This laptop's Wi-Fi profile is **Public**, and phone hotspots usually join as Public too, so "Private networks only" would block the phone. Either set the profile to Private for the demo (Settings > Network & internet > Wi-Fi > the network > Network profile type > Private), or tick **Public networks** in the Windows prompt (or Windows Security > Firewall > Allow an app, Public for that `python.exe`) and remove the rule after the demo. Check from another device: the printed URL must open the dashboard.
3. **Hotspot fallback.** Campus and guest Wi-Fi often isolate devices from each other (the phone cannot reach the laptop even on the same network name). Then switch on a hotspot on a phone, join the laptop and the other phones to it, restart the script and use the newly printed address. Laptop and phones then talk directly.
4. On the laptop open the printed `http://192.168.x.y:8080/` address (not `file://`), start what you want to show (for the live demo: broker `ws://broker.hivemq.com:8000/mqtt`, the site id, "MQTT live: connect", see (b) / (f); or the "Damage demo (simulated)" replay), then press **Phone view**. A QR code and the link appear. The link carries the current view: `?mqtt=1&site=<site>&broker=<url>` (the phone connects to the same MQTT site) or `?replay=demo_events_sim.jsonl&speed=<n>` (the phone plays the same replay). Scan it with the phone camera. If the laptop has several addresses, the other addresses are offered as buttons; pick the one on the network the phone is on.
5. Opened from `file://` (or when `/lan.json` is missing) the Phone view window only explains: run `.venv\Scripts\python.exe tools\serve_dashboard.py` and open the printed URL, then use Phone view.

What examiners see on the phone: the same dashboard in a single column (state banner, animated building, sensor health, the four charts, tables), connected live over the broker. The DAMAGE banner, the building outline and the notifications behave as on the laptop (notifications need a browser that allows them for an http page; they may not appear on every phone).

With the digital twin (f) running on the site, the twin panel appears on the phone too, with its buttons: **anyone who has the site id (the QR code, or anyone on the public broker who guesses it) can press them**, from any number of phones (the twin allows 1 command per 2 s in total). Use it for simulator data only, and stop `twin_server.py` after the demo.

The phone link uses plain `http://` on the local network (fine for demo data, nothing is encrypted and nothing leaves your network except the MQTT traffic to the broker). It does not work from the internet.
