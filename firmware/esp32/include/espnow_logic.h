#pragma once
// Platform-free logic used by the ESP32 ESP-NOW host (src/net_main.cpp, transport_espnow.h).
// No Arduino / ESP-IDF here so it is unit tested on the PC (test/test_espnow_logic, env espnow_logic).
//   * RX queue sizing constants
//   * RxFilter: cheap callback prefilter, frame validation, sender check (allowlist / trust-on-first-use)
//   * 64-bit sampling deadlines and their timing statistics
//   * poll round-trip histogram, noise-estimate statistics
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>
#include "net_proto.h"

namespace net {
namespace host {

// ---------------------------------------------------------------- rx queue sizing
// Frames a node can legitimately hear in one superframe: 1 beacon + every poll attempt (NUM_SLAVES *
// POLL_ATTEMPTS) + every report attempt (same count; slaves hear each other's reports too, broadcast).
constexpr size_t MAX_LEGIT_FRAMES_PER_SUPERFRAME = 1 + 2 * NUM_SLAVES * POLL_ATTEMPTS;  // 9
// A slave leaves its queue untouched for the whole 1.27 s sampling span + compute (<~0.1 s), which is
// less than one superframe, so one superframe of legitimate traffic is the worst case. 16 = ~1.8x that,
// the rest is slack for foreign ESP-NOW frames that pass the prefilter. Overflow is counted, never silent.
constexpr size_t RX_QUEUE_DEPTH = 16;
static_assert(RX_QUEUE_DEPTH >= MAX_LEGIT_FRAMES_PER_SUPERFRAME + 4, "rx queue too shallow for one superframe of traffic");
// The requirement the unit test enforces: one superframe of legitimate traffic (9) + the next beacon
// arriving before the window ends (1) + 6 foreign frames that pass the prefilter = 16.
constexpr size_t RX_QUEUE_MIN_DEPTH = MAX_LEGIT_FRAMES_PER_SUPERFRAME + 1 + 6;

constexpr uint8_t NUM_NODES = 1 + NUM_SLAVES;  // ids 0 (master), 1..NUM_SLAVES

// ---------------------------------------------------------------- rx classification
enum RxReason : uint8_t {
    RX_OK = 0,
    RX_NOT_PROTO,     // callback drops (too short/long, wrong magic): counted by the transport (rx_prefiltered), never by RxFilter, so not printed as a reject reason
    RX_BAD_FRAME,     // CRC / version / length bounds failed
    RX_BAD_ID,        // src id is not 0..NUM_SLAVES
    RX_OWN_ID,        // src id equals ours (duplicate node id or a spoof of us)
    RX_BAD_MAC,       // sender MAC multicast or all zero
    RX_MAC_MISMATCH,  // id seen from a MAC other than the allowlisted / pinned one
    RX_REASONS
};

static inline const char *rx_reason_name(int r) {
    static const char *const n[RX_REASONS] = {"ok", "not_proto", "bad_frame", "bad_id", "own_id", "bad_mac", "mac_mismatch"};
    return (r >= 0 && r < RX_REASONS) ? n[r] : "?";
}

// Cheap check run in the Wi-Fi task callback before a frame is copied into the queue, so foreign
// ESP-NOW traffic on the channel cannot fill the queue. Full CRC validation happens in the main loop.
static inline bool prefilter(const uint8_t *d, int len) {
    return d != nullptr && len >= (int)(HDR_LEN + CRC_LEN) && len <= (int)MAX_PAYLOAD && d[0] == MAGIC;
}

static inline bool mac_equal(const uint8_t *a, const uint8_t *b) { return memcmp(a, b, 6) == 0; }

// ---------------------------------------------------------------- sender check
// Binds node ids to MAC addresses. NOT authentication: the id and the MAC are both unauthenticated
// radio fields, anybody can forge both, and ESP-NOW broadcast is not encrypted. This only catches
// accidents (a second board flashed with the same id, a stray node) and the laziest spoofing.
//   * allow(id, mac): fixed allowlist entry (from node_config.h). Only that MAC is accepted for id.
//   * No allowlist entry: trust on first use. The MAC of the first CRC-valid frame claiming that id
//     is pinned until reboot; frames from other MACs claiming the id are rejected. A spoofer that is
//     first on air after a reboot wins the pin; replacing a board needs the peers to be rebooted.
class PeerPins {
public:
    explicit PeerPins(uint8_t own_id) : own_(own_id), pins_made_(0) {
        memset(mac_, 0, sizeof(mac_));
        memset(fixed_, 0, sizeof(fixed_));
        memset(have_, 0, sizeof(have_));
    }
    bool allow(uint8_t id, const uint8_t *mac) {
        if (id >= NUM_NODES || !mac_ok(mac)) return false;
        memcpy(mac_[id], mac, 6);
        have_[id] = true;
        fixed_[id] = true;
        return true;
    }
    RxReason check(uint8_t src, const uint8_t *mac) {
        if (src >= NUM_NODES) return RX_BAD_ID;
        if (src == own_) return RX_OWN_ID;
        if (!mac_ok(mac)) return RX_BAD_MAC;
        if (have_[src]) return mac_equal(mac_[src], mac) ? RX_OK : RX_MAC_MISMATCH;
        memcpy(mac_[src], mac, 6);
        have_[src] = true;
        pins_made_++;
        return RX_OK;
    }
    bool known(uint8_t id) const { return id < NUM_NODES && have_[id]; }
    bool is_fixed(uint8_t id) const { return id < NUM_NODES && fixed_[id]; }
    const uint8_t *mac(uint8_t id) const { return mac_[id]; }
    uint32_t pins_made() const { return pins_made_; }

private:
    static bool mac_ok(const uint8_t *m) {
        static const uint8_t zero[6] = {0, 0, 0, 0, 0, 0};
        return m != nullptr && !(m[0] & 1) && !mac_equal(m, zero);  // bit 0 of the first octet = multicast/broadcast
    }
    uint8_t own_;
    uint32_t pins_made_;
    uint8_t mac_[NUM_NODES][6];
    bool fixed_[NUM_NODES], have_[NUM_NODES];
};

// Main-loop frame gate: header validation (CRC, magic, version) then the sender check.
// Only frames returning RX_OK are handed to SlaveNode / Master::on_packet. A frame is only allowed
// to pin a MAC once its CRC is valid.
struct RxFilter {
    explicit RxFilter(uint8_t own_id) : pins(own_id) { memset(count, 0, sizeof(count)); }
    RxReason accept(const uint8_t *mac, const uint8_t *data, size_t len) {
        Header h;
        RxReason r;
        if (!decode_header(data, len, &h)) r = RX_BAD_FRAME;
        else r = pins.check(h.src, mac);
        count[r]++;
        return r;
    }
    PeerPins pins;
    uint32_t count[RX_REASONS];
};

// ---------------------------------------------------------------- 64-bit sampling deadlines
// Deadline of sample i, anchored to the time of sample 0 (t0). 64-bit all the way: a 32-bit micros()
// wraps after 71.6 min and (int32_t) differences break once the window crosses the wrap.
static inline uint64_t sample_deadline(uint64_t t0, uint32_t i) { return t0 + (uint64_t)i * SAMPLE_PERIOD_US; }

// Collects, for one window, the actual read time of each sample against its deadline.
struct WindowTimer {
    WindowTimer() { reset(0); }
    void reset(uint64_t t0_) {
        t0 = t0_;
        first = last = 0;
        n = 0;
        max_miss = 0;
        min_miss = 0;
    }
    // actual = time the sensor read started for sample i (deadline = sample_deadline(t0, i))
    void add(uint32_t i, uint64_t actual) {
        int64_t miss = (int64_t)(actual - sample_deadline(t0, i));  // two's complement: signed even if actual < deadline
        if (n == 0) { first = actual; max_miss = min_miss = miss; }
        if (miss > max_miss) max_miss = miss;
        if (miss < min_miss) min_miss = miss;
        last = actual;
        n++;
    }
    int64_t span() const { return (int64_t)(last - first); }  // first to last read; ideal SAMPLE_SPAN_US
    uint64_t t0, first, last;
    uint32_t n;
    int64_t max_miss, min_miss;
};

// ---------------------------------------------------------------- poll round-trip histogram
// Poll send to valid report receive (rx timestamp taken in the receive callback).
// Buckets (ms): <4, 4-6, 6-8, 8-10, 10-12, 12-14, 14-16, 16-18, >=18.
struct RttHist {
    static constexpr int NB = 9;
    RttHist() { clear(); }
    void clear() { memset(b, 0, sizeof(b)); n = 0; min_us = ~(uint32_t)0; max_us = 0; sum_us = 0; }
    static int bucket(uint32_t us) {
        if (us < 4000) return 0;
        int k = 1 + (int)((us - 4000) / 2000);
        return k >= NB ? NB - 1 : k;
    }
    void add(uint32_t us) {
        b[bucket(us)]++;
        n++;
        sum_us += us;
        if (us < min_us) min_us = us;
        if (us > max_us) max_us = us;
    }
    uint32_t b[NB], n, min_us, max_us;
    uint64_t sum_us;
};

// ---------------------------------------------------------------- poll timing bookkeeping
// Per slave per superframe: the time of the FIRST poll (attempt 0) and of the retry (if any). A report is
// timed from attempt 0, so a late reply to attempt 0 that lands after the retry was sent is not hidden as
// a ~1 ms reply to the retry. after_retry counts reports that arrived after the retry was sent (such a
// reply may answer either attempt); over_timeout counts reports whose time from attempt 0 reached
// REPLY_TIMEOUT_US (late for attempt 0). Replies after polling closed never reach on_report: the master
// counts those as rx_stale.
static inline bool poll_info(const uint8_t *b, size_t len, uint8_t *slave, uint8_t *attempt) {
    if (b == nullptr || len != POLL_LEN || b[0] != MAGIC || b[1] != T_POLL) return false;
    if (b[4] < 1 || b[4] > NUM_SLAVES) return false;
    *slave = b[4];
    *attempt = b[HDR_LEN + 8];
    return true;
}

struct RttTracker {
    RttTracker() { clear(); }
    void clear() {
        hist.clear();
        after_retry = over_timeout = unmatched = 0;
        for (int i = 0; i < NUM_SLAVES; i++) { first_[i] = retry_[i] = 0; open_[i] = false; }
    }
    void on_poll(uint8_t slave, uint8_t attempt, uint64_t now) {
        if (slave < 1 || slave > NUM_SLAVES) return;
        int i = slave - 1;
        if (attempt == 0) { first_[i] = now; retry_[i] = 0; open_[i] = true; }
        else if (open_[i]) retry_[i] = now;
    }
    // Returns true if the report was timed.
    bool on_report(uint8_t slave, uint64_t now) {
        if (slave < 1 || slave > NUM_SLAVES) return false;
        int i = slave - 1;
        if (!open_[i] || now < first_[i]) { unmatched++; return false; }
        open_[i] = false;
        uint64_t rtt = now - first_[i];
        hist.add(rtt > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)rtt);
        if (retry_[i] != 0 && now >= retry_[i]) after_retry++;
        if (rtt >= REPLY_TIMEOUT_US) over_timeout++;
        return true;
    }
    RttHist hist;
    uint32_t after_retry, over_timeout, unmatched;

private:
    uint64_t first_[NUM_SLAVES], retry_[NUM_SLAVES];
    bool open_[NUM_SLAVES];
};

// ---------------------------------------------------------------- noise estimate
struct NoiseStats {
    float mean, sd, mn, mx;
};
static inline NoiseStats noise_stats(const float *x, int n) {
    NoiseStats s = {0, 0, 0, 0};
    if (n <= 0) return s;
    float sum = 0;
    s.mn = s.mx = x[0];
    for (int i = 0; i < n; i++) {
        sum += x[i];
        if (x[i] < s.mn) s.mn = x[i];
        if (x[i] > s.mx) s.mx = x[i];
    }
    s.mean = sum / n;
    float v = 0;
    for (int i = 0; i < n; i++) v += (x[i] - s.mean) * (x[i] - s.mean);
    s.sd = sqrtf(v / n);
    return s;
}

// ---------------------------------------------------------------- channel scan (slaves)
// The master's ESP-NOW channel follows its Wi-Fi router (the radio has one channel), so a slave that is not synced
// (power-on or LOST) cycles through the channels 1..13 until it hears a valid master beacon, then stays there.
// The dwell per channel must exceed the beacon period so every dwell is guaranteed to contain a beacon.
constexpr uint8_t SCAN_CH_MIN = 1;
constexpr uint8_t SCAN_CH_MAX = 13;
constexpr uint64_t SCAN_DWELL_US = 1500000;
static_assert(SCAN_DWELL_US > SUPERFRAME_US, "scan dwell must outlast the beacon period (1.4 s)");

static inline uint8_t scan_clamp(uint8_t ch) { return (ch < SCAN_CH_MIN || ch > SCAN_CH_MAX) ? SCAN_CH_MIN : ch; }
static inline uint8_t scan_next(uint8_t ch) { return ch >= SCAN_CH_MAX ? SCAN_CH_MIN : (uint8_t)(ch + 1); }

// Pure decision logic; the host sets the radio channel when update() returns true.
//   * synced: stay on the current channel, scanning stops (= "lock on the first valid beacon": a node that is
//     not synced accepts any CRC-valid beacon, so becoming synced IS the lock);
//   * not synced: the first call starts a scan on the CURRENT channel (a master that restarted on the same
//     channel is found at once), after SCAN_DWELL_US without a lock the next channel is tried, 13 -> 1.
class ChannelScan {
public:
    explicit ChannelScan(uint8_t start_channel = SCAN_CH_MIN)
        : ch_(scan_clamp(start_channel)), scanning_(false), dwell_start_(0), hops_(0), locks_(0) {}
    uint8_t channel() const { return ch_; }
    bool scanning() const { return scanning_; }
    uint32_t hops() const { return hops_; }    // channel changes made so far
    uint32_t locks() const { return locks_; }  // scans that ended with a sync

    // Returns true when the radio must be switched to channel() now.
    bool update(uint64_t now, bool synced) {
        if (synced) {
            if (scanning_) { scanning_ = false; locks_++; }
            return false;
        }
        if (!scanning_) {
            scanning_ = true;
            dwell_start_ = now;
            return false;
        }
        if (now - dwell_start_ >= SCAN_DWELL_US) {
            ch_ = scan_next(ch_);
            dwell_start_ = now;
            hops_++;
            return true;
        }
        return false;
    }
    // Next time update() needs to run to change the channel (UINT64_MAX = never).
    uint64_t next_hop_us() const { return scanning_ ? dwell_start_ + SCAN_DWELL_US : ~(uint64_t)0; }

private:
    uint8_t ch_;
    bool scanning_;
    uint64_t dwell_start_;
    uint32_t hops_, locks_;
};

}  // namespace host
}  // namespace net
