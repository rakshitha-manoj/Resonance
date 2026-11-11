#pragma once
// Master-side fusion (R2): classify every superframe as AMBIENT, LOCAL(slave) or BUILDING from the two slaves' reports.
// Plain C++11, no Arduino, no heap, no platform calls. Behaviour is pinned bit for bit to RefFusion in
// models/training/fusion_eval.py by the parity vectors test/data/fusion_vectors.bin (tools/gen_fusion_vectors.py).
//
// Inputs (all from the master loop, same thread as Master):
//   on_report(slave, report, now)   a report accepted by Master (MasterListener::on_report)
//   on_status(slave, alive, now)    DEAD / ALIVE transition (MasterListener::on_status)
//   advance(master_sf, polling_active, now)   call after every Master::tick / on_packet (FusionListener::service does it).
//                                   It closes superframes that cannot receive more reports.
//   flush(now)                      close the superframe in progress (end of a run).
// Outputs: Sink::on_event for events and the periodic status, Sink::on_decision and then Sink::on_sample (report age, liveness,
// commissioning progress; read-only, built after the decision is final) for every closed superframe.
//
// Per superframe (closed when both slaves reported, or the master finished polling it, or a later superframe starts):
//   * until both slaves have a baseline: COMMISSIONING. Each slave's baseline = median of the az_std (f[0]) of its first
//     COMMISSION_WINDOWS reports (assumes quiet ambient conditions); the superframe that completes the last baseline is still
//     COMMISSIONING. COMMISSIONING takes precedence over NO_DATA/DEGRADED.
//   * no report from either slave (missing or DEAD): NO_DATA.
//   * one report: DEGRADED (degraded = the alive slave, 1 or 2). Only LOCAL(alive) or AMBIENT are possible; LOCAL iff the chosen
//     rule's single-slave condition holds: a: F; b: e; c: F and e (r_other cannot be checked, so it is dropped); d: F.
//     BUILDING cannot be told from LOCAL with one slave and is never output then.
//   * two reports: the chosen rule (see below), ratio r_i = az_std_i / max(baseline_i, BASELINE_MIN) in float32.
// Rule semantics (e_i = (r_i >= T) smoothed 2-of-3 by smooth.h; F_i = tree faulty indication, either state==faulty or, with
// USE_RAW, raw==faulty OR state==faulty):
//   a: both F = BUILDING, one F = LOCAL;  b: both e = BUILDING, one e = LOCAL;
//   c: both e = BUILDING; F_i and e_i and r_other < LOCAL_OTHER_MAX = LOCAL(i);
//   d: both e = BUILDING; else F on exactly one slave = LOCAL(that); F on both (not both e) = LOCAL on the higher r (tie: slave 1).
//   anything else AMBIENT.
// A slave without a report in a superframe does not advance its e history (it is held); the history is cleared when the slave
// comes back (ALIVE after DEAD). Baselines survive a slave outage and a master restart.
// Debounce (R2b, DEBOUNCE k): after the rule, a LOCAL(i)/BUILDING key that differs from the current output must hold k consecutive closed
// superframes before it is output; AMBIENT is reported meanwhile (an event class already being output is kept while another event
// class is pending); non-event decisions are output at once. Events (below) are generated from the debounced output.
// Events: one Event is emitted when the decision key (class, local slave, degraded slave) differs from the previous superframe's
// (and for the first superframe): EV_BEGIN if it is LOCAL/BUILDING and the previous class was not, else EV_CHANGE (event ends or
// switches, degraded/no-data/commissioning transitions). Every STATUS_EVERY_SF closed superframes an EV_STATUS repeats the current
// decision plus baselines, liveness and counters (for the dashboard). Event time = the `now` of the call that closed the superframe.
// Master restart: a report or advance with a superframe number below the next unclosed one restarts the stream (superframe
// numbering restarts), keeping baselines. A jump of more than MAX_CATCHUP_SF superframes is skipped, not replayed.
#include <stdint.h>
#include <stddef.h>
#include "net_proto.h"
#include "tiers.h"
#include "smooth.h"

namespace fusion {

// ---- chosen configuration (selected on the development block, see .claude/STRUCTURE.md "Fusion (R2)")
constexpr char  CHOSEN_RULE = 'b';
constexpr float CHOSEN_T = 3.0f;
constexpr bool  CHOSEN_USE_RAW = false;
constexpr uint8_t CHOSEN_DEBOUNCE = 1;   // R2b: consecutive superframes a LOCAL/BUILDING decision must hold before it is output

constexpr int      COMMISSION_WINDOWS = 20;
constexpr float    LOCAL_OTHER_MAX = 1.5f;   // rule c
constexpr float    BASELINE_MIN = 1.0f;      // counts; guards the division
constexpr uint32_t STATUS_EVERY_SF = 64;     // periodic status, about 90 s
constexpr uint32_t MAX_CATCHUP_SF = 32;

enum Cls : uint8_t { COMMISSIONING = 0, AMBIENT = 1, LOCAL = 2, BUILDING = 3, NO_DATA = 4, NUM_CLS = 5 };
enum EvKind : uint8_t { EV_BEGIN = 1, EV_CHANGE = 2, EV_STATUS = 3 };

static inline const char *cls_name(uint8_t c) {
    return c == COMMISSIONING ? "COMMISSIONING" : c == AMBIENT ? "AMBIENT" : c == LOCAL ? "LOCAL" : c == BUILDING ? "BUILDING" : c == NO_DATA ? "NO_DATA" : "?";
}

struct Config {
    char rule;
    float T;
    bool use_raw;
    uint8_t comm_n;          // <= COMMISSION_WINDOWS
    uint32_t status_every;
    uint8_t debounce;        // k >= 1
};
static inline Config chosen_config() {
    Config c = {CHOSEN_RULE, CHOSEN_T, CHOSEN_USE_RAW, (uint8_t)COMMISSION_WINDOWS, STATUS_EVERY_SF, CHOSEN_DEBOUNCE};
    return c;
}

struct Decision {
    uint32_t sf;
    uint8_t cls;       // Cls
    uint8_t slave;     // LOCAL: 1 or 2, else 0
    uint8_t degraded;  // 0, or the one slave that is alive (cls is then LOCAL or AMBIENT)
    float r[2];        // az_std / baseline per slave, -1 = unknown (no report, or commissioning)
};

struct Event {
    uint8_t kind;      // EvKind
    uint8_t cls, slave, degraded;
    uint32_t sf;
    uint64_t t_us;
    float r[2];
    float base[2];     // baselines, 0 = not known yet
    uint8_t alive_mask;  // bit0 slave 1 alive, bit1 slave 2 alive
    uint32_t closed;     // superframes closed so far
    uint32_t n_events;   // change events emitted so far (including this one for EV_BEGIN / EV_CHANGE)
    uint8_t commission_n[2];  // reports collected towards each slave's baseline (== comm_n once the baseline exists)
};

// One per closed superframe (Sink::on_sample), read-only: it is built after the decision is final and cannot influence it.
struct Sample {
    uint64_t t_us;
    uint32_t sf, closed;
    uint8_t cls, slave, degraded;  // the debounced output, as in Decision
    float r[2];                    // -1 = no report / commissioning
    uint8_t alive_mask;            // bit0 slave 1 alive, bit1 slave 2 alive
    int32_t age_sf[2];             // superframes since the slave's last report (0 = reported in this one), -1 = none since (re)start
    uint8_t commission_n[2];
};

class Sink {
public:
    virtual ~Sink() {}
    virtual void on_event(const Event &e) = 0;
    virtual void on_decision(const Decision &) {}
    virtual void on_sample(const Sample &) {}   // every closed superframe, after on_decision and before any event of it
};

struct Stats {
    uint32_t closed, by_cls[NUM_CLS], local_by_slave[2], degraded_sf;
    uint32_t rejected_reports, restarts, skipped_sf;
};

class Fusion {
public:
    explicit Fusion(Sink *sink = nullptr, const Config &c = chosen_config()) : sink_(sink), cfg_(c) { reset(); }

    void set_sink(Sink *s) { sink_ = s; }

    // Full reset including baselines and statistics.
    void reset() {
        for (int i = 0; i < 2; i++) {
            base_[i] = 0.0f;
            base_ok_[i] = false;
            comm_cnt_[i] = 0;
            alive_[i] = true;
        }
        clear_stream();
        started_ = false;
        armed_ = false;
        armed_sf_ = 0;
        next_sf_ = 0;
        closed_ = 0;
        n_events_ = 0;
        stats_ = Stats();
        stats_.closed = 0;
        for (int c = 0; c < NUM_CLS; c++) stats_.by_cls[c] = 0;
        stats_.local_by_slave[0] = stats_.local_by_slave[1] = 0;
        stats_.degraded_sf = stats_.rejected_reports = stats_.restarts = stats_.skipped_sf = 0;
    }

    void on_report(uint8_t slave, const net::ReportPkt &r, uint64_t now_us) {
        on_report_raw(slave, r.sf, r.raw, r.state, r.f[0], now_us);
    }

    void on_report_raw(uint8_t slave, uint32_t sf, uint8_t raw, uint8_t state, float f0, uint64_t now) {
        if (slave < 1 || slave > 2 || !net::finite_f(f0) || f0 < 0.0f) {
            stats_.rejected_reports++;
            return;
        }
        int i = slave - 1;
        if (!started_) {
            started_ = true;
            next_sf_ = sf;
            clear_slots();
        } else if (sf < next_sf_) {
            restart(sf, now);
        }
        close_upto(sf, now);
        if (!slot_have_[0] && !slot_have_[1]) slot_sf_ = sf;
        else if (slot_sf_ != sf) { clear_slots(); slot_sf_ = sf; }
        slot_have_[i] = true;
        slot_raw_[i] = raw;
        slot_state_[i] = state;
        slot_f0_[i] = f0;
        if (slot_have_[0] && slot_have_[1]) close_upto(sf + 1, now);
    }

    void on_status(uint8_t slave, bool alive, uint64_t /*now*/) {
        if (slave < 1 || slave > 2) return;
        int i = slave - 1;
        if (alive && !alive_[i]) {
            for (int k = 0; k < 3; k++) hist_[i][k] = -1;
            estate_[i] = 0;
        }
        alive_[i] = alive;
    }

    void advance(uint32_t master_sf, bool polling_active, uint64_t now) {
        if (polling_active) {
            if (!started_) {
                started_ = true;
                next_sf_ = master_sf;
            } else if (master_sf < next_sf_) {
                restart(master_sf, now);
            }
            armed_ = true;
            armed_sf_ = master_sf;
            close_upto(master_sf, now);
        } else if (armed_ && started_ && master_sf == armed_sf_) {
            close_upto(master_sf + 1, now);
        }
    }

    void flush(uint64_t now) {
        if (started_ && (slot_have_[0] || slot_have_[1]) && slot_sf_ >= next_sf_) close_upto(slot_sf_ + 1, now);
    }

    const Config &config() const { return cfg_; }
    bool commissioned() const { return base_ok_[0] && base_ok_[1]; }
    bool baseline_ok(uint8_t slave) const { return base_ok_[slave - 1]; }
    float baseline(uint8_t slave) const { return base_[slave - 1]; }
    bool slave_alive(uint8_t slave) const { return alive_[slave - 1]; }
    uint32_t closed() const { return closed_; }
    uint32_t events_emitted() const { return n_events_; }
    const Decision &last_decision() const { return last_; }
    const Stats &stats() const { return stats_; }

private:
    void clear_slots() {
        slot_have_[0] = slot_have_[1] = false;
        slot_sf_ = 0;
    }
    void clear_stream() {
        clear_slots();
        for (int i = 0; i < 2; i++) {
            for (int k = 0; k < 3; k++) hist_[i][k] = -1;
            estate_[i] = 0;
        }
        for (int i = 0; i < 2; i++) { last_rep_ok_[i] = false; last_rep_sf_[i] = 0; }
        key_valid_ = false;
        out_valid_ = false;
        out_cls_ = out_slave_ = out_deg_ = 0;
        cand_cls_ = cand_slave_ = cand_deg_ = 0;
        cand_n_ = 0;
        key_cls_ = key_slave_ = key_deg_ = 0;
        memset(&last_, 0, sizeof(last_));
    }

    void restart(uint32_t new_sf, uint64_t now) {
        if ((slot_have_[0] || slot_have_[1]) && slot_sf_ >= next_sf_) close_upto(slot_sf_ + 1, now);
        clear_stream();
        next_sf_ = new_sf;
        armed_ = false;
        stats_.restarts++;
    }

    void close_upto(uint32_t target, uint64_t now) {
        if (target <= next_sf_) return;
        if (target - next_sf_ > MAX_CATCHUP_SF) {
            stats_.skipped_sf += (target - next_sf_) - MAX_CATCHUP_SF;
            next_sf_ = target - MAX_CATCHUP_SF;
        }
        while (next_sf_ < target) {
            bool have = (slot_have_[0] || slot_have_[1]) && slot_sf_ == next_sf_;
            finalize(next_sf_, have, now);
            if (have) clear_slots();
            next_sf_++;
        }
        if ((slot_have_[0] || slot_have_[1]) && slot_sf_ < next_sf_) clear_slots();
    }

    float ratio(int i, float f0) const {
        float b = base_[i];
        if (b < BASELINE_MIN) b = BASELINE_MIN;
        return f0 / b;
    }

    static float median_of(float *s, int n) {
        for (int a = 1; a < n; a++) {  // insertion sort
            float v = s[a];
            int b = a - 1;
            while (b >= 0 && s[b] > v) { s[b + 1] = s[b]; b--; }
            s[b + 1] = v;
        }
        return (n & 1) ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) * 0.5f;
    }

    void finalize(uint32_t sf, bool have, uint64_t now) {
        closed_++;
        for (int i = 0; i < 2; i++)
            if (have && slot_have_[i]) { last_rep_ok_[i] = true; last_rep_sf_[i] = sf; }  // bookkeeping for Sample only
        Decision d;
        d.sf = sf;
        d.r[0] = d.r[1] = -1.0f;
        d.slave = d.degraded = 0;
        if (!commissioned()) {
            for (int i = 0; i < 2; i++) {
                if (have && slot_have_[i] && !base_ok_[i]) {
                    comm_buf_[i][comm_cnt_[i]++] = slot_f0_[i];
                    if (comm_cnt_[i] == cfg_.comm_n) {
                        base_[i] = median_of(comm_buf_[i], cfg_.comm_n);
                        base_ok_[i] = true;
                    }
                }
            }
            d.cls = COMMISSIONING;
        } else {
            decide(have, &d);
        }
        debounce(&d);
        last_ = d;
        stats_.closed = closed_;
        stats_.by_cls[d.cls]++;
        if (d.cls == LOCAL && d.slave >= 1) stats_.local_by_slave[d.slave - 1]++;
        if (d.degraded) stats_.degraded_sf++;
        if (sink_) {
            sink_->on_decision(d);
            Sample s;
            s.t_us = now;
            s.sf = d.sf;
            s.closed = closed_;
            s.cls = d.cls;
            s.slave = d.slave;
            s.degraded = d.degraded;
            s.r[0] = d.r[0];
            s.r[1] = d.r[1];
            s.alive_mask = (uint8_t)((alive_[0] ? 1 : 0) | (alive_[1] ? 2 : 0));
            for (int i = 0; i < 2; i++) {
                s.age_sf[i] = last_rep_ok_[i] ? (int32_t)(d.sf - last_rep_sf_[i]) : -1;
                s.commission_n[i] = comm_cnt_[i];
            }
            sink_->on_sample(s);
        }
        if (!key_valid_ || d.cls != key_cls_ || d.slave != key_slave_ || d.degraded != key_deg_) {
            bool was_event = key_valid_ && (key_cls_ == LOCAL || key_cls_ == BUILDING);
            uint8_t kind = ((d.cls == LOCAL || d.cls == BUILDING) && !was_event) ? EV_BEGIN : EV_CHANGE;
            n_events_++;
            emit(kind, d, now);
            key_valid_ = true;
            key_cls_ = d.cls;
            key_slave_ = d.slave;
            key_deg_ = d.degraded;
        }
        if (closed_ % cfg_.status_every == 0) emit(EV_STATUS, d, now);
    }

    // R2b debounce. A LOCAL(i)/BUILDING key that differs from the current output must hold cfg_.debounce consecutive closed
    // superframes before it is output (and so before an EV_BEGIN). Meanwhile AMBIENT is reported (degraded kept), or the event
    // class already being output is kept. Any non-event decision is output at once and clears the count.
    void debounce(Decision *d) {
        bool ev = d->cls == LOCAL || d->cls == BUILDING;
        if (!ev) {
            cand_n_ = 0;
            out_valid_ = true;
            out_cls_ = d->cls; out_slave_ = d->slave; out_deg_ = d->degraded;
            return;
        }
        if (out_valid_ && out_cls_ == d->cls && out_slave_ == d->slave && out_deg_ == d->degraded) {
            cand_n_ = 0;
            return;
        }
        if (cand_n_ > 0 && cand_cls_ == d->cls && cand_slave_ == d->slave && cand_deg_ == d->degraded) {
            cand_n_++;
        } else {
            cand_cls_ = d->cls; cand_slave_ = d->slave; cand_deg_ = d->degraded;
            cand_n_ = 1;
        }
        if (cand_n_ >= cfg_.debounce) {
            cand_n_ = 0;
            out_valid_ = true;
            out_cls_ = d->cls; out_slave_ = d->slave; out_deg_ = d->degraded;
            return;
        }
        if (out_valid_ && (out_cls_ == LOCAL || out_cls_ == BUILDING)) {
            d->cls = out_cls_; d->slave = out_slave_; d->degraded = out_deg_;
        } else {
            d->cls = AMBIENT;
            d->slave = 0;
        }
    }

    void emit(uint8_t kind, const Decision &d, uint64_t now) {
        if (!sink_) return;
        Event e;
        e.kind = kind;
        e.cls = d.cls;
        e.slave = d.slave;
        e.degraded = d.degraded;
        e.sf = d.sf;
        e.t_us = now;
        e.r[0] = d.r[0];
        e.r[1] = d.r[1];
        e.base[0] = base_ok_[0] ? base_[0] : 0.0f;
        e.base[1] = base_ok_[1] ? base_[1] : 0.0f;
        e.alive_mask = (uint8_t)((alive_[0] ? 1 : 0) | (alive_[1] ? 2 : 0));
        e.closed = closed_;
        e.n_events = n_events_;
        e.commission_n[0] = comm_cnt_[0];
        e.commission_n[1] = comm_cnt_[1];
        sink_->on_event(e);
    }

    void decide(bool have, Decision *d) {
        bool avail[2] = {have && slot_have_[0], have && slot_have_[1]};
        bool e[2] = {false, false}, F[2] = {false, false};
        for (int i = 0; i < 2; i++) {
            if (!avail[i]) continue;
            d->r[i] = ratio(i, slot_f0_[i]);
            estate_[i] = smooth_update(hist_[i], d->r[i] >= cfg_.T ? 1 : 0, estate_[i]);
            e[i] = estate_[i] == 1;
            F[i] = slot_state_[i] == 2 || (cfg_.use_raw && slot_raw_[i] == 2);
        }
        int n = (avail[0] ? 1 : 0) + (avail[1] ? 1 : 0);
        d->cls = AMBIENT;
        if (n == 0) {
            d->cls = NO_DATA;
            return;
        }
        if (n == 1) {
            int i = avail[0] ? 0 : 1;
            bool loc = cfg_.rule == 'b' ? e[i] : cfg_.rule == 'c' ? (F[i] && e[i]) : F[i];
            d->degraded = (uint8_t)(i + 1);
            if (loc) {
                d->cls = LOCAL;
                d->slave = (uint8_t)(i + 1);
            }
            return;
        }
        const float *r = d->r;
        switch (cfg_.rule) {
        case 'a':
            if (F[0] && F[1]) d->cls = BUILDING;
            else if (F[0] || F[1]) { d->cls = LOCAL; d->slave = F[0] ? 1 : 2; }
            break;
        case 'b':
            if (e[0] && e[1]) d->cls = BUILDING;
            else if (e[0] || e[1]) { d->cls = LOCAL; d->slave = e[0] ? 1 : 2; }
            break;
        case 'c':
            if (e[0] && e[1]) d->cls = BUILDING;
            else if (F[0] && e[0] && r[1] < LOCAL_OTHER_MAX) { d->cls = LOCAL; d->slave = 1; }
            else if (F[1] && e[1] && r[0] < LOCAL_OTHER_MAX) { d->cls = LOCAL; d->slave = 2; }
            break;
        default:  // 'd'
            if (e[0] && e[1]) d->cls = BUILDING;
            else if (F[0] && !F[1]) { d->cls = LOCAL; d->slave = 1; }
            else if (F[1] && !F[0]) { d->cls = LOCAL; d->slave = 2; }
            else if (F[0] && F[1]) { d->cls = LOCAL; d->slave = r[0] >= r[1] ? 1 : 2; }
            break;
        }
    }

    Sink *sink_;
    Config cfg_;
    float base_[2];
    bool base_ok_[2];
    float comm_buf_[2][COMMISSION_WINDOWS];
    uint8_t comm_cnt_[2];
    int8_t hist_[2][3];
    int estate_[2];
    bool alive_[2];
    bool started_, armed_;
    uint32_t armed_sf_, next_sf_, slot_sf_, closed_, n_events_;
    bool slot_have_[2];
    uint8_t slot_raw_[2], slot_state_[2];
    float slot_f0_[2];
    bool key_valid_, out_valid_;
    uint8_t out_cls_, out_slave_, out_deg_, cand_cls_, cand_slave_, cand_deg_;
    uint32_t cand_n_;
    uint8_t key_cls_, key_slave_, key_deg_;
    bool last_rep_ok_[2];
    uint32_t last_rep_sf_[2];
    Decision last_;
    Stats stats_;
};

// Attach to Master: Master m(&tx, &listener). Forwards to `next` (e.g. the netsim recorder) after fusion.
// Call service(master, now) after every Master::tick / on_packet so superframes close when polling ends.
class FusionListener : public net::MasterListener {
public:
    FusionListener(Fusion *f, net::MasterListener *next = nullptr) : f_(f), next_(next) {}
    void on_report(uint8_t slave, const net::ReportPkt &r, uint64_t now_us) override {
        f_->on_report(slave, r, now_us);
        if (next_) next_->on_report(slave, r, now_us);
    }
    void on_status(uint8_t slave, bool alive, uint64_t now_us) override {
        f_->on_status(slave, alive, now_us);
        if (next_) next_->on_status(slave, alive, now_us);
    }
    void service(const net::Master &m, uint64_t now_us) { f_->advance(m.current_sf(), m.polling_active(), now_us); }

private:
    Fusion *f_;
    net::MasterListener *next_;
};

}  // namespace fusion
