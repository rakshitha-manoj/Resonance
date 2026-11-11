#pragma once
// Deterministic discrete-event simulator of 1 master + 2 slaves on a lossy broadcast medium.
// PC only (uses std::vector/string). Runs the same SlaveNode/Master code as the firmware.
//
// Model and limits: every transmission is delivered to every other node independently with
// probability 1-loss (i.i.d. loss, no burst/fading model), after delay_us + uniform jitter.
// There is NO collision, carrier-sense or airtime modelling: frames never overlap or block each
// other, and a frame takes zero time on air. Real loss is bursty and correlated; R5 must measure it.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>
#include <string>
#include <vector>
#include <queue>
#include <functional>
#include "tiers.h"

namespace netsim {

using namespace net;

// Small PRNG (splitmix64) so results do not depend on the C++ library's distributions.
struct Prng {
    uint64_t s;
    explicit Prng(uint64_t seed) : s(seed) {}
    uint64_t next() {
        uint64_t z = (s += 0x9E3779B97F4A7C15ULL);
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        return z ^ (z >> 31);
    }
    double u01() { return (double)(next() >> 11) * (1.0 / 9007199254740992.0); }
    uint32_t below(uint32_t n) { return (uint32_t)(next() % n); }
    // Uniform in [0, max] inclusive; safe for max == UINT32_MAX (below(max + 1) would divide by 0).
    uint32_t below_incl(uint32_t max) { return max == 0xFFFFFFFFu ? (uint32_t)next() : below(max + 1); }
};

// Pluggable source of 128-sample windows. get() must be a pure function of its arguments
// (the simulator may not call it in a fixed order relative to other randomness).
class WindowSource {
public:
    virtual ~WindowSource() {}
    virtual void get(uint8_t slave, uint32_t sf, int16_t *az, int16_t *gx) = 0;
};

// Default: az = gravity offset + sine + uniform noise, gx = small sine + noise.
class SynthSource : public WindowSource {
public:
    explicit SynthSource(uint32_t seed) : seed_(seed) {}
    void get(uint8_t slave, uint32_t sf, int16_t *az, int16_t *gx) override {
        Prng r(((uint64_t)seed_ << 40) ^ ((uint64_t)slave << 32) ^ sf ^ 0x5151ULL);
        double fz = 4.0 + 2.0 * slave + 4.0 * r.u01();     // Hz
        double ph = 6.283185307179586 * r.u01();
        double amp = 500.0 + 150.0 * slave + 200.0 * r.u01();  // counts, well above the noise gate
        for (int i = 0; i < 128; i++) {
            double t = i * 0.01;
            double nz = (r.u01() - 0.5) * 240.0;
            double ng = (r.u01() - 0.5) * 60.0;
            az[i] = (int16_t)(16384.0 + amp * sin(6.283185307179586 * fz * t + ph) + nz);
            gx[i] = (int16_t)(80.0 * sin(6.283185307179586 * (fz + 1.0) * t) + ng);
        }
    }

private:
    uint32_t seed_;
};

// Per-slave window traces from a binary file (tools/gen_traces.py). Layout, little endian:
//   "RTRC" | u32 version=1 | u32 n_slaves=2 | u32 n_windows | for slave 1, then slave 2: n_windows x (128 int16 az, 128 int16 gx).
// Window index = superframe number; past the end the last window is repeated (the CLI stops at n_windows by default).
class TraceSource : public WindowSource {
public:
    bool load(const char *path, std::string *err) {
        FILE *fp = fopen(path, "rb");
        if (!fp) { if (err) *err = std::string("cannot open ") + path; return false; }
        uint32_t h[4];
        if (fread(h, 4, 4, fp) != 4 || memcmp(h, "RTRC", 4) != 0 || h[1] != 1 || h[2] != (uint32_t)NUM_SLAVES || h[3] == 0) {
            fclose(fp);
            if (err) *err = std::string("bad trace header in ") + path;
            return false;
        }
        n_ = h[3];
        data_.assign((size_t)NUM_SLAVES * n_ * 256, 0);
        size_t want = data_.size();
        size_t got = fread(data_.data(), sizeof(int16_t), want, fp);
        fclose(fp);
        if (got != want) { if (err) *err = std::string("truncated trace file ") + path; return false; }
        return true;
    }
    uint32_t windows() const { return n_; }
    void get(uint8_t slave, uint32_t sf, int16_t *az, int16_t *gx) override {
        uint32_t w = sf < n_ ? sf : n_ - 1;
        const int16_t *p = &data_[((size_t)(slave - 1) * n_ + w) * 256];
        memcpy(az, p, 128 * sizeof(int16_t));
        memcpy(gx, p + 128, 128 * sizeof(int16_t));
    }

private:
    uint32_t n_ = 0;
    std::vector<int16_t> data_;
};

struct Action {
    int slave;    // 0 = the master (kill = power off, revive = restart with a fresh epoch), else 1..NUM_SLAVES
    uint32_t sf;  // takes effect at the beacon time of this superframe, before the beacon
};
// Same, at an arbitrary microsecond.
struct TimedAction {
    int node;
    uint64_t t_us;
};
// Make Transport::broadcast of `node` fail for sends in [from_us, until_us).
struct FailWindow {
    int node;
    uint64_t from_us, until_us;
};

struct SimConfig {
    uint32_t seed = 1;
    uint32_t superframes = 30;
    double loss = 0.0;      // per receiver link
    double corrupt = 0.0;   // per delivered copy: one random bit flipped
    uint32_t delay_us = 2000;
    uint32_t jitter_us = 1000;  // extra delay uniform in [0, jitter_us]
    uint32_t compute_us = 30000;  // slave feature+tree time after the last sample
    std::vector<Action> kills, revives;
    std::vector<TimedAction> kills_us, revives_us;
    std::vector<FailWindow> fail_windows;
    double tx_fail = 0.0;             // probability that any send fails (own PRNG stream)
    uint32_t master_late_max_us = 0;  // every master wakeup is delayed by uniform [0, this]
    uint64_t master_stall_from_us = 0, master_stall_until_us = 0;  // master host blocked: wakeups in [from, until) run at until
    bool quiet = false;               // do not accumulate the text log (res.log stays empty)
};

struct TxRec {
    uint64_t t;
    int node;  // 0 master, 1..2 slaves
    uint8_t type;
    size_t len;
};
struct Interval {
    uint64_t start, end;  // first and last sample time
};
struct ReportRec {  // one report accepted by the master
    uint64_t t;
    int slave;
    uint32_t sf, epoch, window;
    uint8_t raw, state, flags;
    uint32_t master_sf, master_epoch;
};
struct Restart {
    uint64_t t;
    uint32_t epoch;  // the new epoch
};
struct LatencyRec {
    int slave;
    uint64_t cause_t, detect_t;
    bool detected;
};

struct SimResult {
    std::vector<std::string> log;
    std::vector<TxRec> tx;
    std::vector<Interval> sampling[NUM_SLAVES + 1];
    std::vector<uint64_t> beacon_times;
    std::vector<ReportRec> reports;
    std::vector<Restart> restarts;  // master (re)starts after the first boot
    uint32_t reports_delivered[NUM_SLAVES + 1] = {0};
    uint32_t retries[NUM_SLAVES + 1] = {0};
    uint32_t polls_tx[NUM_SLAVES + 1] = {0};
    uint64_t bytes_tx[NUM_SLAVES + 1] = {0};
    uint32_t pkts_tx[NUM_SLAVES + 1][4] = {{0}};  // indexed by PktType
    uint32_t tx_failed[NUM_SLAVES + 1] = {0};
    uint32_t windows_rejected = 0;
    uint32_t min_delivery_us = 0xFFFFFFFFu, max_delivery_us = 0;  // delay_us + jitter actually drawn
    uint32_t dropped = 0, corrupted = 0, rx_rejected = 0;
    uint32_t unsynced_reports = 0, low_signal_reports = 0;
    // Invariant counters, recomputed by audit() from tx / sampling / beacon_times.
    uint32_t viol_tx_in_sampling = 0, viol_poll_overrun = 0;
    bool audited = false;
    std::vector<LatencyRec> death, recovery;
    uint32_t superframes = 0;

    // Recompute the invariant counters from the raw records:
    //  * no slave transmits during one of its own sampling windows
    //  * every poll (plus its reply timeout) ends before the next beacon (unless the master was
    //    restarted in between: a restart legitimately cuts the old superframe short)
    void audit() {
        viol_tx_in_sampling = viol_poll_overrun = 0;
        for (size_t i = 0; i < tx.size(); i++) {
            const TxRec &t = tx[i];
            if (t.node > 0 && t.node <= (int)NUM_SLAVES) {
                for (size_t j = 0; j < sampling[t.node].size(); j++)
                    if (t.t >= sampling[t.node][j].start && t.t <= sampling[t.node][j].end) { viol_tx_in_sampling++; break; }
            }
            if (t.type == T_POLL) {
                uint64_t nb = 0;
                bool have = false;
                for (size_t j = 0; j < beacon_times.size(); j++)
                    if (beacon_times[j] > t.t) { nb = beacon_times[j]; have = true; break; }
                bool restarted = false;
                for (size_t j = 0; j < restarts.size(); j++)
                    if (restarts[j].t > t.t && (!have || restarts[j].t <= nb)) restarted = true;
                if (have && !restarted && t.t + REPLY_TIMEOUT_US > nb) viol_poll_overrun++;
            }
        }
        audited = true;
    }

    std::string summary() const {
        std::string s;
        char b[256];
        snprintf(b, sizeof b, "summary: superframes=%u\n", superframes); s += b;
        for (int n = 1; n <= NUM_SLAVES; n++) {
            snprintf(b, sizeof b, "slave%d: reports_delivered=%u/%u polls_tx=%u retries=%u\n", n,
                     reports_delivered[n], superframes, polls_tx[n], retries[n]);
            s += b;
        }
        const char *names[3] = {"master", "slave1", "slave2"};
        for (int n = 0; n <= NUM_SLAVES; n++) {
            snprintf(b, sizeof b, "%s: bytes_tx=%llu pkts beacon=%u poll=%u report=%u\n", names[n],
                     (unsigned long long)bytes_tx[n], pkts_tx[n][T_BEACON], pkts_tx[n][T_POLL], pkts_tx[n][T_REPORT]);
            s += b;
        }
        snprintf(b, sizeof b, "network: dropped=%u corrupted=%u rx_rejected=%u unsynced_reports=%u low_signal_reports=%u\n",
                 dropped, corrupted, rx_rejected, unsynced_reports, low_signal_reports);
        s += b;
        snprintf(b, sizeof b, "faults: tx_failed master=%u s1=%u s2=%u windows_rejected=%u master_restarts=%u\n", tx_failed[0],
                 tx_failed[1], tx_failed[2], windows_rejected, (unsigned)restarts.size());
        s += b;
        for (size_t i = 0; i < restarts.size(); i++) {
            snprintf(b, sizeof b, "restart: master t=%llu epoch=0x%08x\n", (unsigned long long)restarts[i].t, restarts[i].epoch);
            s += b;
        }
        for (size_t i = 0; i < death.size(); i++) {
            const LatencyRec &d = death[i];
            if (d.detected)
                snprintf(b, sizeof b, "death: slave%d killed t=%llu detected t=%llu latency_us=%llu\n", d.slave,
                         (unsigned long long)d.cause_t, (unsigned long long)d.detect_t, (unsigned long long)(d.detect_t - d.cause_t));
            else
                snprintf(b, sizeof b, "death: slave%d killed t=%llu NOT detected\n", d.slave, (unsigned long long)d.cause_t);
            s += b;
        }
        for (size_t i = 0; i < recovery.size(); i++) {
            const LatencyRec &d = recovery[i];
            if (d.detected)
                snprintf(b, sizeof b, "recovery: slave%d revived t=%llu detected t=%llu latency_us=%llu\n", d.slave,
                         (unsigned long long)d.cause_t, (unsigned long long)d.detect_t, (unsigned long long)(d.detect_t - d.cause_t));
            else
                snprintf(b, sizeof b, "recovery: slave%d revived t=%llu NOT detected\n", d.slave, (unsigned long long)d.cause_t);
            s += b;
        }
        snprintf(b, sizeof b, "invariants: tx_in_sampling=%u poll_overrun=%u\n", viol_tx_in_sampling, viol_poll_overrun);
        s += b;
        return s;
    }
};

class Sim : public MasterListener {
public:
    Sim(const SimConfig &c, WindowSource *src = nullptr)
        : cfg_(c), net_(c.seed * 2654435761ULL + 12345), aux_(c.seed * 0x9E3779B97F4A7C15ULL + 777),
          txf_(c.seed * 0xD1B54A32D192ED03ULL + 4242), synth_(c.seed), src_(src ? src : &synth_),
          master_(&mtx_, this), s1_(1, &stx_[0]), s2_(2, &stx_[1]) {
        mtx_.sim = this; mtx_.node = 0;
        for (int i = 0; i < NUM_SLAVES; i++) { stx_[i].sim = this; stx_[i].node = i + 1; }
        for (int n = 0; n <= NUM_SLAVES; n++) { alive_[n] = true; sched_[n] = nominal_[n] = NEVER; }
        res.superframes = c.superframes;
    }

    SimResult res;
    // Optional: called after every master tick / packet (R2: FusionListener::service). Not used by the tests.
    std::function<void(const Master &, uint64_t)> master_hook;

    void run() {
        const uint64_t horizon = (uint64_t)cfg_.superframes * SUPERFRAME_US;
        // Kills are pushed before revives so a kill and revive at the same instant (a restart) keep that order.
        for (size_t i = 0; i < cfg_.kills.size(); i++) push(cfg_.kills[i].sf * (uint64_t)SUPERFRAME_US, K_KILL, cfg_.kills[i].slave, 0, nullptr, 0);
        for (size_t i = 0; i < cfg_.kills_us.size(); i++) push(cfg_.kills_us[i].t_us, K_KILL, cfg_.kills_us[i].node, 0, nullptr, 0);
        for (size_t i = 0; i < cfg_.revives.size(); i++) push(cfg_.revives[i].sf * (uint64_t)SUPERFRAME_US, K_REVIVE, cfg_.revives[i].slave, 0, nullptr, 0);
        for (size_t i = 0; i < cfg_.revives_us.size(); i++) push(cfg_.revives_us[i].t_us, K_REVIVE, cfg_.revives_us[i].node, 0, nullptr, 0);
        epoch_ = fresh_epoch();
        master_.reset(0, epoch_);
        post(0, 0);
        while (!q_.empty()) {
            Ev e = q_.top();
            if (e.t >= horizon) break;
            q_.pop();
            handle(e);
        }
        finalize();
    }

    Master &master() { return master_; }
    // Slave 1..NUM_SLAVES; any other id is a programming error (it used to alias slave 2).
    SlaveNode &slave(int n) {
        SlaveNode *p = slave_ptr(n);
        if (!p) { fprintf(stderr, "netsim: slave(%d) out of range\n", n); abort(); }
        return *p;
    }
    SlaveNode *slave_ptr(int n) { return n == 1 ? &s1_ : n == 2 ? &s2_ : nullptr; }
    uint32_t master_epoch() const { return epoch_; }

    // MasterListener
    void on_report(uint8_t slave, const ReportPkt &r, uint64_t now) override {
        res.reports_delivered[slave]++;
        if (r.flags & FLAG_UNSYNCED) res.unsynced_reports++;
        if (r.flags & FLAG_LOW_SIGNAL) res.low_signal_reports++;
        ReportRec rr = {now, slave, r.sf, r.epoch, r.window, r.raw, r.state, r.flags, master_.current_sf(), master_.epoch()};
        res.reports.push_back(rr);
        logf(now, "M   report slave=%u sf=%u win=%u raw=%u state=%u flags=0x%02x az_std=%.1f", slave, r.sf, r.window, r.raw, r.state, r.flags, (double)r.f[0]);
    }
    void on_status(uint8_t slave, bool alive, uint64_t now) override {
        status_.push_back({slave, alive, now});
        logf(now, "M   status slave=%u %s", slave, alive ? "ALIVE" : "DEAD");
    }

private:
    enum Kind { K_DELIVER, K_WAKE, K_WINDOW_DONE, K_KILL, K_REVIVE };
    struct Ev {
        uint64_t t, seq;
        int kind, node;
        uint64_t arg;
        std::vector<uint8_t> data;
        bool operator>(const Ev &o) const { return t != o.t ? t > o.t : seq > o.seq; }
    };
    struct NodeTx : Transport {
        Sim *sim;
        int node;
        bool broadcast(const uint8_t *buf, size_t len, uint64_t now) override { return sim->on_tx(node, buf, len, now); }
    };
    struct StatusRec { int slave; bool alive; uint64_t t; };

    void push(uint64_t t, int kind, int node, uint64_t arg, const uint8_t *d, size_t len) {
        Ev e;
        e.t = t; e.seq = seq_++; e.kind = kind; e.node = node; e.arg = arg;
        if (d) e.data.assign(d, d + len);
        q_.push(e);
    }

    void logf(uint64_t t, const char *fmt, ...) {
        if (cfg_.quiet) return;
        char b[256];
        int n = snprintf(b, sizeof b, "t=%llu ", (unsigned long long)t);
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(b + n, sizeof b - n, fmt, ap);
        va_end(ap);
        res.log.push_back(b);
    }

    static const char *tname(uint8_t t) { return t == T_BEACON ? "BEACON" : t == T_POLL ? "POLL" : t == T_REPORT ? "REPORT" : "?"; }
    static const char *nname(int n) { return n == 0 ? "M  " : n == 1 ? "S1 " : "S2 "; }

    // A fresh master epoch from the simulator's own PRNG, different from the current one.
    uint32_t fresh_epoch() {
        uint32_t e;
        do { e = (uint32_t)aux_.next(); } while (e == epoch_);
        return e;
    }

    bool send_fails(int node, uint64_t now) {
        for (size_t i = 0; i < cfg_.fail_windows.size(); i++) {
            const FailWindow &w = cfg_.fail_windows[i];
            if (w.node == node && now >= w.from_us && now < w.until_us) return true;
        }
        return cfg_.tx_fail > 0.0 && txf_.u01() < cfg_.tx_fail;
    }

    bool on_tx(int node, const uint8_t *buf, size_t len, uint64_t now) {
        uint8_t type = len > 1 ? buf[1] : 0;
        if (send_fails(node, now)) {
            res.tx_failed[node]++;
            logf(now, "%s tx %s FAILED", nname(node), tname(type));
            return false;
        }
        res.bytes_tx[node] += len;
        if (type < 4) res.pkts_tx[node][type]++;
        res.tx.push_back({now, node, type, len});
        if (type == T_BEACON) res.beacon_times.push_back(now);
        if (type == T_POLL) {
            res.polls_tx[buf[4]]++;
            if (buf[HDR_LEN + 8] > 0) res.retries[buf[4]]++;
        }
        logf(now, "%s tx %s len=%u", nname(node), tname(type), (unsigned)len);
        for (int r = 0; r <= NUM_SLAVES; r++) {
            if (r == node) continue;
            double pl = net_.u01();
            uint32_t jit = cfg_.jitter_us ? net_.below_incl(cfg_.jitter_us) : 0;
            double pc = net_.u01();
            if (pl < cfg_.loss) {
                res.dropped++;
                logf(now, "net drop %s->%s %s", nname(node), nname(r), tname(type));
                continue;
            }
            std::vector<uint8_t> copy(buf, buf + len);
            if (pc < cfg_.corrupt) {
                uint32_t bit = net_.below((uint32_t)(len * 8));
                copy[bit / 8] ^= (uint8_t)(1u << (bit % 8));
                res.corrupted++;
                logf(now, "net corrupt %s->%s %s bit=%u", nname(node), nname(r), tname(type), bit);
            }
            uint64_t d = (uint64_t)cfg_.delay_us + jit;
            uint32_t d32 = d > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)d;
            if (d32 < res.min_delivery_us) res.min_delivery_us = d32;
            if (d32 > res.max_delivery_us) res.max_delivery_us = d32;
            push(now + d, K_DELIVER, r, 0, copy.data(), len);
        }
        return true;
    }

    // After any call into node n: start requested windows, schedule its next wakeup.
    void post(int n, uint64_t now) {
        if (n == 0 && master_hook) master_hook(master_, now);
        if (n > 0) {
            SlaveNode &s = slave(n);
            if (s.window_requested()) {
                uint32_t token = s.take_window_request();
                uint64_t start = s.window_start();
                uint64_t end = start + SAMPLE_SPAN_US;
                res.sampling[n].push_back({start, end});
                logf(start, "%s window start sf=%u", nname(n), s.window_sf());
                push(end + cfg_.compute_us, K_WINDOW_DONE, n, (uint64_t)token | ((uint64_t)s.window_sf() << 32), nullptr, 0);
            }
        }
        uint64_t w = n == 0 ? master_.next_wakeup() : slave(n).next_wakeup();
        if (w != NEVER && w != nominal_[n]) {
            if (w < now) w = now;
            nominal_[n] = w;
            if (n == 0 && cfg_.master_late_max_us) w += aux_.below_incl(cfg_.master_late_max_us);
            if (n == 0 && w >= cfg_.master_stall_from_us && w < cfg_.master_stall_until_us) w = cfg_.master_stall_until_us;
            sched_[n] = w;
            push(w, K_WAKE, n, 0, nullptr, 0);
        }
    }

    void handle(const Ev &e) {
        int n = e.node;
        switch (e.kind) {
        case K_DELIVER:
            if (!alive_[n]) return;
            {
                uint32_t before = n == 0 ? master_.stats().rx_invalid : slave(n).stats().rx_invalid;
                if (n == 0) master_.on_packet(e.data.data(), e.data.size(), e.t);
                else slave(n).on_packet(e.data.data(), e.data.size(), e.t);
                uint32_t after = n == 0 ? master_.stats().rx_invalid : slave(n).stats().rx_invalid;
                res.rx_rejected += after - before;
            }
            post(n, e.t);
            break;
        case K_WAKE:
            if (sched_[n] == e.t) sched_[n] = nominal_[n] = NEVER;
            if (!alive_[n]) return;
            if (n == 0) master_.tick(e.t);
            else slave(n).tick(e.t);
            post(n, e.t);
            break;
        case K_WINDOW_DONE:
            if (!alive_[n]) return;
            {
                uint32_t token = (uint32_t)e.arg, sf = (uint32_t)(e.arg >> 32);
                int16_t az[128], gx[128];
                src_->get((uint8_t)n, sf, az, gx);
                // The slave itself rejects a delivery whose window was abandoned (token mismatch).
                if (slave(n).deliver_window(token, az, gx, e.t))
                    logf(e.t, "%s window done sf=%u state=%u", nname(n), sf, slave(n).report().state);
                else {
                    res.windows_rejected++;
                    logf(e.t, "%s window sf=%u abandoned (late delivery rejected)", nname(n), sf);
                }
            }
            post(n, e.t);
            break;
        case K_KILL:
            alive_[n] = false;
            kill_t_.push_back({n, false, e.t});
            logf(e.t, "%s KILLED", nname(n));
            break;
        case K_REVIVE:
            alive_[n] = true;
            sched_[n] = nominal_[n] = NEVER;
            if (n == 0) {
                // Master restart: superframe numbering starts again at 0 with a fresh epoch.
                epoch_ = fresh_epoch();
                master_.reset(e.t, epoch_);
                res.restarts.push_back({e.t, epoch_});
                logf(e.t, "M   RESTARTED epoch=0x%08x", epoch_);
            } else {
                slave(n).reset();
                logf(e.t, "%s REVIVED", nname(n));
            }
            revive_t_.push_back({n, true, e.t});
            post(n, e.t);
            break;
        }
    }

    void finalize() {
        res.audit();
        for (size_t i = 0; i < kill_t_.size(); i++) {
            if (kill_t_[i].slave < 1) continue;  // master restarts are in res.restarts
            LatencyRec l = {kill_t_[i].slave, kill_t_[i].t, 0, false};
            for (size_t j = 0; j < status_.size(); j++)
                if (status_[j].slave == l.slave && !status_[j].alive && status_[j].t >= l.cause_t) { l.detect_t = status_[j].t; l.detected = true; break; }
            res.death.push_back(l);
        }
        for (size_t i = 0; i < revive_t_.size(); i++) {
            if (revive_t_[i].slave < 1) continue;
            LatencyRec l = {revive_t_[i].slave, revive_t_[i].t, 0, false};
            for (size_t j = 0; j < status_.size(); j++)
                if (status_[j].slave == l.slave && status_[j].alive && status_[j].t >= l.cause_t) { l.detect_t = status_[j].t; l.detected = true; break; }
            res.recovery.push_back(l);
        }
    }

    SimConfig cfg_;
    Prng net_, aux_, txf_;
    SynthSource synth_;
    WindowSource *src_;
    NodeTx mtx_, stx_[NUM_SLAVES];
    Master master_;
    SlaveNode s1_, s2_;
    std::priority_queue<Ev, std::vector<Ev>, std::greater<Ev> > q_;
    uint64_t seq_ = 0;
    uint32_t epoch_ = 0;
    bool alive_[NUM_SLAVES + 1];
    uint64_t sched_[NUM_SLAVES + 1], nominal_[NUM_SLAVES + 1];
    std::vector<StatusRec> status_, kill_t_, revive_t_;
};

}  // namespace netsim
