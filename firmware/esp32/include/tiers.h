#pragma once
// Node state machines: SlaveNode and Master. Plain C++11, no Arduino, no dynamic allocation.
// Driven by tick(now_us) / on_packet(buf,len,now_us) (and deliver_window for the slave).
// next_wakeup() tells the host when tick() must next be called (UINT64_MAX = no deadline).
// NOT thread-safe: on ESP32 queue received packets from the ESP-NOW callback to the main loop.
// Times are 64-bit microseconds (esp_timer_get_time() on hardware, never 32-bit micros()).
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "net_proto.h"
#include "transport.h"
#include "features.h"
#include "smooth.h"

namespace net {

constexpr uint64_t NEVER = ~(uint64_t)0;

// ---------------------------------------------------------------- noise gate
// PROVISIONAL: 92.12 is a placeholder taken from the synthetic dataset. It MUST be set from the
// real MPU6050 noise measured on the mounted sensor (az_std of a motionless structure).
// Applied outside classify(): below the gate the window is "normal" and flagged LOW_SIGNAL.
constexpr float NOISE_GATE_COUNTS = 92.12f;

static inline int apply_noise_gate(const float *f, int raw, uint8_t *flags) {
    if (f[0] < NOISE_GATE_COUNTS) {
        *flags |= FLAG_LOW_SIGNAL;
        return 0;
    }
    return raw;
}

// ---------------------------------------------------------------- field validation
static inline bool finite_f(float x) {
    uint32_t u;
    memcpy(&u, &x, 4);
    return (u & 0x7F800000u) != 0x7F800000u;  // exponent all ones => inf or NaN
}

// Master-side sanity check of a decoded report (CRC already verified).
static inline bool report_fields_ok(const ReportPkt &r) {
    if (r.raw >= NUM_CLASSES || r.state >= NUM_CLASSES) return false;
    if (r.flags & ~FLAGS_VALID) return false;
    for (int i = 0; i < 5; i++)
        if (!finite_f(r.f[i])) return false;
    return true;
}

// ---------------------------------------------------------------- slave
struct SlaveStats {
    uint32_t rx_invalid, rx_ignored, beacons, polls_answered, polls_silent, free_runs;
    uint32_t beacons_rejected;  // synced, same epoch, sf outside (last, last + MAX_SF_JUMP]
    uint32_t epoch_resyncs;     // master epoch changed while synced
    uint32_t windows_rejected;  // deliver_window with a stale/invalid token
    uint32_t tx_failed;         // Transport::broadcast returned false
};

// Sync rules (on_beacon):
//   * Not synced yet (power-on) or LOST: accept any CRC-valid beacon.
//   * Synced and the epoch differs from ours: the master restarted. Resync at once: take the new
//     epoch, abandon the window in progress, DISCARD the cached report (so a pre-restart report is
//     never sent), restart the free-run counter. The smoothing history, smoothed state and window
//     counter are KEPT: they describe the slave's own sensor, which a master restart does not
//     change (the first post-restart report is just window N+1 of the same sensor).
//   * Synced, same epoch: accept sf in (last_sf, last_sf + MAX_SF_JUMP]; sf == last_sf right after
//     a free-run is the "late beacon" and only re-aligns the timing; anything else is rejected.
class SlaveNode {
public:
    SlaveNode(uint8_t id, Transport *tx) : id_(id), tx_(tx), token_ctr_(0) { reset(); }

    // Power-on state. The window token counter is deliberately NOT reset: a delivery that was
    // scheduled before a reset can then never match a window started after it.
    void reset() {
        have_sync_ = false;
        lost_ = false;
        free_run_pending_ = false;
        busy_ = false;
        req_pending_ = false;
        have_report_ = false;
        last_sf_ = 0;
        epoch_ = 0;
        next_expected_ = 0;
        missed_ = 0;
        win_sf_ = 0;
        win_token_ = 0;
        win_start_ = 0;
        win_flags_ = 0;
        window_ctr_ = 0;
        tx_seq_ = 0;
        hist_[0] = hist_[1] = hist_[2] = -1;
        state_ = 0;
        memset(&rep_, 0, sizeof(rep_));
        memset(&stats_, 0, sizeof(stats_));
    }

    // Host asks: has the slave decided a sampling window must start now?
    bool window_requested() const { return req_pending_; }
    // Host acknowledges and clears the request. Returns the window TOKEN (non-zero; 0 = nothing was
    // requested). The host must pass the same token to deliver_window(); a window that a later
    // beacon (or reset) abandoned has a different token, so its late delivery is rejected.
    uint32_t take_window_request() {
        if (!req_pending_) return 0;
        req_pending_ = false;
        return win_token_;
    }
    // Time of the first sample of the current/last requested window, and its superframe.
    uint64_t window_start() const { return win_start_; }
    uint32_t window_sf() const { return win_sf_; }

    // Host delivers the completed window (128 samples each) when sampling and feature computation
    // are done. Returns false (and changes nothing) if the token is not that of the window in
    // progress. Smoothing advances exactly once per accepted delivery.
    bool deliver_window(uint32_t token, const int16_t *az, const int16_t *gx, uint64_t /*now_us*/) {
        if (!busy_ || token == 0 || token != win_token_) {
            stats_.windows_rejected++;
            return false;
        }
        float f[5];
        features(az, gx, f);
        uint8_t flags = win_flags_;
        int raw = apply_noise_gate(f, classify(f), &flags);  // gate BEFORE smoothing
        state_ = smooth_update(hist_, raw, state_);
        rep_.sf = win_sf_;
        rep_.epoch = epoch_;
        rep_.window = window_ctr_++;
        rep_.raw = (uint8_t)raw;
        rep_.state = (uint8_t)state_;
        rep_.flags = flags;
        for (int i = 0; i < 5; i++) rep_.f[i] = f[i];
        have_report_ = true;
        busy_ = false;
        return true;
    }

    // True while the slave is sampling or computing (it never transmits then).
    bool busy() const { return busy_; }

    void tick(uint64_t now) {
        if (have_sync_ && !lost_ && !busy_ && now >= next_expected_ + BEACON_GRACE_US) {
            if (missed_ >= MAX_MISSED_BEACONS) {
                lost_ = true;  // too long without a beacon: stop producing reports until resynced
                return;
            }
            missed_++;
            stats_.free_runs++;
            last_sf_++;
            next_expected_ += SUPERFRAME_US;  // keep the previous timing, no drift accumulation
            free_run_pending_ = true;
            start_window(last_sf_, now, FLAG_UNSYNCED);
        }
    }

    uint64_t next_wakeup() const {
        if (have_sync_ && !lost_ && !busy_) return next_expected_ + BEACON_GRACE_US;
        return NEVER;
    }

    void on_packet(const uint8_t *buf, size_t len, uint64_t now) {
        Header h;
        if (!decode_header(buf, len, &h)) {
            stats_.rx_invalid++;
            return;
        }
        if (h.src != ID_MASTER) {
            stats_.rx_ignored++;
            return;
        }
        if (h.type == T_BEACON) {
            BeaconPkt p;
            if (!decode_beacon(buf, len, &p)) { stats_.rx_invalid++; return; }
            if (h.dst != ID_BROADCAST && h.dst != id_) { stats_.rx_ignored++; return; }
            on_beacon(p.sf, p.epoch, now);
        } else if (h.type == T_POLL) {
            PollPkt p;
            if (!decode_poll(buf, len, &p)) { stats_.rx_invalid++; return; }
            if (h.dst != id_) { stats_.rx_ignored++; return; }
            on_poll(p.sf, p.epoch, now);
        } else {
            stats_.rx_ignored++;
        }
    }

    uint8_t id() const { return id_; }
    bool synced() const { return have_sync_ && !lost_; }
    bool lost() const { return lost_; }
    bool has_report() const { return have_report_; }
    const ReportPkt &report() const { return rep_; }
    const int8_t *smooth_hist() const { return hist_; }
    int smooth_state() const { return state_; }
    uint32_t windows_done() const { return window_ctr_; }
    uint32_t epoch() const { return epoch_; }
    uint32_t last_sf() const { return last_sf_; }
    uint32_t missed_beacons() const { return missed_; }
    const SlaveStats &stats() const { return stats_; }

private:
    void start_window(uint32_t sf, uint64_t start, uint8_t flags) {
        win_sf_ = sf;
        win_start_ = start;
        win_flags_ = flags;
        if (++token_ctr_ == 0) ++token_ctr_;  // 0 is never a valid token
        win_token_ = token_ctr_;
        busy_ = true;  // from now until deliver_window(): no transmissions
        req_pending_ = true;
    }

    void on_beacon(uint32_t sf, uint32_t epoch, uint64_t now) {
        stats_.beacons++;
        if (have_sync_ && epoch != epoch_) {
            stats_.epoch_resyncs++;
            have_report_ = false;  // never answer a post-restart poll with a pre-restart report
        } else if (have_sync_ && !lost_) {
            if (sf == last_sf_ && free_run_pending_) {
                // Late beacon for a superframe we already free-ran: resync timing, keep the running
                // window. The window keeps FLAG_UNSYNCED: it was started on the slave's own timing,
                // not on this beacon, and the flag is the only field evidence of how often that
                // happens (needed to tune BEACON_GRACE_US on hardware).
                free_run_pending_ = false;
                missed_ = 0;
                next_expected_ = now + SUPERFRAME_US;
                return;
            } else {
                uint32_t jump = sf - last_sf_;  // unsigned: sf behind last_sf wraps to a huge value
                if (jump < 1 || jump > MAX_SF_JUMP) {
                    stats_.beacons_rejected++;  // duplicate, stale, or implausibly far ahead
                    return;
                }
            }
        }
        have_sync_ = true;
        lost_ = false;
        free_run_pending_ = false;
        missed_ = 0;
        epoch_ = epoch;
        last_sf_ = sf;
        next_expected_ = now + SUPERFRAME_US;
        start_window(sf, now, 0);  // any window still in progress is abandoned (its token is now stale)
    }

    void on_poll(uint32_t sf, uint32_t epoch, uint64_t now) {
        if (busy_ || !have_report_ || epoch != epoch_ || sf != rep_.sf) {
            stats_.polls_silent++;  // not ready (or wrong superframe/epoch): stay silent
            return;
        }
        uint8_t buf[MAX_PAYLOAD];
        size_t n = encode_report(buf, id_, ID_MASTER, tx_seq_++, rep_);
        if (tx_->broadcast(buf, n, now)) stats_.polls_answered++;
        else stats_.tx_failed++;  // the master will retry
    }

    uint8_t id_;
    Transport *tx_;
    bool have_sync_, lost_, free_run_pending_, busy_, req_pending_, have_report_;
    uint32_t last_sf_, epoch_, missed_, win_sf_, window_ctr_, token_ctr_, win_token_;
    uint64_t next_expected_, win_start_;
    uint8_t win_flags_;
    uint16_t tx_seq_;
    int8_t hist_[3];
    int state_;
    ReportPkt rep_;
    SlaveStats stats_;
};

// ---------------------------------------------------------------- master
class MasterListener {
public:
    virtual ~MasterListener() {}
    // A new valid report was accepted (R2 attaches fusion here).
    virtual void on_report(uint8_t slave, const ReportPkt &r, uint64_t now_us) = 0;
    // Slave transitioned alive->dead (alive=false) or dead->alive (alive=true).
    virtual void on_status(uint8_t slave, bool alive, uint64_t now_us) = 0;
};

struct StatusEvent {
    uint64_t t_us;
    uint32_t sf;
    uint8_t slave;
    bool alive;
};

struct MasterStats {
    uint32_t beacons, polls_tx[NUM_SLAVES], retries[NUM_SLAVES], reports[NUM_SLAVES];
    uint32_t missed_total[NUM_SLAVES];
    uint32_t rx_invalid, rx_ignored, rx_duplicate, rx_stale, rx_future;
    uint32_t rx_epoch;         // report from another master epoch
    uint32_t rx_bad_field;     // class/state out of range, reserved flag bits, non-finite feature
    uint32_t rx_unpolled;      // report from a slave not polled in this superframe
    uint32_t tx_failed;        // Transport::broadcast returned false
    uint32_t beacons_skipped;  // grid points skipped because the master fell >= 1 superframe behind
};

class Master {
public:
    static constexpr uint32_t EVENT_LOG = 16;  // ring of the most recent status events

    Master(Transport *tx, MasterListener *l = nullptr) : tx_(tx), l_(l) { reset(0, 0); }

    // (Re)start. The first beacon (superframe 0) is due at start_us. `epoch` identifies this boot
    // of the master: pass a fresh random value on hardware (e.g. esp_random()) that differs from
    // the previous boot's; tiers.h makes no platform calls itself.
    void reset(uint64_t start_us, uint32_t epoch) {
        phase_ = DONE;
        epoch_ = epoch;
        next_beacon_ = start_us;
        next_sf_ = 0;
        cur_sf_ = 0;
        poll_start_ = 0;
        deadline_ = 0;
        poll_idx_ = 0;
        attempt_ = 0;
        tx_seq_ = 0;
        n_events_ = 0;
        for (int i = 0; i < NUM_SLAVES; i++) {
            got_[i] = false;
            polled_[i] = false;
            have_[i] = false;
            alive_[i] = true;  // optimistic until proven dead
            missed_[i] = 0;
            memset(&latest_[i], 0, sizeof(ReportPkt));
        }
        memset(&stats_, 0, sizeof(stats_));
    }

    uint64_t next_wakeup() const {
        if (phase_ == WAIT_START) return poll_start_ < next_beacon_ ? poll_start_ : next_beacon_;
        if (phase_ == POLLING) return deadline_ < next_beacon_ ? deadline_ : next_beacon_;
        return next_beacon_;
    }

    void tick(uint64_t now) {
        if (now >= next_beacon_) {
            if (phase_ != DONE) finish(now);  // safety: should not happen, polling is bounded
            send_beacon(now);
        }
        if (phase_ == WAIT_START && now >= poll_start_) {
            phase_ = POLLING;
            poll_idx_ = 0;
            next_slave(now);
        } else if (phase_ == POLLING && now >= deadline_) {
            on_timeout(now);
        }
    }

    void on_packet(const uint8_t *buf, size_t len, uint64_t now) {
        Header h;
        if (!decode_header(buf, len, &h)) { stats_.rx_invalid++; return; }
        if (h.dst != ID_MASTER || h.type != T_REPORT) { stats_.rx_ignored++; return; }
        ReportPkt r;
        if (!decode_report(buf, len, &r)) { stats_.rx_invalid++; return; }
        if (r.h.src < 1 || r.h.src > NUM_SLAVES) { stats_.rx_invalid++; return; }
        int idx = r.h.src - 1;
        if (r.epoch != epoch_) { stats_.rx_epoch++; return; }
        if (!report_fields_ok(r)) { stats_.rx_bad_field++; return; }
        if (r.sf < cur_sf_) { stats_.rx_stale++; return; }
        if (r.sf > cur_sf_) { stats_.rx_future++; return; }
        if (got_[idx]) { stats_.rx_duplicate++; return; }
        if (phase_ != POLLING) { stats_.rx_stale++; return; }  // after polling closed: too late
        if (!polled_[idx]) { stats_.rx_unpolled++; return; }   // never asked in this superframe
        got_[idx] = true;
        have_[idx] = true;
        latest_[idx] = r;
        missed_[idx] = 0;
        stats_.reports[idx]++;
        if (!alive_[idx]) {
            alive_[idx] = true;
            record_event(now, (uint8_t)(idx + 1), true);
        }
        if (l_) l_->on_report((uint8_t)(idx + 1), r, now);
        if (poll_idx_ == idx) {
            poll_idx_++;
            next_slave(now);
        }
    }

    bool alive(uint8_t slave) const { return alive_[slave - 1]; }
    bool has_report(uint8_t slave) const { return have_[slave - 1]; }
    const ReportPkt &latest(uint8_t slave) const { return latest_[slave - 1]; }
    uint32_t missed(uint8_t slave) const { return missed_[slave - 1]; }
    uint32_t current_sf() const { return cur_sf_; }
    uint32_t epoch() const { return epoch_; }
    bool polling_active() const { return phase_ != DONE; }
    const MasterStats &stats() const { return stats_; }
    uint32_t event_count() const { return n_events_; }  // total ever recorded
    // k-th event overall (0-based); false if k >= count or it fell out of the ring.
    bool event_at(uint32_t k, StatusEvent *out) const {
        if (k >= n_events_ || n_events_ - k > EVENT_LOG) return false;
        *out = events_[k % EVENT_LOG];
        return true;
    }

private:
    enum Phase { WAIT_START, POLLING, DONE };

    void send_beacon(uint64_t now) {
        uint8_t buf[MAX_PAYLOAD];
        // Fixed grid: the schedule does not inherit this tick's lateness. If we are a whole
        // superframe or more behind, jump to the next grid point after now instead of bursting.
        // The superframe number advances by the skipped grid points too, so it stays equal to the
        // grid index: slaves that free-ran through the stall advanced their own count once per
        // superframe, and a master that fell behind them would have its beacons rejected as stale.
        uint64_t grid = next_beacon_;
        uint64_t k = 0;
        if (now >= grid + SUPERFRAME_US) {
            k = (now - grid) / SUPERFRAME_US;  // >= 1 grid points are already past
            stats_.beacons_skipped += (uint32_t)k;
        }
        next_beacon_ = grid + (k + 1) * SUPERFRAME_US;
        cur_sf_ = next_sf_ + (uint32_t)k;
        next_sf_ = cur_sf_ + 1;
        size_t n = encode_beacon(buf, ID_MASTER, ID_BROADCAST, tx_seq_++, cur_sf_, epoch_);
        stats_.beacons++;
        for (int i = 0; i < NUM_SLAVES; i++) { got_[i] = false; polled_[i] = false; }
        phase_ = WAIT_START;
        poll_start_ = now + POLL_START_OFFSET_US;
        if (!tx_->broadcast(buf, n, now)) stats_.tx_failed++;  // slaves will free-run this superframe
    }

    void next_slave(uint64_t now) {
        while (poll_idx_ < NUM_SLAVES && got_[poll_idx_]) poll_idx_++;
        if (poll_idx_ >= NUM_SLAVES) {
            finish(now);
            return;
        }
        attempt_ = 0;
        send_poll(now);
    }

    void send_poll(uint64_t now) {
        if (now + REPLY_TIMEOUT_US > next_beacon_) {  // would not complete before the next beacon
            poll_idx_++;
            next_slave(now);
            return;
        }
        uint8_t buf[MAX_PAYLOAD];
        size_t n = encode_poll(buf, ID_MASTER, (uint8_t)(poll_idx_ + 1), tx_seq_++, cur_sf_, epoch_, (uint8_t)attempt_);
        bool was_polled = polled_[poll_idx_];
        polled_[poll_idx_] = true;  // set before sending: the reply may be delivered synchronously
        deadline_ = now + REPLY_TIMEOUT_US;
        if (tx_->broadcast(buf, n, now)) {
            stats_.polls_tx[poll_idx_]++;
            if (attempt_ > 0) stats_.retries[poll_idx_]++;
        } else {
            // Failed send = failed attempt: do not wait for a reply that cannot come; the next
            // tick (due now) counts it as a timeout and moves to the retry / next slave.
            stats_.tx_failed++;
            polled_[poll_idx_] = was_polled;
            deadline_ = now;
        }
    }

    void on_timeout(uint64_t now) {
        if (attempt_ + 1 < POLL_ATTEMPTS) {
            attempt_++;
            send_poll(now);
        } else {
            poll_idx_++;
            next_slave(now);
        }
    }

    // End of this superframe's polling: account misses, declare DEAD.
    void finish(uint64_t now) {
        phase_ = DONE;
        for (int i = 0; i < NUM_SLAVES; i++) {
            if (got_[i]) continue;
            missed_[i]++;
            stats_.missed_total[i]++;
            if (missed_[i] >= DEAD_AFTER_MISSES && alive_[i]) {
                alive_[i] = false;
                record_event(now, (uint8_t)(i + 1), false);
            }
        }
    }

    void record_event(uint64_t now, uint8_t slave, bool alive) {
        StatusEvent &e = events_[n_events_ % EVENT_LOG];
        e.t_us = now;
        e.sf = cur_sf_;
        e.slave = slave;
        e.alive = alive;
        n_events_++;
        if (l_) l_->on_status(slave, alive, now);
    }

    Transport *tx_;
    MasterListener *l_;
    Phase phase_;
    uint64_t next_beacon_, poll_start_, deadline_;
    uint32_t epoch_, next_sf_, cur_sf_, attempt_, n_events_;
    int poll_idx_;
    uint16_t tx_seq_;
    bool got_[NUM_SLAVES], polled_[NUM_SLAVES], have_[NUM_SLAVES], alive_[NUM_SLAVES];
    uint32_t missed_[NUM_SLAVES];
    ReportPkt latest_[NUM_SLAVES];
    StatusEvent events_[EVENT_LOG];
    MasterStats stats_;
};

}  // namespace net
