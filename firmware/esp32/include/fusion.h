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
//   f (FINAL, Phase C1/C2a): e_i = b_i OR a_i with b_i = (r_i >= T_b) and a_i = (AE ratio_i >= T [= T_AE]), EACH smoothed 2-of-3
//      separately (own history per slave and per flag), then as b: both e = BUILDING, one e = LOCAL(that), else AMBIENT. The AE flag
//      input is false (and still advances its history) while the slave's AE_READY is not set and while the reported az_std f[0] <
//      gate (the node noise gate, applied before smoothing exactly as in tiers.h). Degraded (one report): LOCAL(alive) iff e of the
//      alive slave. Both histories are held while a slave has no report and cleared when it returns (ALIVE after DEAD) or the stream
//      restarts. Reference: View3 / flags_for in models/training/fusion_eval_r3.py; RefFusion in fusion_eval.py is bit-exact.
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
// DAMAGE channel (Phase A, damage.h; independent of the class decision): once per CLOSED superframe each slave contributes its
// f1_drop if it reported with F1_VALID && F1_COMMISSIONED and a finite drop, otherwise nothing; BEGIN after DMG_P consecutive pooled
// drops >= DMG_DELTA, END after DMG_P below DMG_DELTA/2 (Sink::on_damage). It is fed during fusion commissioning too and survives a
// stream restart (it describes the structure, not the stream). The commissioned f1_0 of a slave is derived on the master as
// f1_est / (1 - f1_drop) from its last usable report.
// Contributor guard: the DAMAGE state is defined by the slaves that contributed at the last transition. If one of them has had no usable
// drop for DMG_ABSENT_HOLD (8) superframes, the decision is HELD and the run counters restart (stats.dmg_held): ACTIVE damage can then
// not end (the slave that saw it may have rebooted and the other would see nothing), INACTIVE a member absent for a full P leaves the set.
// A slave whose f1_0 is reset (COMMISSIONED false -> true after having been true, or f1_0 jumping by > 0.5 %) is announced with a
// DamageEvent whose baseline_slave is 1 or 2 (JSON damage line, state BASELINE). f1_0, the AE normaliser and the b baseline are RAM-only.
// Master restart: a report or advance with a superframe number below the next unclosed one restarts the stream (superframe
// numbering restarts), keeping baselines. A jump of more than MAX_CATCHUP_SF superframes is skipped, not replayed.
#include <stdint.h>
#include <stddef.h>
#include "net_proto.h"
#include "tiers.h"
#include "smooth.h"
#include "damage.h"

namespace fusion {

// ---- chosen configuration (selected on the development block, see .claude/STRUCTURE.md "Fusion (R2)")
// Rule f (b OR e) is the pre-registered fresh-block decision of Phase C1 (fusion_eval_r3_test.md); the constants below are checked
// against models/selected/validation/fusion_eval_r3_selected.json (and the gate against fusion_eval_r3_dev.json) by test_fusion.
constexpr char  CHOSEN_RULE = 'f';
constexpr float CHOSEN_T_AE = 6.0f;        // AE ratio threshold (the "T" of rule f)
constexpr float CHOSEN_T_B = 3.0f;         // std-ratio threshold of the b part
constexpr float CHOSEN_T = CHOSEN_T_AE;    // Config::T: the rule's primary threshold (AE for f; b's T for the old rules)
constexpr float CHOSEN_AE_GATE = 92.12f;   // az_std below this (counts) => the AE flag is false (the provisional node noise gate)
constexpr bool  CHOSEN_USE_RAW = false;
constexpr uint8_t CHOSEN_DEBOUNCE = 1;   // R2b: consecutive superframes a LOCAL/BUILDING decision must hold before it is output

constexpr int      COMMISSION_WINDOWS = 20;
constexpr float    LOCAL_OTHER_MAX = 1.5f;   // rule c
constexpr float    BASELINE_MIN = 1.0f;      // counts; guards the division
constexpr uint32_t STATUS_EVERY_SF = 64;     // periodic status, about 90 s
constexpr uint32_t MAX_CATCHUP_SF = 32;
constexpr uint8_t  DMG_ABSENT_HOLD = 8;      // superframes (11 s) a DAMAGE contributor may be missing before the decision is held

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
    float T_b;               // rule f only: std-ratio threshold of the b part (T is then the AE threshold)
    float gate;              // rule f only: az_std < gate => AE flag false
};
static inline Config chosen_config() {
    Config c = {CHOSEN_RULE, CHOSEN_T, CHOSEN_USE_RAW, (uint8_t)COMMISSION_WINDOWS, STATUS_EVERY_SF, CHOSEN_DEBOUNCE, CHOSEN_T_B, CHOSEN_AE_GATE};
    return c;
}

struct Decision {
    uint32_t sf;
    uint8_t cls;       // Cls
    uint8_t slave;     // LOCAL: 1 or 2, else 0
    uint8_t degraded;  // 0, or the one slave that is alive (cls is then LOCAL or AMBIENT)
    float r[2];        // az_std / baseline per slave, -1 = unknown (no report, or commissioning)
    float ae[2];       // AE ratio per slave, -1 = unknown (no report, or AE not ready)
    float f1[2];       // f1 estimate (Hz) per slave, NAN = unknown (no report or not valid)
    float drop[2];     // f1 drop per slave, NAN = unknown (no report, not valid or not commissioned)
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
    float ae[2], f1[2], drop[2];  // as in Decision
    uint8_t dmg;              // DAMAGE state (1 = active) after this superframe
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
    float ae[2], f1[2], drop[2];   // as in Decision
    uint8_t dmg;                   // DAMAGE state (1 = active) after this superframe
};

// DAMAGE begin / end (Sink::on_damage), after the Sample of the superframe that triggered it.
struct DamageEvent {
    uint64_t t_us;
    uint32_t sf;
    bool begin;        // true BEGIN, false END (not meaningful for a BASELINE note)
    uint8_t baseline_slave = 0;  // 0 = a DAMAGE BEGIN / END; 1 or 2 = a BASELINE note: that slave's f1_0 was reset (it recommissioned)
    float drop;        // pooled drop of the triggering superframe
    float f1[2];       // each slave's f1 estimate in that superframe (NAN = no usable estimate)
    float f1_0[2];     // each slave's commissioned f1 (last usable report, NAN = never)
};

// One slave's report as fusion needs it (see Fusion::on_report_ex).
struct SlaveIn {
    uint8_t raw, state;
    float f0;               // az_std
    uint8_t xflags;         // FLAG_AE_READY | FLAG_F1_VALID | FLAG_F1_COMMISSIONED (others ignored)
    float ae, f1_est, f1_drop;
};

class Sink {
public:
    virtual ~Sink() {}
    virtual void on_event(const Event &e) = 0;
    virtual void on_decision(const Decision &) {}
    virtual void on_sample(const Sample &) {}   // every closed superframe, after on_decision and before any event of it
    virtual void on_damage(const DamageEvent &) {}  // DAMAGE begin / end, after the sample and before the class event of that superframe
};

struct Stats {
    uint32_t closed, by_cls[NUM_CLS], local_by_slave[2], degraded_sf;
    uint32_t rejected_reports, restarts, skipped_sf;
    uint32_t ae_bad, f1_bad;          // optional fields found unusable here (a Master already filters them: normally 0)
    uint32_t dmg_begins, dmg_ends;
    uint32_t dmg_held;                // superframes in which the DAMAGE decision was held because a contributing slave was missing
    uint32_t baselines;               // slave f1_0 baselines seen to be reset (recommissioning), each also logged as a damage BASELINE line
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
            f1_0_[i] = NAN;
            dmg_absent_[i] = 0;
            comm_ever_[i] = comm_now_[i] = false;
        }
        dmg_set_ = 3;
        dmg_init_default(&dmg_);
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
        stats_.ae_bad = stats_.f1_bad = stats_.dmg_begins = stats_.dmg_ends = stats_.dmg_held = stats_.baselines = 0;
    }

    // A report accepted by the Master. Its optional fields are sanitized again here (a Master already did it; a direct caller may not).
    void on_report(uint8_t slave, const net::ReportPkt &r, uint64_t now_us) {
        net::ReportPkt c = r;
        net::report_sanitize(&c, &stats_.ae_bad, &stats_.f1_bad);
        SlaveIn in;
        in.raw = c.raw;
        in.state = c.state;
        in.f0 = c.f[0];
        in.xflags = (uint8_t)(c.flags & (net::FLAG_AE_READY | net::FLAG_F1_VALID | net::FLAG_F1_COMMISSIONED));
        in.ae = c.ae_ratio;
        in.f1_est = c.f1_est;
        in.f1_drop = c.f1_drop;
        on_report_ex(slave, r.sf, in, now_us);
    }

    // Tree / std-ratio inputs only (rules a-d, no AE, no f1).
    void on_report_raw(uint8_t slave, uint32_t sf, uint8_t raw, uint8_t state, float f0, uint64_t now) {
        SlaveIn in;
        in.raw = raw;
        in.state = state;
        in.f0 = f0;
        in.xflags = 0;
        in.ae = 0.0f;
        in.f1_est = in.f1_drop = NAN;
        on_report_ex(slave, sf, in, now);
    }

    // The flags should be consistent (see net::report_sanitize): AE_READY => ae finite, F1_VALID => f1_est finite,
    // F1_VALID && F1_COMMISSIONED => f1_drop finite. Inconsistent input is downgraded (flag dropped, counted), never used.
    void on_report_ex(uint8_t slave, uint32_t sf, const SlaveIn &in, uint64_t now) {
        float f0 = in.f0;
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
        slot_raw_[i] = in.raw;
        slot_state_[i] = in.state;
        slot_f0_[i] = f0;
        uint8_t xf = (uint8_t)(in.xflags & (net::FLAG_AE_READY | net::FLAG_F1_VALID | net::FLAG_F1_COMMISSIONED));
        if ((xf & net::FLAG_AE_READY) && !(net::finite_f(in.ae) && in.ae >= 0.0f)) { xf = (uint8_t)(xf & ~net::FLAG_AE_READY); stats_.ae_bad++; }
        if ((xf & net::FLAG_F1_VALID) && !net::finite_f(in.f1_est)) { xf = (uint8_t)(xf & ~net::FLAG_F1_VALID); stats_.f1_bad++; }
        if ((xf & net::FLAG_F1_VALID) && (xf & net::FLAG_F1_COMMISSIONED) && !net::finite_f(in.f1_drop)) { xf = (uint8_t)(xf & ~net::FLAG_F1_COMMISSIONED); stats_.f1_bad++; }
        slot_x_[i] = xf;
        slot_ae_[i] = in.ae;
        slot_f1_[i] = in.f1_est;
        slot_drop_[i] = in.f1_drop;
        if (slot_have_[0] && slot_have_[1]) close_upto(sf + 1, now);
    }

    void on_status(uint8_t slave, bool alive, uint64_t /*now*/) {
        if (slave < 1 || slave > 2) return;
        int i = slave - 1;
        if (alive && !alive_[i]) {
            for (int k = 0; k < 3; k++) hist_[i][k] = hist_a_[i][k] = -1;
            estate_[i] = astate_[i] = 0;
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
    bool damage_active() const { return dmg_.active != 0; }
    const dmg_t &damage() const { return dmg_; }
    float slave_f1_0(uint8_t slave) const { return f1_0_[slave - 1]; }   // NAN until a usable report
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
            for (int k = 0; k < 3; k++) hist_[i][k] = hist_a_[i][k] = -1;
            estate_[i] = astate_[i] = 0;
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
        // per-slave AE / f1 view of this superframe (what the log shows) and the DAMAGE feed
        uint8_t dok[2] = {0, 0};
        float ddrop[2] = {NAN, NAN};
        uint8_t note = 0;   // bit i: slave i+1's baseline (f1_0) was reset
        for (int i = 0; i < 2; i++) {
            d.ae[i] = -1.0f;
            d.f1[i] = d.drop[i] = NAN;
            if (!(have && slot_have_[i])) continue;
            uint8_t xf = slot_x_[i];
            {
                bool c = (xf & net::FLAG_F1_COMMISSIONED) != 0;
                if (c && !comm_now_[i] && comm_ever_[i]) note |= (uint8_t)(1 << i);   // commissioned again after having been commissioned: a reboot
                if (c) comm_ever_[i] = true;
                comm_now_[i] = c;
            }
            if (xf & net::FLAG_AE_READY) d.ae[i] = slot_ae_[i];
            if (xf & net::FLAG_F1_VALID) d.f1[i] = slot_f1_[i];
            if ((xf & net::FLAG_F1_VALID) && (xf & net::FLAG_F1_COMMISSIONED)) {
                d.drop[i] = slot_drop_[i];
                dok[i] = 1;
                ddrop[i] = slot_drop_[i];
                if (1.0f - slot_drop_[i] > 0.01f) {
                    float old = f1_0_[i], nw = slot_f1_[i] / (1.0f - slot_drop_[i]);
                    if (old == old && fabsf(nw - old) > 0.005f * old) note |= (uint8_t)(1 << i);   // f1_0 jumped by > 0.5 %: baseline reset
                    f1_0_[i] = nw;
                }
            }
        }
        // DAMAGE decision with a contributor guard (documented in the header comment and STRUCTURE.md): the state is defined by the
        // slaves that contributed at the last transition (dmg_set_). While a member of that set has had no usable drop for
        // DMG_ABSENT_HOLD superframes the decision is HELD and the runs restart: ACTIVE damage never ends while a BEGIN contributor is
        // missing (the slave that saw the damage may have rebooted; the others would see nothing); INACTIVE, a member absent for a
        // full P superframes leaves the set (the remaining slaves' own P-run is then genuine evidence). Shorter dropouts are pooled over
        // whoever is left, as in the evaluation of freq_tracking.
        for (int i = 0; i < 2; i++) dmg_absent_[i] = dok[i] ? 0 : (uint8_t)(dmg_absent_[i] < 255 ? dmg_absent_[i] + 1 : 255);
        bool hold = false;
        for (int i = 0; i < 2; i++) {
            if (!((dmg_set_ >> i) & 1) || dmg_absent_[i] < DMG_ABSENT_HOLD) continue;
            if (!dmg_.active && dmg_absent_[i] >= dmg_.p) dmg_set_ = (uint8_t)(dmg_set_ & ~(1 << i));
            else hold = true;
        }
        uint8_t cur = (uint8_t)((dok[0] ? 1 : 0) | (dok[1] ? 2 : 0));
        int dev = DMG_EV_NONE;
        if (hold) {
            dmg_.up_run = dmg_.down_run = 0;
            stats_.dmg_held++;
        } else {
            if (!dmg_.active && (cur & ~dmg_set_)) { dmg_set_ = (uint8_t)(dmg_set_ | cur); dmg_.up_run = 0; }   // a returning slave joins the set
            dev = dmg_update(&dmg_, 2, dok, ddrop);
            if (dev != DMG_EV_NONE) dmg_set_ = cur;   // the transition is defined by who contributed to it
        }
        if (dev == DMG_EV_BEGIN) stats_.dmg_begins++;
        if (dev == DMG_EV_END) stats_.dmg_ends++;
        stats_.baselines += (uint32_t)((note & 1) + ((note >> 1) & 1));
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
            for (int i = 0; i < 2; i++) { s.ae[i] = d.ae[i]; s.f1[i] = d.f1[i]; s.drop[i] = d.drop[i]; }
            s.dmg = (uint8_t)(dmg_.active ? 1 : 0);
            s.alive_mask = (uint8_t)((alive_[0] ? 1 : 0) | (alive_[1] ? 2 : 0));
            for (int i = 0; i < 2; i++) {
                s.age_sf[i] = last_rep_ok_[i] ? (int32_t)(d.sf - last_rep_sf_[i]) : -1;
                s.commission_n[i] = comm_cnt_[i];
            }
            sink_->on_sample(s);
            if (dev != DMG_EV_NONE) {
                DamageEvent de;
                de.t_us = now;
                de.sf = d.sf;
                de.begin = dev == DMG_EV_BEGIN;
                de.drop = dmg_.pooled;
                for (int i = 0; i < 2; i++) { de.f1[i] = dok[i] ? d.f1[i] : NAN; de.f1_0[i] = f1_0_[i]; }
                de.baseline_slave = 0;
                sink_->on_damage(de);
            }
            for (int i = 0; i < 2; i++) {
                if (!((note >> i) & 1)) continue;
                DamageEvent bn;
                bn.t_us = now;
                bn.sf = d.sf;
                bn.begin = false;
                bn.baseline_slave = (uint8_t)(i + 1);
                bn.drop = dmg_.pooled;
                for (int k = 0; k < 2; k++) { bn.f1[k] = dok[k] ? d.f1[k] : NAN; bn.f1_0[k] = f1_0_[k]; }
                sink_->on_damage(bn);
            }
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
        for (int i = 0; i < 2; i++) { e.ae[i] = d.ae[i]; e.f1[i] = d.f1[i]; e.drop[i] = d.drop[i]; }
        e.dmg = (uint8_t)(dmg_.active ? 1 : 0);
        sink_->on_event(e);
    }

    void decide(bool have, Decision *d) {
        bool avail[2] = {have && slot_have_[0], have && slot_have_[1]};
        bool e[2] = {false, false}, F[2] = {false, false};
        const bool rf = cfg_.rule == 'f';
        const float tb = rf ? cfg_.T_b : cfg_.T;   // the std-ratio threshold (rule f: T_b, T is the AE threshold)
        for (int i = 0; i < 2; i++) {
            if (!avail[i]) continue;
            d->r[i] = ratio(i, slot_f0_[i]);
            estate_[i] = smooth_update(hist_[i], d->r[i] >= tb ? 1 : 0, estate_[i]);
            e[i] = estate_[i] == 1;
            if (rf) {
                // AE flag: false while AE is not ready and below the noise gate (applied BEFORE smoothing); smoothed separately
                bool x = (slot_x_[i] & net::FLAG_AE_READY) && !(slot_f0_[i] < cfg_.gate) && slot_ae_[i] >= cfg_.T;
                astate_[i] = smooth_update(hist_a_[i], x ? 1 : 0, astate_[i]);
                e[i] = e[i] || astate_[i] == 1;
            }
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
            bool loc = (cfg_.rule == 'b' || rf) ? e[i] : cfg_.rule == 'c' ? (F[i] && e[i]) : F[i];
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
        case 'f':
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
    int8_t hist_[2][3], hist_a_[2][3];   // std-ratio flag history / AE flag history (rule f)
    int estate_[2], astate_[2];
    bool alive_[2];
    bool started_, armed_;
    uint32_t armed_sf_, next_sf_, slot_sf_, closed_, n_events_;
    bool slot_have_[2];
    uint8_t slot_raw_[2], slot_state_[2];
    float slot_f0_[2];
    uint8_t slot_x_[2];
    float slot_ae_[2], slot_f1_[2], slot_drop_[2];
    float f1_0_[2];
    dmg_t dmg_;
    uint8_t dmg_set_;           // slaves whose drops define the DAMAGE state (those that contributed at the last transition)
    uint8_t dmg_absent_[2];     // consecutive closed superframes without a usable f1 drop per slave (saturating)
    bool comm_ever_[2], comm_now_[2];
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
