#pragma once
// Slave-side analytics context (Phase C2a): everything a slave computes per 128-sample window beyond the 5 tree features,
// in ONE place shared by the firmware (net_main.cpp), the virtual slaves (virtual_net.h), netsim and the self-test.
//   features_ex (one az DFT)  ->  spec_bands + gx kurtosis = base20  ->  AeNode (int8 autoencoder ratio)
//   spec_zoom (Goertzel 3..12 Hz)  ->  f1t_t tracker (first natural frequency, drop against the node's own f1_0)
// Plain C++11, no Arduino, no heap, no platform calls.
//
// MEMORY: one SlaveAnalytics is about 25.1 KB (f1t_t 23.4 KB + AeNode 1.7 KB). It is owned by the CALLER and attached to a
// SlaveNode (SlaveNode::attach), so a host with many slaves (netsim) allocates them on its own heap and the firmware declares
// ONE as a static/global, never as a local of loop() (the Arduino loop stack is 8 KB; process() itself needs about 2.5 KB).
//
// Per-window semantics:
//   * AE: windows 1..20 after a reset are the commissioning windows (ae_node_commission, no ratio, AE_READY = 0). From
//     window 21 on AE_READY = 1 and ae_ratio = ae_node_ratio. The first 20 windows are assumed quiet and representative (the
//     same assumption as the std baseline in fusion.h; nothing detects a bad commissioning). The noise gate is NOT applied
//     here: the master applies it to the reported az_std (fusion.h, rule f), exactly as in the evaluation.
//   * f1: f1t_update with the zoom spectrum of every window. Flags: F1_VALID = this window's estimate is valid,
//     F1_COMMISSIONED = f1_0 exists (f1_drop valid together with F1_VALID), F1_NOT_COMMISSIONED = overdue (f1t_not_commissioned).
//     Unavailable floats are NaN on the wire (ae_ratio 0 while not ready).
#include <stdint.h>
#include <math.h>
#include "net_proto.h"
#include "features.h"
#include "spectrum.h"
#include "ae_input.h"
#include "ae.h"
#include "f1track.h"

namespace net {

struct SlaveOut {
    float f[5];       // the 5 tree features (identical to features())
    float ae_ratio;   // 0 unless ae_ready
    float f1_est;     // NAN unless f1_valid
    float f1_drop;    // NAN unless f1_valid and f1_commissioned
    uint8_t xflags;   // FLAG_AE_READY | FLAG_F1_VALID | FLAG_F1_COMMISSIONED | FLAG_F1_NOT_COMMISSIONED
};

struct SlaveAnalytics {
    AeNode ae;
    f1t_t f1;

    SlaveAnalytics() { reset(); }

    // Power-on state: recommissions both (20 windows for the AE, about 96 windows = 2.2 min for f1).
    void reset() {
        ae_node_reset(&ae);
        f1t_init_default(&f1);
    }

    // One window. Time on the ESP32: see src/selftest.cpp (it prints the full-pipeline time per window).
    void process(const int16_t *az, const int16_t *gx, SlaveOut *o) {
        float p[WIN_N / 2];
        features_ex(az, gx, o->f, p);
        float sg, kg;
        feat_std_kurt(gx, &sg, &kg);
        float b20[AE_NBASE];
        ae_base_ex(o->f, p, kg, b20);
        uint8_t x = 0;
        o->ae_ratio = 0.0f;
        if (ae_node_ready(&ae)) {
            o->ae_ratio = ae_node_ratio(&ae, b20, nullptr);
            x |= FLAG_AE_READY;
        } else {
            ae_node_commission(&ae, b20);  // this window is a commissioning window: no ratio, not ready yet
        }
        float z[SPEC_ZOOM_N];
        spec_zoom(az, z);
        f1t_update(&f1, z);
        o->f1_est = f1.valid ? f1.f1_est : NAN;
        o->f1_drop = f1t_drop(&f1);
        if (f1.valid) x |= FLAG_F1_VALID;
        if (f1.commissioned) x |= FLAG_F1_COMMISSIONED;
        if (f1t_not_commissioned(&f1)) x |= FLAG_F1_NOT_COMMISSIONED;
        o->xflags = x;
    }
};

}  // namespace net
