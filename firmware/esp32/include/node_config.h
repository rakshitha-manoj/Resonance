#pragma once
// Per-build node configuration for src/net_main.cpp. Set role and id with build flags
// (see the master / slave1 / slave2 envs in platformio.ini); everything else can be overridden
// with -D flags too.
#include <stdint.h>

#if defined(NODE_ROLE_MASTER) == defined(NODE_ROLE_SLAVE)
#error "define exactly one of NODE_ROLE_MASTER / NODE_ROLE_SLAVE"
#endif

#ifdef NODE_ROLE_MASTER
#  ifndef NODE_ID
#    define NODE_ID 0
#  endif
static_assert(NODE_ID == 0, "the master is node 0");
#else
#  ifndef NODE_ID
#    error "slave builds need -DNODE_ID=1 or -DNODE_ID=2"
#  endif
static_assert(NODE_ID == 1 || NODE_ID == 2, "slave ids are 1 and 2");
#endif

// ESP-NOW channel (1..13), the same on all three boards. NOTE: when the cloud/Wi-Fi uplink is added later
// the master's Wi-Fi station will sit on the ROUTER's channel, and ESP-NOW then runs on that channel too.
// Every node must therefore use the router's channel, so change this one value (or -DESPNOW_CHANNEL=n).
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

// Optional MAC allowlist, one macro per node id, each a 6-byte initializer. Leave undefined (the
// default) to use trust-on-first-use pinning instead. Read each board's MAC from its boot line
// "mac=..". Example:
//   -DNODE0_MAC="{0x24,0x6F,0x28,0x00,0x00,0x01}" -DNODE1_MAC=... -DNODE2_MAC=...
// A macro left undefined means "pin the first MAC seen for that id". This is NOT authentication:
// MAC addresses and node ids are unauthenticated and can be forged (see espnow_logic.h).
// #define NODE0_MAC {0x00,0x00,0x00,0x00,0x00,0x00}

// Noise gate override in raw counts of az_std (0 = use net::NOISE_GATE_COUNTS from tiers.h).
// Set -DNOISE_GATE_OVERRIDE=<value> ONLY in the slave envs' build_flags in platformio.ini, never in the
// shared [env] section: the native and netsim envs inherit [env] and must keep the trained gate.
// tiers.h will read the macro (separate change); until then the value is NOT applied and net_main
// prints a warning at boot if it differs from NOISE_GATE_COUNTS.
#ifndef NOISE_GATE_OVERRIDE
#define NOISE_GATE_OVERRIDE 0
#endif

// Master only: the physical floor each slave is mounted on (a small frame model: 1 and 2). They only label the
// dashboard: they are printed in the "floors" field of the meta line of the JSON event log (schema 2) and
// have no effect on classification. Override with -DSLAVE_FLOOR_1=5 -DSLAVE_FLOOR_2=10.
#ifndef SLAVE_FLOOR_1
#define SLAVE_FLOOR_1 1
#endif
#ifndef SLAVE_FLOOR_2
#define SLAVE_FLOOR_2 2
#endif

// Master only: print one "sample" JSON line per superframe (schema 2) in addition to the events. 0 = events only.
// Samples are the first thing dropped when the serial TX buffer is short on room.
#ifndef JSON_SAMPLES
#define JSON_SAMPLES 1
#endif

// Print statistics every this many superframes (master) / windows (slave). 1.4 s each.
#ifndef STATS_EVERY
#define STATS_EVERY 20
#endif

// Slave only: optional GPIO pulsed high during sample 0 (low at sample 1) for comparing the two
// slaves' window starts on a scope / logic analyser. -1 = off.
#ifndef SYNC_PIN
#define SYNC_PIN -1
#endif

// Master only: local alarm outputs (include/annunciator.h). -1 = not fitted. A LED pin drives an LED through a
// ~220 ohm resistor to GND; the buzzer pin drives a passive piezo buzzer (tone via LEDC). Enabled in the Wokwi cloud demo
// (wokwi/master_cloud/diagram.json); on hardware add e.g. -DANN_LED_DAMAGE_PIN=25 to the master env.
#ifndef ANN_LED_HEARTBEAT_PIN
#define ANN_LED_HEARTBEAT_PIN -1   // blue: pulses once per closed superframe (the on-board LED of a DevKit is GPIO 2)
#endif
#ifndef ANN_LED_EVENT_PIN
#define ANN_LED_EVENT_PIN -1       // yellow: on during a LOCAL / BUILDING vibration event
#endif
#ifndef ANN_LED_DAMAGE_PIN
#define ANN_LED_DAMAGE_PIN -1      // red: blinks after DAMAGE BEGIN, then on while DAMAGE is active
#endif
#ifndef ANN_BUZZER_PIN
#define ANN_BUZZER_PIN -1          // passive buzzer: event chirp, DAMAGE alarm, 30 s reminder, clear tone
#endif
#define ANN_ENABLED (ANN_LED_HEARTBEAT_PIN >= 0 || ANN_LED_EVENT_PIN >= 0 || ANN_LED_DAMAGE_PIN >= 0 || ANN_BUZZER_PIN >= 0)

// Slave only: back-to-back windows taken by the 'n' noise-estimate command.
#ifndef NOISE_WINDOWS
#define NOISE_WINDOWS 30
#endif
