# Wokwi configs

## The 3-ESP32 system

| Folder | Board | Build env | What you see in Wokwi |
|---|---|---|---|
| `master/` | Master, ESP32 only, no sensor | `master` | Real master firmware running alone: boot line, JSON `meta` line, beacons and polls. No slaves answer, so it declares both DEAD after about 4 s. |
| `slave1/` | Slave 1, ESP32 + MPU6050 | `slave1_wokwi` | Slave 1 with the radio replaced by a synthetic beacon every 1.4 s. Prints one `W,` line per window: sample span, compute time, deadline misses, class. |
| `slave2/` | Slave 2, ESP32 + MPU6050 | `slave2_wokwi` | Same as slave 1, with node id 2. |
| `master_cloud/` | Master, ESP32 only, **two virtual slaves inside the firmware** | `master_wokwi_cloud` | The IoT cloud path without hardware: Wi-Fi `Wokwi-GUEST` (open, channel 6), MQTT to the public HiveMQ broker, the demo traces replayed in a loop (impact, building-wide event, frequency shift every 448 s, with the DAMAGE alarm at about 5:30). See below. |
| `all_three/` | All three boards | none | **Wiring picture only, cannot be simulated.** Open it in the Wokwi editor to see the layout. It has no `wokwi.toml` on purpose. |

**Wokwi cannot simulate several boards talking to each other in one project.** Multiple microcontrollers in one diagram is still an open feature request: https://github.com/wokwi/wokwi-features/issues/186. So each board is simulated on its own here. The full 3-board network, with radio, packet loss and dead nodes, is simulated on the PC by `netsim` (see `.claude/STRUCTURE.md`).

### Running one board
1. Build it from `firmware/esp32`:
   - `pio run -e master`
   - `pio run -e slave1_wokwi`
   - `pio run -e slave2_wokwi`
2. In VS Code, run "Wokwi: Select Config File" and pick that folder's `wokwi.toml`.
3. Run "Wokwi: Start Simulator".

### Cloud demo (`master_cloud/`, env `master_wokwi_cloud`)
The only Wokwi project that talks to the internet. Nothing is measured: two `SlaveNode` state machines from `tiers.h` run inside the master firmware behind a loopback transport (`include/virtual_net.h`) and are fed windows from `test/data/demo_traces.bin` (converted to flash by `tools/gen_demo_header.py`, 320 windows x 2 slaves = 327,680 B, nothing decimated; the trace loops). Everything after the slaves is the real master path: `Master`, `Fusion`, the schema 2 JSON formatter, then the MQTT task on core 0.
1. `pio run -e master_wokwi_cloud` (generates `include/demo_traces.h` first), select `wokwi/master_cloud/wokwi.toml`, start the simulator.
2. The serial monitor shows `wifi: connected ssid=Wokwi-GUEST channel=6`, `mqtt: connected`, the JSON lines and `S,mqtt,...` statistics.
3. Open `dashboard/index.html` in a browser, source **MQTT live**, site id `rsn-822298ab` (the default in `include/mqtt_config.h`), Connect. Or run `tools/alerter.py` / any MQTT client on `resonance/rsn-822298ab/#` at `broker.hivemq.com`.
Wokwi for VS Code reaches the internet through its Private IoT Gateway (your computer's network). Plain MQTT on port 1883 can open the TCP connection and still get no CONNACK there, so the demo (and `master_mqtt`) default to TLS on port 8883 with the committed root CA (`include/hivemq_public_ca.h`, Amazon Root CA 1, valid to 2038; the broker's leaf certificate expires 2027-01-31 and is renewed by HiveMQ). This build verifies the certificate chain but not its dates (no mbedtls date support in the framework), so nothing waits for the clock; SNTP runs best-effort only. Build with `-DMQTT_PUBLIC_TLS=0` for plain 1883.
The topics are public: anybody who knows the site id can read them, so this is for simulated data only. Change `MQTT_SITE` for your own runs. The cloud demo was built but NOT run in Wokwi here; the same virtual-slave code is verified on the PC (`pio test -e cloud_demo`: its lines equal the netsim output of the same traces).

## Wiring of the real 3-board system

The boards talk over ESP-NOW (WiFi radio, 2.4 GHz), so **there are no wires between the boards**. Each board is powered from its own USB cable. The master's USB cable also carries the event log to the PC and the dashboard.

| Board | Connections |
|---|---|
| Master (ESP32 DevKit v1) | USB to the PC only |
| Slave 1 (ESP32 DevKit v1) + MPU6050 on floor A | 3V3 to VCC, GND to GND, D21 to SDA, D22 to SCL; USB for power |
| Slave 2 (ESP32 DevKit v1) + MPU6050 on floor B | Same as slave 1 |

Mount each MPU6050 with its z axis along the sway direction; the model reads `az`. Flashing and the measurement steps are in `docs/hardware_bringup.md`.

## Earlier phases (single ESP32)

| Folder | Build env | Firmware |
|---|---|---|
| `single_node/` | `esp32dev` | `src/main.cpp`, the original one-board live firmware |
| `single_node_selftest/` | `selftest` | `src/selftest.cpp`, the boot self-test (606 windows plus the scripted sequence) |

They are kept because the earlier results (Phases 1-5) were produced with them, and each slave still runs the same sensing and tree code.
