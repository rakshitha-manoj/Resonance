#pragma once
// Two virtual slaves in the master's process, for the Wokwi cloud demo (src/net_main.cpp, -DWOKWI_CLOUD_DEMO) where
// there is no radio and no second board. Plain C++11, no Arduino, no heap, host tested (test/test_cloud_demo).
//   * VirtualNet is the master's net::Transport: a broadcast (beacon / poll) is handed to two real tiers.h
//     SlaveNode state machines after `delay_us`, a slave's reply travels back after another `delay_us`.
//   * Each slave "samples" for SAMPLE_SPAN_US + compute_us after its beacon, then deliver_window() gets the window
//     from a VWindowSource (the demo traces); the slave classifies it exactly as on a board (features, noise gate,
//     smoothing, and with `pipes` the AE and the f1 tracker), so reports, fusion decisions and the JSON lines are those of the
//     real code path.
//   * Lossless and without jitter: this is a demo of the cloud path, not a network test (netsim does that).
// The host calls service() often (every loop) and sleeps until next_event_us() / Master::next_wakeup().
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "tiers.h"

namespace net {

class VWindowSource {
public:
    virtual ~VWindowSource() {}
    // Window of slave (1..NUM_SLAVES) for superframe sf; 128 samples each. The source decides what happens past its end.
    virtual void get(uint8_t slave, uint32_t sf, int16_t *az, int16_t *gx) = 0;
};

class VirtualNet : public Transport {
public:
    static constexpr size_t QUEUE = 8;

    // pipes: NUM_SLAVES analytics contexts (slave_pipe.h, ~25 KB each) owned by the CALLER (static on the ESP32, never on the
    // loop stack), attached to the two slaves; nullptr = tree features only.
    VirtualNet(VWindowSource *src, SlaveAnalytics *pipes = nullptr, uint32_t delay_us = 2000, uint32_t compute_us = 30000)
        : src_(src), delay_(delay_us), compute_(compute_us), dropped_(0), s1_(1, &tx_[0]), s2_(2, &tx_[1]) {
        if (pipes) {
            s1_.attach(&pipes[0]);
            s2_.attach(&pipes[1]);
        }
        static_assert(NUM_SLAVES == 2, "VirtualNet hosts exactly two slaves");
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            tx_[i].net = this;
            pending_[i] = false;
            due_[i] = 0;
            token_[i] = 0;
        }
        for (size_t i = 0; i < QUEUE; i++) q_[i].used = false;
    }
    VirtualNet(const VirtualNet &) = delete;
    VirtualNet &operator=(const VirtualNet &) = delete;

    // Master -> slaves (the master's Transport).
    bool broadcast(const uint8_t *buf, size_t len, uint64_t now) override {
        bool poll = len > 1 && buf[1] == T_POLL;
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            if (poll && pending_[i]) continue;  // still "sampling" (before its due time): a real slave is deaf then
            sl(i).on_packet(buf, len, now + delay_);
        }
        harvest_requests();
        return true;
    }

    // Runs everything that is due at `now`: window completions, slave free-run ticks, frames to the master.
    // on_frame(const uint8_t *buf, size_t len, uint64_t t_us) is called for each slave frame that reaches the master.
    template <class F>
    void service(uint64_t now, F on_frame) {
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            sl(i).tick(now);
        }
        harvest_requests();
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            if (pending_[i] && now >= due_[i]) pending_[i] = false;   // the window is "complete": the slave may answer polls now
        }
        for (;;) {  // frames to the master, oldest first
            int k = -1;
            for (size_t j = 0; j < QUEUE; j++)
                if (q_[j].used && q_[j].t <= now && (k < 0 || q_[j].t < q_[k].t)) k = (int)j;
            if (k < 0) break;
            Frame f = q_[k];
            q_[k].used = false;
            on_frame(f.data, f.len, f.t);
        }
    }

    // Earliest time something needs service() (NEVER = nothing pending).
    uint64_t next_event_us() const {
        uint64_t t = NEVER;
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            if (pending_[i] && due_[i] < t) t = due_[i];
            uint64_t w = sl(i).next_wakeup();
            if (w < t) t = w;
        }
        for (size_t j = 0; j < QUEUE; j++)
            if (q_[j].used && q_[j].t < t) t = q_[j].t;
        return t;
    }

    const SlaveNode &slave(uint8_t id) const { return sl(id - 1); }
    uint32_t dropped() const { return dropped_; }  // replies lost because the frame queue was full (never expected)

private:
    struct Frame {
        uint64_t t;
        uint8_t len;
        bool used;
        uint8_t data[MAX_PAYLOAD];
    };
    struct SlaveTx : Transport {
        VirtualNet *net = nullptr;
        bool broadcast(const uint8_t *buf, size_t len, uint64_t now) override { return net->enqueue(buf, len, now + net->delay_); }
    };

    bool enqueue(const uint8_t *buf, size_t len, uint64_t t) {
        if (len > MAX_PAYLOAD) return false;
        for (size_t j = 0; j < QUEUE; j++) {
            if (q_[j].used) continue;
            q_[j].used = true;
            q_[j].t = t;
            q_[j].len = (uint8_t)len;
            memcpy(q_[j].data, buf, len);
            return true;
        }
        dropped_++;
        return false;
    }

    // A beacon (or a free-run tick) asked a slave for a window. The trace window is available at once, so the slave's FULL pipeline
    // (features, AE, zoom, f1 tracker: tens of ms on a slow host, in Wokwi) runs RIGHT NOW, right after the beacon, not at the
    // sampling-complete time where it would delay the master's poll schedule (two slaves back to back at 1.32 s made the polls late).
    // The slave stays deaf to polls until the modelled completion time start + SAMPLE_SPAN_US + compute_ (released in service()).
    void harvest_requests() {
        for (uint8_t i = 0; i < NUM_SLAVES; i++) {
            uint32_t tok = sl(i).take_window_request();
            if (!tok) continue;
            token_[i] = tok;
            int16_t az[WIN_N], gx[WIN_N];
            src_->get((uint8_t)(i + 1), sl(i).window_sf(), az, gx);
            sl(i).deliver_window(tok, az, gx, sl(i).window_start());
            due_[i] = sl(i).window_start() + SAMPLE_SPAN_US + compute_;
            pending_[i] = true;
        }
    }

    SlaveNode &sl(uint8_t i) { return i == 0 ? s1_ : s2_; }
    const SlaveNode &sl(uint8_t i) const { return i == 0 ? s1_ : s2_; }

    VWindowSource *src_;
    uint32_t delay_, compute_, dropped_;
    SlaveTx tx_[NUM_SLAVES];
    SlaveNode s1_, s2_;
    bool pending_[NUM_SLAVES];
    uint64_t due_[NUM_SLAVES];
    uint32_t token_[NUM_SLAVES];
    Frame q_[QUEUE];
};

}  // namespace net
