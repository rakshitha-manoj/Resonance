# Viva-day demo runbook

Everything here is SIMULATED data (a simulated 10-storey building, sensors on floors 5 and 10). Say so at the start. Nothing was measured on hardware.

The scenario is hand-picked: the excitation realisations of the building-wide event and the frequency shift (exc ids 16 and 18) were chosen by scanning for clean, unambiguous events. On the fresh test block the deployed rule's mean event recall (LOCAL and BUILDING recall averaged) is 0.734, about 73 %, at 1.64 false onsets per hour over healthy and noise-only hours, which does NOT meet the 1/h target (`models/selected/validation/fusion_eval_r3_test.md`, README). Do not present the demo as typical.

**Telegram warning:** if `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are already set permanently (User environment variables), `tools/alerter.py` and `--selftest-message` send REAL messages. Check with `echo $env:TELEGRAM_BOT_TOKEN` (do not paste it anywhere) and remove them from a window with `Remove-Item Env:TELEGRAM_BOT_TOKEN, Env:TELEGRAM_CHAT_ID` if you only want a dry run.

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
