# Wokwi configs

## The 3-ESP32 system

| Folder | Board | Build env | What you see in Wokwi |
|---|---|---|---|
| `master/` | Master, ESP32 only, no sensor | `master` | Real master firmware running alone: boot line, JSON `meta` line, beacons and polls. No slaves answer, so it declares both DEAD after about 4 s. |
| `slave1/` | Slave 1, ESP32 + MPU6050 | `slave1_wokwi` | Slave 1 with the radio replaced by a synthetic beacon every 1.4 s. Prints one `W,` line per window: sample span, compute time, deadline misses, class. |
| `slave2/` | Slave 2, ESP32 + MPU6050 | `slave2_wokwi` | Same as slave 1, with node id 2. |
| `all_three/` | All three boards | none | **Wiring picture only, cannot be simulated.** Open it in the Wokwi editor to see the layout. It has no `wokwi.toml` on purpose. |

**Wokwi cannot simulate several boards talking to each other in one project.** Multiple microcontrollers in one diagram is still an open feature request: https://github.com/wokwi/wokwi-features/issues/186. So each board is simulated on its own here. The full 3-board network, with radio, packet loss and dead nodes, is simulated on the PC by `netsim` (see `.claude/STRUCTURE.md`).

### Running one board
1. Build it from `firmware/esp32`:
   - `pio run -e master`
   - `pio run -e slave1_wokwi`
   - `pio run -e slave2_wokwi`
2. In VS Code, run "Wokwi: Select Config File" and pick that folder's `wokwi.toml`.
3. Run "Wokwi: Start Simulator".

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
