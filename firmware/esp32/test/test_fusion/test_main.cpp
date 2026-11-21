#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <vector>
#include <string>
#include <unity.h>
#include "fusion.h"
#include "fusion_json.h"
#include "netsim.h"

using namespace fusion;

void setUp(void) {}
void tearDown(void) {}

struct Col : Sink {
    std::vector<Decision> dec;
    std::vector<Event> ev;
    void on_event(const Event &e) override { ev.push_back(e); }
    void on_decision(const Decision &d) override { dec.push_back(d); }
};

static Config cfg(char rule, float T, bool raw, uint8_t comm_n = 3, uint32_t st = 64, uint8_t k = 1) {
    Config c = {rule, T, raw, comm_n, st, k, 3.0f, CHOSEN_AE_GATE};
    return c;
}
// Rule f: T = the AE threshold, T_b = the std-ratio threshold of the b part.
static Config cfgf(float T_ae, float T_b = 3.0f, uint8_t k = 1, uint8_t comm_n = 3) {
    Config c = {'f', T_ae, false, comm_n, 64, k, T_b, CHOSEN_AE_GATE};
    return c;
}

// Drives a Fusion like the master would: both slaves report every superframe unless told otherwise.
struct Rig {
    Col col;
    Fusion f;
    uint32_t sf;
    explicit Rig(const Config &c) : f(&col, c), sf(0) {}
    uint64_t t() const { return (uint64_t)sf * 1400000ull + 1320000ull; }
    void one(int slave, uint8_t raw, uint8_t state, float f0) { f.on_report_raw((uint8_t)slave, sf, raw, state, f0, t()); }
    // report with the version-3 fields: AE ratio / ready, f1 estimate / drop (NAN = not valid; a finite drop = commissioned)
    void onex(int slave, float f0, float ae, bool ready, float f1 = NAN, float drop = NAN) {
        SlaveIn in;
        in.raw = in.state = 0;
        in.f0 = f0;
        in.xflags = (uint8_t)((ready ? net::FLAG_AE_READY : 0) | (f1 == f1 ? net::FLAG_F1_VALID : 0) | ((f1 == f1 && drop == drop) ? net::FLAG_F1_COMMISSIONED : 0));
        in.ae = ae;
        in.f1_est = f1;
        in.f1_drop = drop;
        f.on_report_ex((uint8_t)slave, sf, in, t());
    }
    // both slaves with AE ratios (ready) and optional f1 / drop; the second report closes the superframe
    const Decision &bothx(float f1v, float f2v, float ae1, float ae2, bool rdy1 = true, bool rdy2 = true) {
        onex(1, f1v, ae1, rdy1);
        onex(2, f2v, ae2, rdy2);
        sf++;
        return col.dec.back();
    }
    const Decision &bothd(float d1, float d2, float f1_0 = 5.0f) {   // both with f1 and drop: f1 = f1_0 * (1 - drop), AE ready and quiet
        onex(1, 100, 0.5f, true, f1_0 * (1.0f - d1), d1);
        onex(2, 100, 0.5f, true, f1_0 * (1.0f - d2), d2);
        sf++;
        return col.dec.back();
    }
    // both slaves report; the second report closes the superframe
    const Decision &both(float f1, float f2, uint8_t raw1 = 0, uint8_t st1 = 0, uint8_t raw2 = 0, uint8_t st2 = 0) {
        one(1, raw1, st1, f1);
        one(2, raw2, st2, f2);
        sf++;
        return col.dec.back();
    }
    void commission(float b1 = 100.0f, float b2 = 100.0f) {
        for (int i = 0; i < f.config().comm_n; i++) both(b1, b2);
    }
    // one slave only: the superframe is closed by the master finishing its polling
    const Decision &single(int slave, float f0, uint8_t raw = 0, uint8_t state = 0) {
        f.advance(sf, true, t() - 1000);
        one(slave, raw, state, f0);
        f.advance(sf, false, t() + 1000);
        sf++;
        return col.dec.back();
    }
    const Decision &none() {
        f.advance(sf, true, t() - 1000);
        f.advance(sf, false, t() + 1000);
        sf++;
        return col.dec.back();
    }
};

static void expect(const Decision &d, int cls, int slave = 0, int degraded = 0) {
    TEST_ASSERT_EQUAL_INT(cls, d.cls);
    TEST_ASSERT_EQUAL_INT(slave, d.slave);
    TEST_ASSERT_EQUAL_INT(degraded, d.degraded);
}

// ------------------------------------------------------------------ constants
// The final selected configuration as written into the parity vectors by tools/gen_fusion_vectors.py (version 4), which reads it
// from models/selected/validation/fusion_eval_r3_selected.json (rule f, T = AE threshold, T_b, k), the gate from fusion_eval_r3_dev.json
// and the commissioning windows from fusion_eval.json: an INDEPENDENT source for the constants in fusion.h.
#pragma pack(push, 1)
struct SelRec { uint8_t rule, use_raw, k, comm_n; float T; uint32_t status_every, max_catchup; float other_max; float T_b, gate; uint8_t gated, pad[3]; };
#pragma pack(pop)
static_assert(sizeof(SelRec) == 32, "selected-config block is 32 bytes");

static const uint32_t VECTORS_VERSION = 4;

static void read_vectors_header(FILE *fp, uint32_t *nseq, SelRec *sel) {
    char magic[4];
    uint32_t ver;
    TEST_ASSERT_EQUAL_INT(4, (int)fread(magic, 1, 4, fp));
    TEST_ASSERT_EQUAL_INT(0, memcmp(magic, "FUSV", 4));
    TEST_ASSERT_EQUAL_INT(1, (int)fread(&ver, 4, 1, fp));
    TEST_ASSERT_EQUAL_INT(1, (int)fread(nseq, 4, 1, fp));
    TEST_ASSERT_EQUAL_UINT32(VECTORS_VERSION, ver);
    TEST_ASSERT_EQUAL_INT(1, (int)fread(sel, sizeof(SelRec), 1, fp));
}

static void test_chosen_constants() {
    Fusion f;
    TEST_ASSERT_EQUAL_INT(CHOSEN_RULE, f.config().rule);
    TEST_ASSERT_EQUAL_FLOAT(CHOSEN_T, f.config().T);
    TEST_ASSERT_TRUE(f.config().use_raw == CHOSEN_USE_RAW);
    TEST_ASSERT_EQUAL_INT(COMMISSION_WINDOWS, f.config().comm_n);
    TEST_ASSERT_EQUAL_UINT32(STATUS_EVERY_SF, f.config().status_every);
    TEST_ASSERT_EQUAL_INT(CHOSEN_DEBOUNCE, f.config().debounce);
    TEST_ASSERT_TRUE(CHOSEN_DEBOUNCE >= 1 && CHOSEN_DEBOUNCE <= 3);
    // the constants must equal the selected configuration recorded in the vectors (from fusion_eval.json "final_selected")
    FILE *fp = fopen(FUSION_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    uint32_t nseq = 0;
    SelRec sel;
    read_vectors_header(fp, &nseq, &sel);
    fclose(fp);
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)sel.rule, (int)CHOSEN_RULE, "CHOSEN_RULE != selected rule");
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)'f', (int)CHOSEN_RULE, "the deployed rule is f (Phase C1 decision)");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&sel.T, &CHOSEN_T, 4) == 0, "CHOSEN_T != selected T");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&sel.T, &CHOSEN_T_AE, 4) == 0, "CHOSEN_T_AE != selected T (the AE threshold)");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&sel.T_b, &CHOSEN_T_B, 4) == 0, "CHOSEN_T_B != selected T_b");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&sel.gate, &CHOSEN_AE_GATE, 4) == 0, "CHOSEN_AE_GATE != noise_gate_counts");
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, (int)sel.gated, "the AE flag is gated");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&f.config().T_b, &sel.T_b, 4) == 0 && memcmp(&f.config().gate, &sel.gate, 4) == 0, "config T_b / gate");
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)sel.use_raw, (int)CHOSEN_USE_RAW, "CHOSEN_USE_RAW != selected use_raw");
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)sel.k, (int)CHOSEN_DEBOUNCE, "CHOSEN_DEBOUNCE != selected k");
    TEST_ASSERT_EQUAL_INT_MESSAGE((int)sel.comm_n, (int)COMMISSION_WINDOWS, "COMMISSION_WINDOWS != commissioning_windows");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(sel.status_every, STATUS_EVERY_SF, "STATUS_EVERY_SF != reference");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(sel.max_catchup, MAX_CATCHUP_SF, "MAX_CATCHUP_SF != reference");
    TEST_ASSERT_TRUE_MESSAGE(memcmp(&sel.other_max, &LOCAL_OTHER_MAX, 4) == 0, "LOCAL_OTHER_MAX != reference");
    TEST_ASSERT_EQUAL_FLOAT(1.0f, BASELINE_MIN);
}

// ------------------------------------------------------------------ commissioning
static void test_commissioning_median_and_precedence() {
    Config c = chosen_config();
    Rig r(c);
    // slave 1: 20 values, the sorted 10th and 11th are 11 and 12 -> baseline 11.5; slave 2 constant 50
    float v1[20] = {1000, 5, 9, 14, 20, 13, 7, 12, 11, 15, 10, 16, 17, 18, 19, 8, 6, 4, 3, 2};
    for (int i = 0; i < 20; i++) {
        TEST_ASSERT_FALSE(r.f.commissioned());
        const Decision &d = r.both(v1[i], 50.0f);
        expect(d, COMMISSIONING);
        TEST_ASSERT_EQUAL_FLOAT(-1.0f, d.r[0]);
    }
    TEST_ASSERT_TRUE(r.f.commissioned());
    TEST_ASSERT_EQUAL_FLOAT(11.5f, r.f.baseline(1));
    TEST_ASSERT_EQUAL_FLOAT(50.0f, r.f.baseline(2));
    // the 21st superframe is the first classified one; baseline 1 clamped to BASELINE_MIN not needed (11.5 > 1)
    const Decision &d = r.both(11.5f, 50.0f);
    expect(d, AMBIENT);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, d.r[0]);
}

static void test_commissioning_waits_for_both_slaves() {
    Rig r(cfg('b', 2.0f, false, 3));
    for (int i = 0; i < 3; i++) r.single(1, 100.0f);  // slave 1 complete, slave 2 never reported
    TEST_ASSERT_TRUE(r.f.baseline_ok(1));
    TEST_ASSERT_FALSE(r.f.baseline_ok(2));
    expect(r.single(1, 500.0f), COMMISSIONING);      // not DEGRADED: baselines first
    expect(r.none(), COMMISSIONING);                 // and not NO_DATA either
    r.single(2, 100.0f);
    r.single(2, 100.0f);
    expect(r.single(2, 100.0f), COMMISSIONING);      // completes the last baseline: still commissioning
    TEST_ASSERT_TRUE(r.f.commissioned());
    expect(r.both(100.0f, 100.0f), AMBIENT);
}

static void test_commissioning_ignores_extra_reports() {
    Rig r(cfg('b', 2.0f, false, 3));
    for (int i = 0; i < 3; i++) r.single(1, 100.0f);
    for (int i = 0; i < 5; i++) r.single(1, 9999.0f);  // slave 1 keeps reporting while slave 2 is missing
    TEST_ASSERT_EQUAL_FLOAT(100.0f, r.f.baseline(1));
}

static void test_baseline_floor() {
    Rig r(cfg('b', 2.0f, false, 3));
    r.commission(0.25f, 0.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.25f, r.f.baseline(1));
    const Decision &d = r.both(2.0f, 3.0f);
    TEST_ASSERT_EQUAL_FLOAT(2.0f, d.r[0]);   // divided by max(0.25, 1.0)
    TEST_ASSERT_EQUAL_FLOAT(3.0f, d.r[1]);
}

// ------------------------------------------------------------------ rules
static void test_rule_a() {
    Rig r(cfg('a', 2.0f, false));
    r.commission();
    expect(r.both(100, 100, 0, 0, 0, 0), AMBIENT);
    expect(r.both(100, 100, 2, 2, 0, 0), LOCAL, 1);
    expect(r.both(100, 100, 0, 0, 2, 2), LOCAL, 2);
    expect(r.both(100, 100, 2, 2, 2, 2), BUILDING);
    expect(r.both(100, 100, 2, 0, 2, 0), AMBIENT);        // smoothed variant: raw faulty alone does not count
    expect(r.both(100, 100, 0, 1, 0, 1), AMBIENT);        // abnormal is not faulty
    Rig q(cfg('a', 2.0f, true));
    q.commission();
    expect(q.both(100, 100, 2, 0, 0, 0), LOCAL, 1);       // raw variant: raw faulty counts
    expect(q.both(100, 100, 2, 0, 2, 0), BUILDING);
    expect(q.both(100, 100, 0, 2, 2, 0), BUILDING);       // state on one, raw on the other
    // the ratio plays no role in rule a
    expect(q.both(900, 900, 0, 0, 0, 0), AMBIENT);
}

static void test_rule_b() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    expect(r.both(300, 100), AMBIENT);          // one elevated report is not enough (2-of-3)
    expect(r.both(300, 100), LOCAL, 1);
    expect(r.both(300, 300), LOCAL, 1);         // slave 2: first elevated report, e2 not yet
}

static void test_rule_b_building_needs_two_of_three() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    expect(r.both(300, 300), AMBIENT);          // e needs 2 of the last 3 on each slave
    expect(r.both(300, 300), BUILDING);
    expect(r.both(100, 100), BUILDING);         // history 1,1,0 -> e holds (2 of 3)
    expect(r.both(100, 100), AMBIENT);          // 1,0,0 -> e falls back to 0
    expect(r.both(100, 300), AMBIENT);
    expect(r.both(100, 300), LOCAL, 2);
    expect(r.both(100, 100, 2, 2, 2, 2), LOCAL, 2);  // the tree is ignored by rule b (e2 holds)
}

static void test_rule_c() {
    Rig r(cfg('c', 2.0f, false));
    r.commission();
    expect(r.both(300, 100, 0, 2, 0, 0), AMBIENT);      // e1 not yet
    expect(r.both(300, 100, 0, 2, 0, 0), LOCAL, 1);     // F1 & e1 & r2 < 1.5
    expect(r.both(300, 149.0f, 0, 2, 0, 0), LOCAL, 1);
    expect(r.both(300, 150.0f, 0, 2, 0, 0), AMBIENT);   // r2 == 1.5 exactly is NOT < 1.5
    expect(r.both(300, nextafterf(150.0f, 0.0f), 0, 2, 0, 0), LOCAL, 1);
    // no tree fault: elevated alone does not make a LOCAL
    Rig q(cfg('c', 2.0f, false));
    q.commission();
    q.both(300, 100);
    expect(q.both(300, 100), AMBIENT);
    // both elevated: BUILDING whatever the tree says
    q.both(300, 300);
    expect(q.both(300, 300, 0, 2, 0, 2), BUILDING);
    // slave 2 local
    Rig s(cfg('c', 2.0f, true));
    s.commission();
    s.both(100, 300, 0, 0, 2, 0);
    expect(s.both(100, 300, 0, 0, 2, 0), LOCAL, 2);     // raw variant
    Rig u(cfg('c', 2.0f, false));
    u.commission();
    u.both(100, 300, 0, 0, 2, 0);
    expect(u.both(100, 300, 0, 0, 2, 0), AMBIENT);      // smoothed variant: state still 0
}

static void test_rule_d() {
    Rig r(cfg('d', 2.0f, false));
    r.commission();
    expect(r.both(100, 100, 0, 2, 0, 0), LOCAL, 1);     // exactly one F, no e needed
    expect(r.both(100, 100, 0, 0, 0, 2), LOCAL, 2);
    expect(r.both(100, 100, 0, 2, 0, 2), LOCAL, 1);     // both F, r tie -> slave 1
    expect(r.both(100, 110, 0, 2, 0, 2), LOCAL, 2);     // both F, higher r -> slave 2
    expect(r.both(130, 110, 0, 2, 0, 2), LOCAL, 1);
    expect(r.both(100, 100, 0, 0, 0, 0), AMBIENT);
    // both e dominates
    Rig q(cfg('d', 2.0f, false));
    q.commission();
    q.both(300, 300, 0, 2, 0, 0);
    expect(q.both(300, 300, 0, 2, 0, 0), BUILDING);
    // e on one slave without F: AMBIENT
    Rig s(cfg('d', 2.0f, false));
    s.commission();
    s.both(300, 100);
    expect(s.both(300, 100), AMBIENT);
}

// ------------------------------------------------------------------ threshold edges
static void edge_case(float T) {
    float base = 128.0f;
    float edge = T * base;
    {  // exactly T*base twice -> elevated (>=)
        Rig r(cfg('b', T, false));
        r.commission(base, base);
        r.both(edge, base);
        expect(r.both(edge, base), LOCAL, 1);
    }
    {  // one ulp below, twice -> not elevated
        Rig r(cfg('b', T, false));
        r.commission(base, base);
        float below = nextafterf(edge, 0.0f);
        r.both(below, base);
        expect(r.both(below, base), AMBIENT);
    }
    {  // one ulp above
        Rig r(cfg('b', T, false));
        r.commission(base, base);
        float above = nextafterf(edge, 1e9f);
        r.both(above, base);
        expect(r.both(above, base), LOCAL, 1);
    }
}
static void test_threshold_edges() {
    edge_case(1.5f);
    edge_case(2.0f);
    edge_case(3.0f);
}

// ------------------------------------------------------------------ DEGRADED / NO_DATA
static void test_degraded_per_rule() {
    // alive slave 2, missing slave 1
    const char rules[4] = {'a', 'b', 'c', 'd'};
    for (int k = 0; k < 4; k++) {
        Rig r(cfg(rules[k], 2.0f, true));
        r.commission();
        expect(r.single(2, 100.0f, 0, 0), AMBIENT, 0, 2);
        const Decision &d1 = r.single(2, 300.0f, 2, 2);  // e2 still needs a second elevated report (rules b, c)
        if (rules[k] == 'a' || rules[k] == 'd') expect(d1, LOCAL, 2, 2);
        else expect(d1, AMBIENT, 0, 2);
        expect(r.single(2, 300.0f, 2, 2), LOCAL, 2, 2);  // F and e
        expect(r.single(2, 300.0f, 0, 0), rules[k] == 'b' ? LOCAL : AMBIENT, rules[k] == 'b' ? 2 : 0, 2);
        // BUILDING is never output with one slave
        for (int i = 0; i < 4; i++) TEST_ASSERT_TRUE(r.single(2, 900.0f, 2, 2).cls != BUILDING);
    }
    Rig q(cfg('c', 2.0f, true));
    q.commission();
    const Decision &d = q.single(1, 100.0f);
    expect(d, AMBIENT, 0, 1);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, d.r[0]);
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, d.r[1]);
}

static void test_no_data_and_dead() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    expect(r.both(100, 100), AMBIENT);
    const Decision &n = r.none();
    expect(n, NO_DATA);
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, n.r[0]);
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, n.r[1]);
    r.f.on_status(1, false, r.t());
    r.f.on_status(2, false, r.t());
    TEST_ASSERT_FALSE(r.f.slave_alive(1));
    expect(r.none(), NO_DATA);
    // a revived slave starts with a clean e history
    Rig q(cfg('b', 2.0f, false));
    q.commission();
    q.both(300, 100);            // e1 history 1
    q.f.on_status(1, false, q.t());
    q.f.on_status(1, true, q.t());   // ALIVE after DEAD clears the history
    q.both(300, 100);
    expect(q.col.dec.back(), AMBIENT);  // would be LOCAL(1) with the old history
    // while a slave is missing, its history is held
    Rig h(cfg('b', 2.0f, false));
    h.commission();
    h.both(300, 100);
    h.single(2, 100.0f);          // slave 1 missing: its history is held, not advanced
    expect(h.both(300, 100), LOCAL, 1);
}

static void test_degraded_event_and_recovery() {
    Rig r(cfg('a', 2.0f, false));
    r.commission();
    r.both(100, 100);
    r.single(1, 100.0f, 0, 2);   // LOCAL(1) while degraded
    r.both(100, 100);
    Col &c = r.col;
    // events: first decision (COMMISSIONING), AMBIENT, DEGRADED local, back to AMBIENT
    TEST_ASSERT_TRUE(c.ev.size() >= 4);
    TEST_ASSERT_EQUAL_INT(COMMISSIONING, c.ev[0].cls);
    TEST_ASSERT_EQUAL_INT(AMBIENT, c.ev[1].cls);
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, c.ev[1].kind);
    TEST_ASSERT_EQUAL_INT(LOCAL, c.ev[2].cls);
    TEST_ASSERT_EQUAL_INT(EV_BEGIN, c.ev[2].kind);
    TEST_ASSERT_EQUAL_INT(1, c.ev[2].degraded);
    TEST_ASSERT_EQUAL_INT(AMBIENT, c.ev[3].cls);
}

// ------------------------------------------------------------------ events
static void test_events_only_on_change() {
    Rig r(cfg('a', 2.0f, false, 3, 7));
    r.commission();
    TEST_ASSERT_EQUAL_UINT32(1, (uint32_t)r.col.ev.size());   // COMMISSIONING announced once, not three times
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev[0].kind);
    r.both(100, 100);
    r.both(100, 100);
    TEST_ASSERT_EQUAL_UINT32(2, (uint32_t)r.col.ev.size());   // AMBIENT once
    r.both(100, 100, 2, 2, 0, 0);
    r.both(100, 100, 2, 2, 0, 0);
    r.both(100, 100, 2, 2, 0, 0);
    // statuses are interleaved every 7 superframes
    uint32_t begins = 0, changes = 0, statuses = 0;
    for (size_t i = 0; i < r.col.ev.size(); i++) {
        begins += r.col.ev[i].kind == EV_BEGIN;
        changes += r.col.ev[i].kind == EV_CHANGE;
        statuses += r.col.ev[i].kind == EV_STATUS;
    }
    TEST_ASSERT_EQUAL_UINT32(1, begins);
    TEST_ASSERT_EQUAL_UINT32(2, changes);
    TEST_ASSERT_EQUAL_UINT32(r.f.closed() / 7, statuses);
    TEST_ASSERT_EQUAL_UINT32(3, r.f.events_emitted());
    r.both(100, 100, 2, 2, 2, 2);                              // LOCAL(1) -> BUILDING is a change, not a new begin
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev.back().kind);
    TEST_ASSERT_EQUAL_INT(BUILDING, r.col.ev.back().cls);
    r.both(100, 100, 2, 2, 0, 0);                              // BUILDING -> LOCAL(1): change
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev.back().kind);
    r.both(100, 100, 0, 0, 2, 2);                              // LOCAL(1) -> LOCAL(2): change
    TEST_ASSERT_EQUAL_INT(2, r.col.ev.back().slave);
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev.back().kind);
    // event fields
    const Event &e = r.col.ev.back();
    TEST_ASSERT_EQUAL_UINT64(r.t() - 1400000ull, e.t_us);
    TEST_ASSERT_EQUAL_UINT8(3, e.alive_mask);
    TEST_ASSERT_EQUAL_FLOAT(100.0f, e.base[0]);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, e.r[0]);
}

// ------------------------------------------------------------------ debounce (R2b)
static const Decision &lone(Rig &r) { return r.both(100, 100, 2, 2, 0, 0); }   // rule a, smoothed: LOCAL(1) decision
static const Decision &quiet(Rig &r) { return r.both(100, 100, 0, 0, 0, 0); }

static void test_debounce_k2() {
    Rig r(cfg('a', 2.0f, false, 3, 64, 2));
    r.commission();
    expect(quiet(r), AMBIENT);
    size_t ev0 = r.col.ev.size();
    expect(lone(r), AMBIENT);                          // first superframe of the candidate: still AMBIENT
    TEST_ASSERT_EQUAL_UINT32((uint32_t)ev0, (uint32_t)r.col.ev.size());   // no begin yet
    expect(lone(r), LOCAL, 1);                         // second: output, begin emitted now
    TEST_ASSERT_EQUAL_UINT32((uint32_t)ev0 + 1, (uint32_t)r.col.ev.size());
    TEST_ASSERT_EQUAL_INT(EV_BEGIN, r.col.ev.back().kind);
    expect(lone(r), LOCAL, 1);                         // stays, no new event
    TEST_ASSERT_EQUAL_UINT32((uint32_t)ev0 + 1, (uint32_t)r.col.ev.size());
    expect(quiet(r), AMBIENT);                         // return at once
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev.back().kind);
}

static void test_debounce_k3_interrupted_run() {
    Rig r(cfg('a', 2.0f, false, 3, 64, 3));
    r.commission();
    quiet(r);
    expect(lone(r), AMBIENT);
    expect(lone(r), AMBIENT);
    expect(quiet(r), AMBIENT);                         // run broken: count restarts
    expect(lone(r), AMBIENT);
    expect(lone(r), AMBIENT);
    TEST_ASSERT_EQUAL_UINT32(0, r.f.stats().by_cls[LOCAL]);
    expect(lone(r), LOCAL, 1);                         // third consecutive
    // different candidate key resets the count: LOCAL(1) x2 then LOCAL(2) x2 never reaches 3
    Rig q(cfg('a', 2.0f, false, 3, 64, 3));
    q.commission();
    q.both(100, 100, 2, 2, 0, 0);
    q.both(100, 100, 2, 2, 0, 0);
    expect(q.both(100, 100, 0, 0, 2, 2), AMBIENT);
    expect(q.both(100, 100, 0, 0, 2, 2), AMBIENT);
    TEST_ASSERT_EQUAL_UINT32(0, q.f.stats().by_cls[LOCAL]);
    expect(q.both(100, 100, 0, 0, 2, 2), LOCAL, 2);
}

static void test_debounce_event_to_event_keeps_previous() {
    Rig r(cfg('a', 2.0f, false, 3, 64, 2));
    r.commission();
    lone(r);
    expect(lone(r), LOCAL, 1);
    expect(r.both(100, 100, 2, 2, 2, 2), LOCAL, 1);    // BUILDING pending: previous event class kept, no flicker through AMBIENT
    TEST_ASSERT_EQUAL_INT(EV_BEGIN, r.col.ev.back().kind);
    expect(r.both(100, 100, 2, 2, 2, 2), BUILDING);
    TEST_ASSERT_EQUAL_INT(EV_CHANGE, r.col.ev.back().kind);   // LOCAL -> BUILDING is a change, not a begin
    // one BUILDING superframe then back to LOCAL(1): the pending BUILDING is dropped
    Rig q(cfg('a', 2.0f, false, 3, 64, 2));
    q.commission();
    q.both(100, 100, 2, 2, 0, 0);
    q.both(100, 100, 2, 2, 0, 0);
    q.both(100, 100, 2, 2, 2, 2);
    expect(q.both(100, 100, 2, 2, 0, 0), LOCAL, 1);
    expect(q.both(100, 100, 2, 2, 2, 2), LOCAL, 1);    // count restarted
}

static void test_debounce_degraded_and_k1() {
    Rig r(cfg('a', 2.0f, false, 3, 64, 2));
    r.commission();
    r.both(100, 100);
    expect(r.single(1, 100.0f, 0, 2), AMBIENT, 0, 1);  // LOCAL(1) pending while degraded: AMBIENT, degraded kept
    expect(r.single(1, 100.0f, 0, 2), LOCAL, 1, 1);
    // k = 1 is no debounce
    Rig q(cfg('a', 2.0f, false, 3, 64, 1));
    q.commission();
    expect(lone(q), LOCAL, 1);
    // pending state is cleared by a stream restart
    Rig s(cfg('a', 2.0f, false, 3, 64, 2));
    s.commission();
    lone(s);
    s.sf = 0;
    expect(lone(s), AMBIENT);                          // restart cleared the count: needs two again
    expect(lone(s), LOCAL, 1);
}

static void test_status_period() {
    Rig r(cfg('b', 2.0f, false, 3, 64));
    r.commission();
    for (int i = 0; i < 200; i++) r.both(100, 100);
    uint32_t st = 0;
    for (size_t i = 0; i < r.col.ev.size(); i++)
        if (r.col.ev[i].kind == EV_STATUS) {
            TEST_ASSERT_EQUAL_UINT32(0, r.col.ev[i].closed % 64);
            st++;
        }
    TEST_ASSERT_EQUAL_UINT32(203 / 64, st);
}

// ------------------------------------------------------------------ stream handling
static void test_restart_keeps_baselines() {
    Rig r(cfg('b', 2.0f, false));
    r.commission(100.0f, 100.0f);
    r.both(100, 100);
    r.sf = 0;                      // master restarted: superframe numbering from 0
    expect(r.both(100, 100), AMBIENT);
    TEST_ASSERT_EQUAL_UINT32(1, r.f.stats().restarts);
    TEST_ASSERT_EQUAL_FLOAT(100.0f, r.f.baseline(1));
    // a lone report is closed by the restart
    Rig q(cfg('b', 2.0f, false));
    q.commission();
    q.one(1, 0, 0, 100.0f);        // slave 1 only, superframe not yet closed
    q.sf = 0;
    q.both(100, 100);
    TEST_ASSERT_EQUAL_UINT32(1, q.f.stats().restarts);
    TEST_ASSERT_EQUAL_UINT32(1, q.f.stats().degraded_sf);   // the lone report was closed (DEGRADED) by the restart
}

static void test_gap_is_bounded() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    r.both(100, 100);
    uint32_t before = (uint32_t)r.col.dec.size();
    r.sf += 1000;                  // 1000 silent superframes
    r.both(100, 100);
    uint32_t added = (uint32_t)r.col.dec.size() - before;
    TEST_ASSERT_EQUAL_UINT32(MAX_CATCHUP_SF + 1, added);   // at most MAX_CATCHUP_SF NO_DATA, then the real one
    TEST_ASSERT_EQUAL_UINT32(1000 - MAX_CATCHUP_SF, r.f.stats().skipped_sf);
    TEST_ASSERT_EQUAL_INT(NO_DATA, r.col.dec[before].cls);
}

static void test_missing_superframes_without_advance() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    r.both(100, 100);
    r.sf += 2;                     // two superframes with no report at all and no advance() calls
    r.both(100, 100);
    size_t n = r.col.dec.size();
    expect(r.col.dec[n - 2], NO_DATA);
    expect(r.col.dec[n - 3], NO_DATA);
    expect(r.col.dec[n - 1], AMBIENT);
}

static void test_flush_and_advance() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    r.one(1, 0, 0, 100.0f);
    size_t n = r.col.dec.size();
    r.f.advance(r.sf, true, 1);           // polling in progress: the superframe stays open
    TEST_ASSERT_EQUAL_UINT32((uint32_t)n, (uint32_t)r.col.dec.size());
    r.f.advance(r.sf, false, 2);          // polling done: closes it (DEGRADED)
    TEST_ASSERT_EQUAL_UINT32((uint32_t)n + 1, (uint32_t)r.col.dec.size());
    expect(r.col.dec.back(), AMBIENT, 0, 1);
    r.f.advance(r.sf, false, 3);          // idempotent
    TEST_ASSERT_EQUAL_UINT32((uint32_t)n + 1, (uint32_t)r.col.dec.size());
    r.sf++;
    r.one(2, 0, 0, 100.0f);
    r.f.flush(9);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)n + 2, (uint32_t)r.col.dec.size());
    r.f.flush(10);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)n + 2, (uint32_t)r.col.dec.size());
    // advance before the first beacon (not armed) does nothing
    Fusion f2;
    f2.advance(0, false, 1);
    TEST_ASSERT_EQUAL_UINT32(0, f2.closed());
}

static void test_rejected_reports() {
    Rig r(cfg('b', 2.0f, false));
    r.commission();
    r.f.on_report_raw(1, r.sf, 0, 0, NAN, 1);
    r.f.on_report_raw(1, r.sf, 0, 0, INFINITY, 1);
    r.f.on_report_raw(1, r.sf, 0, 0, -1.0f, 1);
    r.f.on_report_raw(3, r.sf, 0, 0, 1.0f, 1);
    r.f.on_report_raw(0, r.sf, 0, 0, 1.0f, 1);
    TEST_ASSERT_EQUAL_UINT32(5, r.f.stats().rejected_reports);
    expect(r.both(100, 100), AMBIENT);
}

// ------------------------------------------------------------------ Master/netsim integration
static void test_listener_forwards() {
    struct Rec : net::MasterListener {
        int reports = 0, statuses = 0;
        void on_report(uint8_t, const net::ReportPkt &, uint64_t) override { reports++; }
        void on_status(uint8_t, bool, uint64_t) override { statuses++; }
    } rec;
    Col col;
    Fusion f(&col, cfg('b', 2.0f, false, 3));
    FusionListener tap(&f, &rec);
    net::ReportPkt p;
    memset(&p, 0, sizeof p);
    p.h.src = 1;
    p.sf = 0;
    p.f[0] = 100.0f;
    tap.on_report(1, p, 10);
    p.h.src = 2;
    tap.on_report(2, p, 11);
    tap.on_status(1, false, 12);
    TEST_ASSERT_EQUAL_INT(2, rec.reports);
    TEST_ASSERT_EQUAL_INT(1, rec.statuses);
    TEST_ASSERT_EQUAL_UINT32(1, f.closed());
    TEST_ASSERT_FALSE(f.slave_alive(1));
    FusionListener alone(&f);          // no next listener
    alone.on_status(1, true, 13);
    TEST_ASSERT_TRUE(f.slave_alive(1));
}

static void test_netsim_integration() {
    netsim::SimConfig c;
    c.superframes = 40;
    c.quiet = true;
    netsim::Sim sim(c);
    Col col;
    Fusion f(&col, cfg('b', 2.0f, false, 20));
    FusionListener tap(&f, &sim);
    sim.master().set_listener(&tap);
    sim.master_hook = [&](const net::Master &m, uint64_t now) { tap.service(m, now); };
    sim.run();
    f.flush(40ull * net::SUPERFRAME_US);
    TEST_ASSERT_TRUE(f.commissioned());
    TEST_ASSERT_TRUE(f.closed() >= 39);
    TEST_ASSERT_EQUAL_UINT32(f.closed(), (uint32_t)col.dec.size());
    for (size_t i = 1; i < col.dec.size(); i++) TEST_ASSERT_EQUAL_UINT32(col.dec[i - 1].sf + 1, col.dec[i].sf);
    TEST_ASSERT_EQUAL_UINT32(sim.res.reports_delivered[1], sim.res.reports_delivered[2]);  // the recorder still saw everything
    // kill slave 2 mid-run: DEAD arrives as status and the rest of the superframes are DEGRADED(1)
    netsim::SimConfig c2 = c;
    c2.superframes = 60;
    c2.kills.push_back({2, 30});
    netsim::Sim sim2(c2);
    Col col2;
    Fusion f2(&col2, cfg('b', 2.0f, false, 20));
    FusionListener tap2(&f2, &sim2);
    sim2.master().set_listener(&tap2);
    sim2.master_hook = [&](const net::Master &m, uint64_t now) { tap2.service(m, now); };
    sim2.run();
    f2.flush(60ull * net::SUPERFRAME_US);
    TEST_ASSERT_FALSE(f2.slave_alive(2));
    TEST_ASSERT_TRUE(f2.stats().degraded_sf >= 25);
    TEST_ASSERT_EQUAL_INT(1, col2.dec.back().degraded);
}

static void test_trace_source() {
    const char *path = "fusion_trace_test.bin";
    FILE *fp = fopen(path, "wb");
    TEST_ASSERT_NOT_NULL(fp);
    uint32_t h[4] = {0, 1, 2, 3};
    memcpy(h, "RTRC", 4);
    fwrite(h, 4, 4, fp);
    for (int s = 0; s < 2; s++)
        for (int w = 0; w < 3; w++) {
            int16_t buf[256];
            for (int i = 0; i < 256; i++) buf[i] = (int16_t)(s * 1000 + w * 100 + (i < 128 ? 1 : 2));
            fwrite(buf, 2, 256, fp);
        }
    fclose(fp);
    netsim::TraceSource ts;
    std::string err;
    TEST_ASSERT_TRUE(ts.load(path, &err));
    TEST_ASSERT_EQUAL_UINT32(3, ts.windows());
    int16_t az[128], gx[128];
    ts.get(2, 1, az, gx);
    TEST_ASSERT_EQUAL_INT16(1101, az[0]);
    TEST_ASSERT_EQUAL_INT16(1102, gx[127]);
    ts.get(1, 99, az, gx);   // past the end: last window
    TEST_ASSERT_EQUAL_INT16(201, az[5]);
    // truncated file and bad magic are rejected
    fp = fopen(path, "r+b");
    fseek(fp, 100, SEEK_SET);
    fputc(0, fp);
    fclose(fp);
    netsim::TraceSource t2;
    TEST_ASSERT_TRUE(t2.load(path, &err));   // same size, contents differ: fine
    fp = fopen(path, "wb");
    fwrite(h, 4, 4, fp);
    fwrite(h, 4, 4, fp);
    fclose(fp);
    TEST_ASSERT_FALSE(ts.load(path, &err));
    TEST_ASSERT_FALSE(ts.load("no_such_file.bin", &err));
    remove(path);
}

// ------------------------------------------------------------------ parity vectors
#pragma pack(push, 1)
struct OpRec { uint8_t type, slave, fb; uint32_t sf; float f0; float ae; uint32_t t_ms; };   // fb: raw | state << 2 | ae_ready << 4
struct SeqHdr { uint8_t rule, use_raw, comm_n, k; uint32_t status_every; float T, T_b, gate; uint32_t n_ops, n_dec, n_rec; };
struct DecRec { uint32_t sf; uint8_t cls, slave, deg, pad; float r1, r2; };
struct EvRec { uint8_t kind, cls, slave, deg; uint32_t sf, t_ms; float r1, r2, b1, b2; uint8_t alive, p0, p1, p2; uint32_t closed, nev; };
#pragma pack(pop)

static bool same_float(float a, float b) { return memcmp(&a, &b, 4) == 0; }

// ------------------------------------------------------------------ Sample callback (schema 2)
struct SampCol : Col {
    std::vector<Sample> smp;
    void on_sample(const Sample &x) override { smp.push_back(x); }
};

static uint64_t st_t(uint32_t sf) { return (uint64_t)sf * 1400000ull + 1320000ull; }

// A fixed script: 3 commissioning superframes, both, slave 1 only, nothing, slave 2 only (slave 2 DEAD meanwhile), both.
static void run_script(Fusion &f) {
    for (uint32_t sf = 0; sf < 4; sf++) {
        f.on_report_raw(1, sf, 0, 0, 100.0f, st_t(sf));
        f.on_report_raw(2, sf, 0, 0, 100.0f, st_t(sf));
    }
    f.advance(4, true, st_t(4) - 1000);
    f.on_report_raw(1, 4, 0, 0, 100.0f, st_t(4));
    f.advance(4, false, st_t(4) + 1000);
    f.on_status(2, false, st_t(5));
    f.advance(5, true, st_t(5) - 1000);
    f.advance(5, false, st_t(5) + 1000);
    f.advance(6, true, st_t(6) - 1000);
    f.on_report_raw(2, 6, 0, 2, 900.0f, st_t(6));
    f.advance(6, false, st_t(6) + 1000);
    f.on_status(2, true, st_t(7) - 10);
    f.on_report_raw(1, 7, 0, 0, 100.0f, st_t(7));
    f.on_report_raw(2, 7, 0, 0, 100.0f, st_t(7));
    f.flush(st_t(8));
}

static void test_sample_callback_read_only_and_correct() {
    Col plain;
    SampCol withs;
    Fusion fa(&plain, cfg('b', 2.0f, false, 3));
    Fusion fb(&withs, cfg('b', 2.0f, false, 3));
    run_script(fa);
    run_script(fb);
    // the extra callback changes nothing: same decisions, same events
    TEST_ASSERT_EQUAL_UINT32(plain.dec.size(), withs.dec.size());
    for (size_t i = 0; i < plain.dec.size(); i++) {
        TEST_ASSERT_EQUAL_UINT32(plain.dec[i].sf, withs.dec[i].sf);
        TEST_ASSERT_EQUAL_INT(plain.dec[i].cls, withs.dec[i].cls);
        TEST_ASSERT_EQUAL_INT(plain.dec[i].slave, withs.dec[i].slave);
        TEST_ASSERT_EQUAL_INT(plain.dec[i].degraded, withs.dec[i].degraded);
        TEST_ASSERT_EQUAL_FLOAT(plain.dec[i].r[0], withs.dec[i].r[0]);
        TEST_ASSERT_EQUAL_FLOAT(plain.dec[i].r[1], withs.dec[i].r[1]);
    }
    TEST_ASSERT_EQUAL_UINT32(plain.ev.size(), withs.ev.size());
    for (size_t i = 0; i < plain.ev.size(); i++) {
        TEST_ASSERT_EQUAL_INT(plain.ev[i].kind, withs.ev[i].kind);
        TEST_ASSERT_EQUAL_INT(plain.ev[i].cls, withs.ev[i].cls);
        TEST_ASSERT_EQUAL_UINT32(plain.ev[i].sf, withs.ev[i].sf);
        TEST_ASSERT_TRUE(plain.ev[i].t_us == withs.ev[i].t_us);
    }
    // one sample per closed superframe, in order, consistent with the decision
    TEST_ASSERT_EQUAL_UINT32(withs.dec.size(), withs.smp.size());
    TEST_ASSERT_EQUAL_UINT32(fb.closed(), withs.smp.size());
    TEST_ASSERT_EQUAL_UINT32(8, withs.smp.size());
    for (size_t i = 0; i < withs.smp.size(); i++) {
        const Sample &x = withs.smp[i];
        TEST_ASSERT_EQUAL_UINT32(withs.dec[i].sf, x.sf);
        TEST_ASSERT_EQUAL_UINT32(i + 1, x.closed);
        TEST_ASSERT_EQUAL_INT(withs.dec[i].cls, x.cls);
        TEST_ASSERT_EQUAL_INT(withs.dec[i].slave, x.slave);
        TEST_ASSERT_EQUAL_INT(withs.dec[i].degraded, x.degraded);
        TEST_ASSERT_EQUAL_FLOAT(withs.dec[i].r[0], x.r[0]);
        TEST_ASSERT_EQUAL_FLOAT(withs.dec[i].r[1], x.r[1]);
    }
    // commissioning progress (comm_n = 3): 1,2,3 then it stays at 3 once the baseline exists
    const uint8_t cn[8] = {1, 2, 3, 3, 3, 3, 3, 3};
    for (int i = 0; i < 8; i++) {
        TEST_ASSERT_EQUAL_INT(cn[i], withs.smp[i].commission_n[0]);
        TEST_ASSERT_EQUAL_INT(cn[i], withs.smp[i].commission_n[1]);
    }
    TEST_ASSERT_EQUAL_INT(COMMISSIONING, withs.smp[2].cls);   // the superframe that completes the baseline
    TEST_ASSERT_EQUAL_INT(AMBIENT, withs.smp[3].cls);
    // report age: sf3 both 0/0; sf4 only slave 1 -> 0/1; sf5 nobody -> 1/2; sf6 only slave 2 -> 2/0; sf7 both -> 0/0
    const int32_t a1[5] = {0, 0, 1, 2, 0}, a2[5] = {0, 1, 2, 0, 0};
    for (int k = 0; k < 5; k++) {
        TEST_ASSERT_EQUAL_INT32(a1[k], withs.smp[3 + k].age_sf[0]);
        TEST_ASSERT_EQUAL_INT32(a2[k], withs.smp[3 + k].age_sf[1]);
    }
    // alive bits: slave 2 DEAD for sf5 and sf6 (status arrived after sf4 closed, ALIVE before sf7 closed)
    TEST_ASSERT_EQUAL_INT(3, withs.smp[4].alive_mask);
    TEST_ASSERT_EQUAL_INT(1, withs.smp[5].alive_mask);
    TEST_ASSERT_EQUAL_INT(1, withs.smp[6].alive_mask);
    TEST_ASSERT_EQUAL_INT(3, withs.smp[7].alive_mask);
    TEST_ASSERT_TRUE(withs.smp[7].t_us == st_t(7));   // closed by the second report of sf 7
    // events carry the same commissioning progress
    TEST_ASSERT_EQUAL_INT(1, withs.ev[0].commission_n[0]);
    TEST_ASSERT_EQUAL_INT(3, withs.ev.back().commission_n[1]);
}

static void test_sample_age_after_restart_and_never() {
    SampCol c;
    Fusion f(&c, cfg('b', 2.0f, false, 3));
    f.on_report_raw(1, 0, 0, 0, 100.0f, 10);    // only slave 1 ever reports: slave 2 age stays unknown
    f.advance(0, true, 5);
    f.advance(0, false, 20);
    TEST_ASSERT_EQUAL_UINT32(1, c.smp.size());
    TEST_ASSERT_EQUAL_INT32(0, c.smp[0].age_sf[0]);
    TEST_ASSERT_EQUAL_INT32(-1, c.smp[0].age_sf[1]);
    f.advance(1, true, 30);
    f.advance(1, false, 40);
    TEST_ASSERT_EQUAL_INT32(1, c.smp.back().age_sf[0]);
    TEST_ASSERT_EQUAL_INT32(-1, c.smp.back().age_sf[1]);
    // master restart (sf goes back to 0): ages are unknown again until each slave reports
    f.on_report_raw(2, 0, 0, 0, 100.0f, 50);
    f.flush(60);
    TEST_ASSERT_EQUAL_UINT32(1, f.stats().restarts);
    TEST_ASSERT_EQUAL_INT32(-1, c.smp.back().age_sf[0]);
    TEST_ASSERT_EQUAL_INT32(0, c.smp.back().age_sf[1]);
}

// ------------------------------------------------------------------ JSON-line formatter
namespace J = fusion::json;

static std::string fmt_num(double v) {
    char b[64];
    J::Writer w(b, sizeof b);
    w.num(v);
    w.finish();
    return std::string(b, strlen(b) - 1);   // without the newline
}

static void test_json_number_format() {
    TEST_ASSERT_EQUAL_STRING("0", fmt_num(0.0).c_str());
    TEST_ASSERT_EQUAL_STRING("0", fmt_num(-0.0).c_str());
    TEST_ASSERT_EQUAL_STRING("3", fmt_num(3.0).c_str());
    TEST_ASSERT_EQUAL_STRING("0.5", fmt_num(0.5).c_str());
    TEST_ASSERT_EQUAL_STRING("1.4", fmt_num(1.4).c_str());
    TEST_ASSERT_EQUAL_STRING("0.1", fmt_num((double)0.1f).c_str());        // 0.100000001490116... in float32
    TEST_ASSERT_EQUAL_STRING("75.2", fmt_num((double)75.2f).c_str());
    TEST_ASSERT_EQUAL_STRING("102.6", fmt_num((double)102.6f).c_str());
    TEST_ASSERT_EQUAL_STRING("123456.8", fmt_num((double)123456.789f).c_str());      // 7 significant digits
    TEST_ASSERT_EQUAL_STRING("1234567", fmt_num(1234567.4).c_str());
    TEST_ASSERT_EQUAL_STRING("0.7033805", fmt_num((double)0.703380466f).c_str());
    TEST_ASSERT_EQUAL_STRING("0.000001", fmt_num(1e-6).c_str());
    TEST_ASSERT_EQUAL_STRING("0", fmt_num(4e-8).c_str());                  // rounds to zero, no exponent form
    TEST_ASSERT_EQUAL_STRING("1", fmt_num(0.99999996).c_str());            // carry into the integer part
    TEST_ASSERT_EQUAL_STRING("2", fmt_num(1.9999999).c_str());
    TEST_ASSERT_EQUAL_STRING("-1.5", fmt_num(-1.5).c_str());
    TEST_ASSERT_EQUAL_STRING("0", fmt_num(-4e-8).c_str());                 // no "-0"
    TEST_ASSERT_EQUAL_STRING("3000000000", fmt_num((double)3e9f).c_str());
    TEST_ASSERT_EQUAL_STRING("1000000000000000", fmt_num(1e15).c_str());
    TEST_ASSERT_EQUAL_STRING("null", fmt_num(NAN).c_str());
    TEST_ASSERT_EQUAL_STRING("null", fmt_num(INFINITY).c_str());
    TEST_ASSERT_EQUAL_STRING("null", fmt_num(-INFINITY).c_str());
    TEST_ASSERT_EQUAL_STRING("null", fmt_num(1e19).c_str());               // beyond uint64 range
    TEST_ASSERT_EQUAL_STRING("null", fmt_num((double)3.4e38f).c_str());
    // integers
    char b[64];
    J::Writer w(b, sizeof b);
    w.u64(18446744073709551615ull).ch(' ').u64(0).ch(' ').i64(-9223372036854775807ll - 1).ch(' ').i64(-5);
    w.finish();
    TEST_ASSERT_EQUAL_STRING("18446744073709551615 0 -9223372036854775808 -5\n", b);
    // ratio: negative or NaN = unknown
    J::Writer r(b, sizeof b);
    r.ratio(-1.0f).ch(' ').ratio(0.0f).ch(' ').ratio(NAN).ch(' ').ratio(2.5f);
    r.finish();
    TEST_ASSERT_EQUAL_STRING("null 0 null 2.5\n", b);
}

static void test_json_string_escaping() {
    char b[128];
    J::Writer w(b, sizeof b);
    w.str("plain");
    w.finish();
    TEST_ASSERT_EQUAL_STRING("\"plain\"\n", b);
    J::Writer w2(b, sizeof b);
    w2.str("a\"b\\c\n\r\t\b\f\x01\x1f/\xc3\xa9");
    w2.finish();
    TEST_ASSERT_EQUAL_STRING("\"a\\\"b\\\\c\\n\\r\\t\\b\\f\\u0001\\u001f/\xc3\xa9\"\n", b);
    J::Writer w3(b, sizeof b);
    w3.str("");
    w3.finish();
    TEST_ASSERT_EQUAL_STRING("\"\"\n", b);
}

static Event mk_event(uint8_t kind, uint8_t cls) {
    Event e;
    memset(&e, 0, sizeof e);
    e.kind = kind;
    e.cls = cls;
    e.ae[0] = e.ae[1] = -1.0f;             // unknown
    e.f1[0] = e.f1[1] = e.drop[0] = e.drop[1] = NAN;
    return e;
}

static void test_json_meta_line() {
    char b[J::JSON_LINE_MAX];
    J::Meta m;
    m.source = "netsim";
    m.cfg = chosen_config();
    m.floor1 = 5;
    m.floor2 = 10;
    m.epoch = 2866824256u;
    m.samples = true;
    size_t n = J::meta_line(b, sizeof b, m);
    const char *want = "{\"type\":\"meta\",\"schema\":3,\"source\":\"netsim\",\"rule\":\"f\",\"T\":3,\"T_ae\":6,\"gate\":92.12,\"use_raw\":false,\"debounce\":1,"
                       "\"commission_windows\":20,\"status_every_sf\":64,\"dmg_delta\":0.02,\"dmg_p\":32,\"superframe_s\":1.4,\"floors\":[5,10],\"epoch\":2866824256,\"samples\":true}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    // unknown epoch -> null, escaped source, samples off, other rule/T/raw, negative floor
    m.source = "a\"b\\";
    m.epoch = 0;
    m.samples = false;
    m.cfg = cfg('d', 2.5f, true, 12, 32, 3);
    m.floor1 = -1;
    n = J::meta_line(b, sizeof b, m);
    want = "{\"type\":\"meta\",\"schema\":3,\"source\":\"a\\\"b\\\\\",\"rule\":\"d\",\"T\":2.5,\"use_raw\":true,\"debounce\":3,"
           "\"commission_windows\":12,\"status_every_sf\":32,\"dmg_delta\":0.02,\"dmg_p\":32,\"superframe_s\":1.4,\"floors\":[-1,10],\"epoch\":null,\"samples\":false}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
}

static void test_json_event_lines() {
    char b[J::JSON_LINE_MAX];
    Event e = mk_event(EV_BEGIN, LOCAL);
    e.slave = 1;
    e.sf = 45;
    e.t_us = 64329824ull;
    e.r[0] = 9.5f;
    e.r[1] = -1.0f;                 // no report -> null
    e.base[0] = 75.2f;
    e.base[1] = 102.6f;
    e.alive_mask = 3;
    e.closed = 46;
    e.n_events = 3;
    e.commission_n[0] = e.commission_n[1] = 20;
    e.ae[0] = 12.5f;                // AE ratio of slave 1, slave 2 has none
    e.f1[0] = 5.375f;
    e.drop[0] = 0.0125f;
    e.dmg = 1;
    size_t n = J::event_line(b, sizeof b, e);
    const char *want = "{\"type\":\"begin\",\"t_us\":64329824,\"sf\":45,\"class\":\"LOCAL\",\"slave\":1,\"degraded_alive\":0,\"r1\":9.5,\"r2\":null,"
                       "\"ae\":[12.5,null],\"f1\":[5.375,null],\"drop\":[0.0125,null],\"dmg\":1,"
                       "\"base1\":75.2,\"base2\":102.6,\"alive\":[true,true],\"closed\":46,\"events\":3,\"commission_n\":[20,20]}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    // change, degraded, one slave dead, baseline unknown (0), commissioning
    e = mk_event(EV_CHANGE, COMMISSIONING);
    e.degraded = 2;
    e.sf = 0;
    e.t_us = 1329818ull;
    e.r[0] = e.r[1] = -1.0f;
    e.alive_mask = 2;
    e.closed = 1;
    e.n_events = 1;
    e.commission_n[0] = 1;
    e.commission_n[1] = 0;
    J::event_line(b, sizeof b, e);
    want = "{\"type\":\"change\",\"t_us\":1329818,\"sf\":0,\"class\":\"COMMISSIONING\",\"slave\":0,\"degraded_alive\":2,\"r1\":null,\"r2\":null,"
           "\"ae\":[null,null],\"f1\":[null,null],\"drop\":[null,null],\"dmg\":0,\"base1\":0,\"base2\":0,\"alive\":[false,true],\"closed\":1,\"events\":1,\"commission_n\":[1,0]}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    // status, BUILDING, ratio exactly 0
    e = mk_event(EV_STATUS, BUILDING);
    e.sf = 63;
    e.t_us = 92101531ull;
    e.r[0] = 0.0f;
    e.r[1] = 3.0f;
    e.base[0] = e.base[1] = 100.0f;
    e.alive_mask = 0;
    e.closed = 64;
    e.n_events = 8;
    J::event_line(b, sizeof b, e);
    want = "{\"type\":\"status\",\"t_us\":92101531,\"sf\":63,\"class\":\"BUILDING\",\"slave\":0,\"degraded_alive\":0,\"r1\":0,\"r2\":3,"
           "\"ae\":[null,null],\"f1\":[null,null],\"drop\":[null,null],\"dmg\":0,\"base1\":100,\"base2\":100,\"alive\":[false,false],\"closed\":64,\"events\":8,\"commission_n\":[0,0]}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    e.cls = NO_DATA;
    J::event_line(b, sizeof b, e);
    TEST_ASSERT_NOT_NULL(strstr(b, "\"class\":\"NO_DATA\""));
}

static void test_json_sample_and_restart_lines() {
    char b[J::JSON_LINE_MAX];
    Sample s;
    memset(&s, 0, sizeof s);
    s.t_us = 1400;
    s.sf = 7;
    s.closed = 8;
    s.cls = LOCAL;
    s.slave = 2;
    s.r[0] = 0.25f;
    s.r[1] = 4.5f;
    s.alive_mask = 2;
    s.age_sf[0] = -1;
    s.age_sf[1] = 0;
    s.commission_n[0] = s.commission_n[1] = 20;
    s.ae[0] = -1.0f;
    s.ae[1] = 12.5f;
    s.f1[0] = NAN;
    s.f1[1] = 5.4f;
    s.drop[0] = NAN;
    s.drop[1] = 0.0123f;
    s.dmg = 0;
    size_t n = J::sample_line(b, sizeof b, s);
    const char *want = "{\"type\":\"sample\",\"t_us\":1400,\"sf\":7,\"class\":\"LOCAL\",\"slave\":2,\"degraded_alive\":0,\"r1\":0.25,\"r2\":4.5,"
                       "\"ae\":[null,12.5],\"f1\":[null,5.4],\"drop\":[null,0.0123],\"dmg\":0,\"alive\":[false,true],\"age\":[null,0],\"commission_n\":[20,20],\"closed\":8}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    s.cls = AMBIENT;
    s.slave = 0;
    s.degraded = 1;
    s.r[1] = -1.0f;
    s.alive_mask = 1;
    s.age_sf[0] = 0;
    s.age_sf[1] = 12;
    s.ae[1] = 3.9e18f;                 // an absurd ratio is shown capped at 1e9
    s.f1[0] = 11.99f;
    s.drop[0] = -0.2f;
    s.dmg = 1;
    J::sample_line(b, sizeof b, s);
    want = "{\"type\":\"sample\",\"t_us\":1400,\"sf\":7,\"class\":\"AMBIENT\",\"slave\":0,\"degraded_alive\":1,\"r1\":0.25,\"r2\":null,"
           "\"ae\":[null,1000000000],\"f1\":[11.99,5.4],\"drop\":[-0.2,0.0123],\"dmg\":1,\"alive\":[true,false],\"age\":[0,12],\"commission_n\":[20,20],\"closed\":8}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);

    n = J::restart_line(b, sizeof b, 5, 7, 0);
    TEST_ASSERT_EQUAL_STRING("{\"type\":\"restart\",\"t_us\":5,\"epoch\":7,\"prev_epoch\":null}\n", b);
    TEST_ASSERT_EQUAL_UINT32(strlen(b), n);
    J::restart_line(b, sizeof b, 123456789012ull, 4294967295u, 3u);
    TEST_ASSERT_EQUAL_STRING("{\"type\":\"restart\",\"t_us\":123456789012,\"epoch\":4294967295,\"prev_epoch\":3}\n", b);
    J::restart_line(b, sizeof b, 1, 0, 0);
    TEST_ASSERT_EQUAL_STRING("{\"type\":\"restart\",\"t_us\":1,\"epoch\":null,\"prev_epoch\":null}\n", b);
}

struct StubJson : J::JsonLineSink {
    std::vector<std::string> out;
    std::vector<int> is_s;
    bool accept = true;
    bool write_line(const char *l, size_t n, bool smp) override {
        if (!accept) return false;
        out.push_back(std::string(l, n));
        is_s.push_back(smp ? 1 : 0);
        return true;
    }
};

static void test_json_node_line() {
    char b[J::JSON_LINE_MAX];
    size_t n = J::node_line(b, sizeof b, 64329824ull, 45, 2, false);
    const char *want = "{\"type\":\"node\",\"t_us\":64329824,\"sf\":45,\"slave\":2,\"state\":\"DEAD\"}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    n = J::node_line(b, sizeof b, 0, 0, 1, true);
    want = "{\"type\":\"node\",\"t_us\":0,\"sf\":0,\"slave\":1,\"state\":\"ALIVE\"}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    // largest values fit; a buffer one byte short returns 0
    n = J::node_line(b, sizeof b, 18446744073709551615ull, 4294967295u, 255, false);
    TEST_ASSERT_TRUE(n > 0 && n < J::JSON_LINE_MAX - 32);
    char exact[96];
    size_t m = J::node_line(exact, sizeof exact, 5, 7, 1, true);
    TEST_ASSERT_EQUAL_UINT32(0, J::node_line(exact, m, 5, 7, 1, true));
    TEST_ASSERT_EQUAL_UINT32(m, J::node_line(exact, m + 1, 5, 7, 1, true));
    // the sink writes it through the same counters and flags it as a non-sample line
    StubJson sj;
    sj.node(1234, 9, 2, false);
    sj.node(5678, 12, 2, true);
    TEST_ASSERT_EQUAL_UINT32(2, sj.lines);
    TEST_ASSERT_EQUAL_UINT32(0, sj.sample_lines);
    TEST_ASSERT_EQUAL_STRING("{\"type\":\"node\",\"t_us\":1234,\"sf\":9,\"slave\":2,\"state\":\"DEAD\"}\n", sj.out[0].c_str());
    TEST_ASSERT_EQUAL_INT(0, sj.is_s[1]);
    sj.accept = false;
    sj.node(1, 1, 1, false);
    TEST_ASSERT_EQUAL_UINT32(1, sj.dropped);
}

static void test_json_overflow_and_worst_case() {
    char small[24];
    Event e = mk_event(EV_BEGIN, LOCAL);
    TEST_ASSERT_EQUAL_UINT32(0, J::event_line(small, sizeof small, e));         // too small: 0, never a half line reported as ok
    TEST_ASSERT_EQUAL_UINT32(0, J::event_line(small, 0, e));
    // a buffer exactly one byte short fails, the exact size works
    char b[J::JSON_LINE_MAX];
    size_t n = J::restart_line(b, sizeof b, 5, 7, 0);
    char exact[256];
    TEST_ASSERT_EQUAL_UINT32(0, J::restart_line(exact, n, 5, 7, 0));            // needs n + 1 for the NUL
    TEST_ASSERT_EQUAL_UINT32(n, J::restart_line(exact, n + 1, 5, 7, 0));
    // longest possible lines fit JSON_LINE_MAX with room to spare
    Event w = mk_event(EV_CHANGE, COMMISSIONING);
    w.slave = 2; w.degraded = 2; w.sf = 4294967295u; w.t_us = 18446744073709551615ull;
    w.r[0] = w.r[1] = 3999999999.123456f;
    w.base[0] = w.base[1] = 3.9e18f;
    w.alive_mask = 0; w.closed = w.n_events = 4294967295u;
    w.commission_n[0] = w.commission_n[1] = 255;
    n = J::event_line(b, sizeof b, w);
    TEST_ASSERT_TRUE(n > 0 && n < J::JSON_LINE_MAX - 32);
    Sample s;
    memset(&s, 0, sizeof s);
    s.t_us = 18446744073709551615ull; s.sf = s.closed = 4294967295u; s.cls = COMMISSIONING; s.slave = 2; s.degraded = 2;
    s.r[0] = s.r[1] = 3.9e18f; s.age_sf[0] = s.age_sf[1] = 2147483647; s.commission_n[0] = s.commission_n[1] = 255;
    n = J::sample_line(b, sizeof b, s);
    TEST_ASSERT_TRUE(n > 0 && n < J::JSON_LINE_MAX - 32);
}

static void test_json_sink_counts_and_drops() {
    StubJson sj;
    Fusion f(&sj, cfg('b', 2.0f, false, 3));
    run_script(f);
    TEST_ASSERT_EQUAL_UINT32(8, sj.sample_lines);
    TEST_ASSERT_EQUAL_UINT32(8 + f.events_emitted() + 0u, sj.lines - 0u + 0u - 0u + 0u);   // events + samples; no status in 8 sf
    TEST_ASSERT_EQUAL_UINT32(0, sj.dropped);
    TEST_ASSERT_EQUAL_UINT32(sj.out.size(), sj.lines);
    for (size_t i = 0; i < sj.out.size(); i++) {
        TEST_ASSERT_EQUAL_CHAR('\n', sj.out[i][sj.out[i].size() - 1]);
        TEST_ASSERT_EQUAL_CHAR('{', sj.out[i][0]);
        TEST_ASSERT_TRUE(sj.out[i].find('\n') == sj.out[i].size() - 1);   // exactly one line each
    }
    // order per superframe: sample first, then its event (sf 0)
    TEST_ASSERT_NOT_NULL(strstr(sj.out[0].c_str(), "\"type\":\"sample\""));
    TEST_ASSERT_NOT_NULL(strstr(sj.out[1].c_str(), "\"type\":\"change\""));
    // samples off: no sample lines, events unchanged
    StubJson so;
    so.samples = false;
    Fusion f2(&so, cfg('b', 2.0f, false, 3));
    run_script(f2);
    TEST_ASSERT_EQUAL_UINT32(0, so.sample_lines);
    TEST_ASSERT_EQUAL_UINT32(f2.events_emitted(), so.lines);
    for (size_t i = 0; i < so.out.size(); i++) TEST_ASSERT_NULL(strstr(so.out[i].c_str(), "\"sample\""));
    // a writer that cannot take the line: counted as dropped, the fusion run is unaffected
    StubJson sd;
    sd.accept = false;
    Fusion f3(&sd, cfg('b', 2.0f, false, 3));
    run_script(f3);
    TEST_ASSERT_EQUAL_UINT32(0, sd.lines);
    TEST_ASSERT_EQUAL_UINT32(8 + f3.events_emitted(), sd.dropped);
    TEST_ASSERT_EQUAL_UINT32(8, sd.sample_dropped);
    TEST_ASSERT_EQUAL_UINT32(f.events_emitted(), f3.events_emitted());
    TEST_ASSERT_EQUAL_UINT32(f.closed(), f3.closed());
    // host code lines (meta/restart) go through the same counters
    StubJson sh;
    char b[J::JSON_LINE_MAX];
    sh.emit(b, J::restart_line(b, sizeof b, 1, 2, 0), false);
    sh.emit(b, 0, false);                                   // formatter overflow
    TEST_ASSERT_EQUAL_UINT32(1, sh.lines);
    TEST_ASSERT_EQUAL_UINT32(1, sh.dropped);
    TEST_ASSERT_EQUAL_UINT32(1, sh.overflow);
}


// ------------------------------------------------------------------ rule f (b OR e), Phase C2a
static void test_rule_f_or_of_separately_smoothed_flags() {
    Rig r(cfgf(6.0f, 3.0f));
    r.commission();
    // AE alone (ratio 6 exactly, ready): the AE flag needs its own 2-of-3
    expect(r.bothx(100, 100, 6.0f, 0.5f), AMBIENT);
    expect(r.bothx(100, 100, 6.0f, 0.5f), LOCAL, 1);
    r.bothx(100, 100, 0.5f, 0.5f);
    r.bothx(100, 100, 0.5f, 0.5f);
    expect(r.bothx(100, 100, 0.5f, 0.5f), AMBIENT);
    // b alone (az_std 3 x the baseline): the b flag as in rule b
    r.bothx(300, 100, 0.5f, 0.5f);
    expect(r.bothx(300, 100, 0.5f, 0.5f), LOCAL, 1);
    for (int i = 0; i < 3; i++) r.bothx(100, 100, 0.5f, 0.5f);
    // the two flags are smoothed SEPARATELY: b-only, AE-only, quiet, b-only, ... reaches 2 of 3 in neither history (one flag
    // smoothed after the OR would see 1,1,0,1,1,0 and fire)
    for (int i = 0; i < 9; i++) {
        const Decision &d = (i % 3 == 0) ? r.bothx(300, 100, 0.5f, 0.5f) : (i % 3 == 1) ? r.bothx(100, 100, 6.0f, 0.5f) : r.bothx(100, 100, 0.5f, 0.5f);
        expect(d, AMBIENT);
    }
    for (int i = 0; i < 3; i++) r.bothx(100, 100, 0.5f, 0.5f);
    // slave 1 by b, slave 2 by the AE: both e = BUILDING (OR per slave, then the b decision)
    r.bothx(300, 100, 0.5f, 6.0f);
    expect(r.bothx(300, 100, 0.5f, 6.0f), BUILDING);
    // the report keeps its ratio: r = az_std / baseline, not the AE value
    TEST_ASSERT_EQUAL_FLOAT(3.0f, r.col.dec.back().r[0]);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, r.col.dec.back().r[1]);
    TEST_ASSERT_EQUAL_FLOAT(0.5f, r.col.dec.back().ae[0]);
    TEST_ASSERT_EQUAL_FLOAT(6.0f, r.col.dec.back().ae[1]);
}

static void test_rule_f_threshold_gate_and_ready_edges() {
    // AE ratio exactly T and one ulp either side
    {
        float T = 6.0f, below = nextafterf(T, 0.0f), above = nextafterf(T, 1e9f);
        Rig a(cfgf(T)); a.commission(); a.bothx(100, 100, below, 0.5f);
        expect(a.bothx(100, 100, below, 0.5f), AMBIENT);
        Rig b(cfgf(T)); b.commission(); b.bothx(100, 100, T, 0.5f);
        expect(b.bothx(100, 100, T, 0.5f), LOCAL, 1);
        Rig c(cfgf(T)); c.commission(); c.bothx(100, 100, 0.5f, above);
        expect(c.bothx(100, 100, 0.5f, above), LOCAL, 2);
    }
    // gate: az_std one ulp below 92.12 silences the AE flag, exactly the gate (and above) does not
    {
        float g = CHOSEN_AE_GATE, below = nextafterf(g, 0.0f), above = nextafterf(g, 1e9f);
        Rig a(cfgf(6.0f)); a.commission(100.0f, 100.0f); a.bothx(below, 100, 100.0f, 0.5f);
        expect(a.bothx(below, 100, 100.0f, 0.5f), AMBIENT);
        Rig b(cfgf(6.0f)); b.commission(100.0f, 100.0f); b.bothx(g, 100, 100.0f, 0.5f);
        expect(b.bothx(g, 100, 100.0f, 0.5f), LOCAL, 1);
        Rig c(cfgf(6.0f)); c.commission(100.0f, 100.0f); c.bothx(above, 100, 100.0f, 0.5f);
        expect(c.bothx(above, 100, 100.0f, 0.5f), LOCAL, 1);
    }
    // AE_READY: not ready = flag false (ratio ignored, history advances with 0), ready from then on
    {
        Rig r(cfgf(6.0f));
        r.commission();
        r.bothx(100, 100, 100.0f, 100.0f, false, false);
        expect(r.bothx(100, 100, 100.0f, 100.0f, false, false), AMBIENT);
        r.bothx(100, 100, 100.0f, 100.0f, true, true);
        expect(r.bothx(100, 100, 100.0f, 100.0f, true, true), BUILDING);
        // ready on one slave only
        Rig q(cfgf(6.0f));
        q.commission();
        q.bothx(100, 100, 100.0f, 100.0f, true, false);
        expect(q.bothx(100, 100, 100.0f, 100.0f, true, false), LOCAL, 1);
    }
    // the other rules ignore the AE completely
    Rig o(cfg('b', 3.0f, false));
    o.commission();
    o.bothx(100, 100, 1000.0f, 1000.0f);
    expect(o.bothx(100, 100, 1000.0f, 1000.0f), AMBIENT);
}

static void test_rule_f_degraded_dead_alive_and_restart() {
    Rig r(cfgf(6.0f));
    r.commission();
    // only slave 2 reports (degraded): LOCAL(2) iff its (b OR e)
    r.f.advance(r.sf, true, r.t() - 1000); r.onex(2, 100, 6.0f, true); r.f.advance(r.sf, false, r.t() + 1000); r.sf++;
    expect(r.col.dec.back(), AMBIENT, 0, 2);
    r.f.advance(r.sf, true, r.t() - 1000); r.onex(2, 100, 6.0f, true); r.f.advance(r.sf, false, r.t() + 1000); r.sf++;
    expect(r.col.dec.back(), LOCAL, 2, 2);
    r.f.advance(r.sf, true, r.t() - 1000); r.onex(2, 300, 0.5f, true); r.f.advance(r.sf, false, r.t() + 1000); r.sf++;
    expect(r.col.dec.back(), LOCAL, 2, 2);               // the b flag joins by OR (AE history 1,1,0 holds, b history 0,0,1)
    // slave 1 missing: its two histories are held; slave 2 DEAD then ALIVE clears BOTH of slave 2's
    Rig h(cfgf(6.0f));
    h.commission();
    h.bothx(300, 100, 0.5f, 6.0f);                        // slave 1: b history 1; slave 2: AE history 1
    h.f.on_status(2, false, h.t());
    h.f.on_status(2, true, h.t());                        // cleared
    h.onex(1, 300, 0.5f, true); h.onex(2, 100, 6.0f, true); h.sf++;
    expect(h.col.dec.back(), LOCAL, 1);                   // slave 1 held its history (b: 1,1 -> on), slave 2's AE restarted (1 of 3: not yet)
    // master restart (superframe numbering back to 0) clears the histories too
    Rig q(cfgf(6.0f));
    q.commission();
    q.bothx(100, 100, 6.0f, 0.5f);
    q.sf = 0;
    q.bothx(100, 100, 6.0f, 0.5f);
    expect(q.col.dec.back(), AMBIENT);
    expect(q.bothx(100, 100, 6.0f, 0.5f), LOCAL, 1);
    // both slaves missing: NO_DATA, histories held
    Rig n(cfgf(6.0f));
    n.commission();
    n.bothx(100, 100, 6.0f, 0.5f);
    expect(n.none(), NO_DATA);
    expect(n.bothx(100, 100, 6.0f, 0.5f), LOCAL, 1);
}

static void test_report_pkt_optional_fields_sanitised() {
    // A report straight from the wire (ReportPkt): non-finite ae / f1 with their flag set -> that field unavailable, the report
    // (and rule b's az_std) is KEPT. Fusion sanitises again, so a direct caller cannot feed it garbage either.
    Col col;
    Fusion f(&col, cfgf(6.0f));
    net::ReportPkt p;
    memset(&p, 0, sizeof p);
    p.f[0] = 100.0f;
    for (uint32_t sf = 0; sf < 3; sf++)
        for (int s = 1; s <= 2; s++) { p.sf = sf; f.on_report((uint8_t)s, p, sf * 1400000ull); }
    TEST_ASSERT_TRUE(f.commissioned());
    p.sf = 3;
    p.f[0] = 400.0f;                                       // 4 x the baseline: b flag on slave 1
    p.flags = net::FLAG_AE_READY | net::FLAG_F1_VALID | net::FLAG_F1_COMMISSIONED;
    p.ae_ratio = NAN;
    p.f1_est = INFINITY;
    p.f1_drop = NAN;
    f.on_report(1, p, 4000000ull);
    p.f[0] = 100.0f;
    f.on_report(2, p, 4000001ull);
    TEST_ASSERT_EQUAL_UINT32(4, f.stats().rejected_reports + f.stats().ae_bad + f.stats().f1_bad + 0u);   // ae 2, f1 2 + 2 more below (slave 2)
    TEST_ASSERT_EQUAL_UINT32(0, f.stats().rejected_reports);
    TEST_ASSERT_TRUE(f.stats().ae_bad >= 2 && f.stats().f1_bad >= 2);
    TEST_ASSERT_EQUAL_UINT32(4, f.closed());
    expect(col.dec.back(), AMBIENT);                       // b needs 2 of 3 on slave 1
    p.sf = 4;
    p.f[0] = 400.0f;
    f.on_report(1, p, 5000000ull);
    p.f[0] = 100.0f;
    f.on_report(2, p, 5000001ull);
    expect(col.dec.back(), LOCAL, 1);                      // rule b still works on those reports
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, col.dec.back().ae[0]);  // AE shown as unknown
    TEST_ASSERT_TRUE(col.dec.back().f1[0] != col.dec.back().f1[0]);   // NAN
    TEST_ASSERT_FALSE(f.damage_active());
}

// ------------------------------------------------------------------ DAMAGE channel on the master
struct DCol : SampCol {
    std::vector<DamageEvent> dmg;
    void on_damage(const DamageEvent &d) override { dmg.push_back(d); }
};
struct DRig {
    DCol col;
    Fusion f;
    uint32_t sf;
    explicit DRig(const Config &c) : f(&col, c), sf(0) {}
    uint64_t t() const { return (uint64_t)sf * 1400000ull + 1320000ull; }
    void onex(int slave, float f1, float drop) {
        SlaveIn in;
        in.raw = in.state = 0;
        in.f0 = 100.0f;
        in.xflags = (uint8_t)(net::FLAG_AE_READY | (f1 == f1 ? net::FLAG_F1_VALID : 0) | ((f1 == f1 && drop == drop) ? net::FLAG_F1_COMMISSIONED : 0));
        in.ae = 0.5f;
        in.f1_est = f1;
        in.f1_drop = drop;
        f.on_report_ex((uint8_t)slave, sf, in, t());
    }
    void both(float d1, float d2) {   // f1_0 = 5 and 6 Hz
        onex(1, 5.0f * (1.0f - d1), d1);
        onex(2, 6.0f * (1.0f - d2), d2);
        sf++;
    }
};

static void test_damage_begin_end_and_pooling() {
    DRig r(cfgf(6.0f));
    for (int i = 0; i < 3; i++) r.both(0.0f, 0.0f);   // fusion commissioning: the damage channel is already fed
    TEST_ASSERT_TRUE(r.f.commissioned());
    TEST_ASSERT_FALSE(r.f.damage_active());
    // pooled drop = (0.04 + 0) / 2 = 0.02 = DMG_DELTA exactly (inclusive): BEGIN after exactly DMG_P consecutive superframes
    for (int i = 0; i < DMG_P - 1; i++) r.both(0.04f, 0.0f);
    TEST_ASSERT_EQUAL_UINT32(0, (uint32_t)r.col.dmg.size());
    TEST_ASSERT_FALSE(r.f.damage_active());
    r.both(0.04f, 0.0f);
    TEST_ASSERT_EQUAL_UINT32(1, (uint32_t)r.col.dmg.size());
    TEST_ASSERT_TRUE(r.f.damage_active());
    const DamageEvent &b = r.col.dmg[0];
    TEST_ASSERT_TRUE(b.begin);
    TEST_ASSERT_EQUAL_UINT32(r.sf - 1, b.sf);
    TEST_ASSERT_EQUAL_FLOAT(0.02f, b.drop);
    TEST_ASSERT_EQUAL_FLOAT(5.0f * (1.0f - 0.04f), b.f1[0]);
    TEST_ASSERT_EQUAL_FLOAT(6.0f, b.f1[1]);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 5.0f, b.f1_0[0]);     // f1_0 = f1 / (1 - drop), derived on the master
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 6.0f, b.f1_0[1]);
    TEST_ASSERT_TRUE(b.t_us == r.col.smp.back().t_us);
    TEST_ASSERT_EQUAL_INT(1, r.col.smp.back().dmg);       // the sample of that superframe already shows the state
    TEST_ASSERT_EQUAL_INT(0, r.col.smp[r.col.smp.size() - 2].dmg);
    // the damage channel does not change the class decision (rule f stays AMBIENT)
    TEST_ASSERT_EQUAL_INT(AMBIENT, r.col.dec.back().cls);
    TEST_ASSERT_EQUAL_FLOAT(0.04f, r.col.smp.back().drop[0]);
    // hysteresis: pooled between delta/2 and delta does not end it; END after DMG_P below delta/2 = 0.01
    for (int i = 0; i < 40; i++) r.both(0.015f, 0.015f);
    TEST_ASSERT_TRUE(r.f.damage_active());
    for (int i = 0; i < DMG_P - 1; i++) r.both(0.0f, 0.005f);
    TEST_ASSERT_TRUE(r.f.damage_active());
    r.both(0.0f, 0.005f);
    TEST_ASSERT_FALSE(r.f.damage_active());
    TEST_ASSERT_EQUAL_UINT32(2, (uint32_t)r.col.dmg.size());
    TEST_ASSERT_FALSE(r.col.dmg[1].begin);
    TEST_ASSERT_EQUAL_UINT32(1, r.f.stats().dmg_begins);
    TEST_ASSERT_EQUAL_UINT32(1, r.f.stats().dmg_ends);
}

static void test_damage_missing_slave_and_no_report_hold() {
    DRig r(cfgf(6.0f));
    for (int i = 0; i < 3; i++) r.both(0.0f, 0.0f);
    // slave 2 reports nothing for a stretch (its superframes are closed by the master finishing its polling): only slave 1 pools
    for (int i = 0; i < DMG_P; i++) {
        r.f.advance(r.sf, true, r.t() - 1000);
        r.onex(1, 5.0f * 0.97f, 0.03f);
        r.f.advance(r.sf, false, r.t() + 1000);
        r.sf++;
    }
    TEST_ASSERT_TRUE(r.f.damage_active());                // pooled over slave 1 alone: 0.03
    TEST_ASSERT_EQUAL_UINT32(1, (uint32_t)r.col.dmg.size());
    TEST_ASSERT_TRUE(r.col.dmg[0].f1[1] != r.col.dmg[0].f1[1]);   // slave 2: no usable estimate -> null in the log
    // silence from both: state AND run counters are held (no END however long)
    for (int i = 0; i < 100; i++) {
        r.f.advance(r.sf, true, r.t() - 1000);
        r.f.advance(r.sf, false, r.t() + 1000);
        r.sf++;
    }
    TEST_ASSERT_TRUE(r.f.damage_active());
    TEST_ASSERT_EQUAL_UINT32(1, (uint32_t)r.col.dmg.size());
    // a report whose f1 is not valid, or not commissioned, contributes nothing (neither up nor down)
    for (int i = 0; i < 100; i++) {
        r.onex(1, NAN, NAN);                               // F1_VALID clear
        r.onex(2, 6.0f * 0.9f, NAN);                       // valid estimate but no commissioned drop
        r.sf++;
    }
    TEST_ASSERT_TRUE(r.f.damage_active());
    // slave 1's DEAD slot does not count either; back with a low drop: END after P
    for (int i = 0; i < DMG_P; i++) {
        r.f.advance(r.sf, true, r.t() - 1000);
        r.onex(2, 6.0f, 0.0f);
        r.f.advance(r.sf, false, r.t() + 1000);
        r.sf++;
    }
    TEST_ASSERT_FALSE(r.f.damage_active());
    TEST_ASSERT_EQUAL_UINT32(2, (uint32_t)r.col.dmg.size());
    // fresh start without any commissioned slave: nothing ever happens
    DRig q(cfgf(6.0f));
    for (int i = 0; i < 200; i++) { q.onex(1, 5.0f, NAN); q.onex(2, 5.0f, NAN); q.sf++; }
    TEST_ASSERT_EQUAL_UINT32(0, (uint32_t)q.col.dmg.size());
}

static void test_damage_survives_stream_restart_and_status() {
    DRig r(cfgf(6.0f));
    for (int i = 0; i < 3; i++) r.both(0.0f, 0.0f);
    for (int i = 0; i < DMG_P; i++) r.both(0.05f, 0.05f);
    TEST_ASSERT_TRUE(r.f.damage_active());
    r.sf = 0;                                              // master restart: numbering restarts, the structure is still damaged
    r.both(0.05f, 0.05f);
    TEST_ASSERT_EQUAL_UINT32(1, r.f.stats().restarts);
    TEST_ASSERT_TRUE(r.f.damage_active());
    TEST_ASSERT_EQUAL_INT(1, r.col.smp.back().dmg);
    // events carry the state too
    TEST_ASSERT_TRUE(r.col.ev.size() > 0);
}

// ------------------------------------------------------------------ schema 3: damage line, sink order, widths
static void test_json_damage_line_and_sink_order() {
    char b[J::JSON_LINE_MAX];
    DamageEvent d;
    d.t_us = 64329824ull;
    d.sf = 45;
    d.begin = true;
    d.drop = 0.0205f;
    d.f1[0] = 5.25f;
    d.f1[1] = NAN;
    d.f1_0[0] = 5.5f;
    d.f1_0[1] = 6.0f;
    size_t n = J::damage_line(b, sizeof b, d);
    const char *want = "{\"type\":\"damage\",\"t_us\":64329824,\"sf\":45,\"state\":\"BEGIN\",\"drop\":0.0205,\"f1\":[5.25,null],\"f1_0\":[5.5,6]}\n";
    TEST_ASSERT_EQUAL_STRING(want, b);
    TEST_ASSERT_EQUAL_UINT32(strlen(want), n);
    d.begin = false;
    d.drop = NAN;
    d.f1_0[1] = NAN;
    J::damage_line(b, sizeof b, d);
    TEST_ASSERT_EQUAL_STRING("{\"type\":\"damage\",\"t_us\":64329824,\"sf\":45,\"state\":\"END\",\"drop\":null,\"f1\":[5.25,null],\"f1_0\":[5.5,null]}\n", b);
    // through the sink: the damage line comes after the sample and before the class event of its superframe, as a non-sample line
    StubJson sj;
    DRig r(cfgf(6.0f));
    r.f.set_sink(&sj);
    for (int i = 0; i < 3; i++) r.both(0.0f, 0.0f);
    for (int i = 0; i < DMG_P; i++) r.both(0.03f, 0.03f);
    int di = -1;
    for (size_t i = 0; i < sj.out.size(); i++)
        if (sj.out[i].find("\"type\":\"damage\"") != std::string::npos) di = (int)i;
    TEST_ASSERT_TRUE(di > 0);
    TEST_ASSERT_NOT_NULL(strstr(sj.out[di - 1].c_str(), "\"type\":\"sample\""));
    TEST_ASSERT_NOT_NULL(strstr(sj.out[di - 1].c_str(), "\"dmg\":1"));
    TEST_ASSERT_EQUAL_INT(0, sj.is_s[di]);
    TEST_ASSERT_NOT_NULL(strstr(sj.out[di].c_str(), "\"state\":\"BEGIN\""));
    TEST_ASSERT_NOT_NULL(strstr(sj.out[di].c_str(), "\"drop\":0.03"));
}

static void test_json_schema3_worst_case_widths() {
    char b[J::JSON_LINE_MAX];
    // everything at its widest PLAUSIBLE value (what a Master lets through: f1 <= 100 Hz, |drop| <= 1, ae capped at 1e9 in the log)
    Event w = mk_event(EV_CHANGE, COMMISSIONING);
    w.slave = 2; w.degraded = 2; w.sf = 4294967295u; w.t_us = 18446744073709551615ull;
    w.r[0] = w.r[1] = 3999999999.123456f;
    w.base[0] = w.base[1] = 3.9e18f;
    w.alive_mask = 0; w.closed = w.n_events = 4294967295u;
    w.commission_n[0] = w.commission_n[1] = 255;
    w.ae[0] = w.ae[1] = 3.9e18f;
    w.f1[0] = w.f1[1] = 99.99999f;
    w.drop[0] = w.drop[1] = -0.9999999f;
    w.dmg = 1;
    size_t ne = J::event_line(b, sizeof b, w);
    Sample s;
    memset(&s, 0, sizeof s);
    s.t_us = 18446744073709551615ull; s.sf = s.closed = 4294967295u; s.cls = COMMISSIONING; s.slave = 2; s.degraded = 2;
    s.r[0] = s.r[1] = 3.9e18f; s.age_sf[0] = s.age_sf[1] = 2147483647; s.commission_n[0] = s.commission_n[1] = 255;
    s.ae[0] = s.ae[1] = 3.9e18f; s.f1[0] = s.f1[1] = 99.99999f; s.drop[0] = s.drop[1] = -0.9999999f; s.dmg = 1;
    size_t ns = J::sample_line(b, sizeof b, s);
    DamageEvent d;
    d.t_us = 18446744073709551615ull; d.sf = 4294967295u; d.begin = true; d.drop = -0.9999999f;
    d.f1[0] = d.f1[1] = d.f1_0[0] = d.f1_0[1] = 99.99999f;
    size_t nd = J::damage_line(b, sizeof b, d);
    J::Meta m;
    m.source = "wokwi_demo"; m.cfg = chosen_config(); m.floor1 = -2147483647; m.floor2 = 2147483647; m.epoch = 4294967295u; m.samples = true;
    size_t nm = J::meta_line(b, sizeof b, m);
    printf("schema-3 widest lines: event %u, sample %u, damage %u, meta %u (limit %u)\n", (unsigned)ne, (unsigned)ns, (unsigned)nd, (unsigned)nm, (unsigned)J::JSON_LINE_MAX);
    TEST_ASSERT_TRUE(ne > 0 && ne < J::JSON_LINE_MAX);
    TEST_ASSERT_TRUE(ns > 0 && ns < J::JSON_LINE_MAX);
    TEST_ASSERT_TRUE(nd > 0 && nd < J::JSON_LINE_MAX);
    TEST_ASSERT_TRUE(nm > 0 && nm < J::JSON_LINE_MAX);
    // and the absolute worst a hostile struct could carry (f1 / drop = 3.9e18, never produced by a Master) still fits
    w.f1[0] = w.f1[1] = w.drop[0] = w.drop[1] = 3.9e18f;
    s.f1[0] = s.f1[1] = s.drop[0] = s.drop[1] = 3.9e18f;
    d.drop = d.f1[0] = d.f1[1] = d.f1_0[0] = d.f1_0[1] = 3.9e18f;
    TEST_ASSERT_TRUE(J::event_line(b, sizeof b, w) > 0);
    TEST_ASSERT_TRUE(J::sample_line(b, sizeof b, s) > 0);
    TEST_ASSERT_TRUE(J::damage_line(b, sizeof b, d) > 0);
}

// ------------------------------------------------------------------ Master -> Fusion with the version-3 report (netsim)
static void test_netsim_rule_f_end_to_end() {
    netsim::SimConfig c;
    c.superframes = 60;
    c.quiet = true;
    netsim::Sim sim(c);      // analytics on: the slaves run the AE and the f1 tracker
    DCol col;
    Fusion f(&col);          // the deployed configuration (rule f)
    FusionListener tap(&f, &sim);
    sim.master().set_listener(&tap);
    sim.master_hook = [&](const net::Master &m, uint64_t now) { tap.service(m, now); };
    sim.run();
    f.flush(60ull * net::SUPERFRAME_US);
    TEST_ASSERT_EQUAL_INT('f', f.config().rule);
    TEST_ASSERT_TRUE(f.commissioned());
    TEST_ASSERT_EQUAL_UINT32(0, f.stats().ae_bad + f.stats().f1_bad);
    TEST_ASSERT_EQUAL_UINT32(0, sim.master().stats().rx_ae_bad + sim.master().stats().rx_f1_bad + sim.master().stats().rx_bad_field);
    // the slave AE is ready from its 21st window; before that the AE field is unknown in the log
    TEST_ASSERT_TRUE(col.smp.size() >= 55);
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, col.smp[10].ae[0]);
    TEST_ASSERT_TRUE(col.smp[30].ae[0] >= 0.0f);
    TEST_ASSERT_TRUE(col.smp[30].ae[1] >= 0.0f);
    // the synthetic windows are loud sines: f1 shows up once the zoom ring has 32 windows and stays inside the grid
    TEST_ASSERT_TRUE(col.smp[10].f1[0] != col.smp[10].f1[0]);
    TEST_ASSERT_TRUE(col.smp[50].f1[0] >= 3.0f && col.smp[50].f1[0] <= 12.0f);
}

static void test_parity_vectors() {
    FILE *fp = fopen(FUSION_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    uint32_t nseq = 0;
    SelRec sel;
    read_vectors_header(fp, &nseq, &sel);
    TEST_ASSERT_TRUE(nseq >= 89);   // 63 rule a-d candidates + 4 rule-c edges + 18 rule-f candidates + 1 rule-f edge sequence + 3 long chosen-config sequences
    uint32_t total_dec = 0, total_rec = 0, mism = 0, f_seq = 0;
    for (uint32_t s = 0; s < nseq; s++) {
        SeqHdr h;
        TEST_ASSERT_EQUAL_INT(1, (int)fread(&h, sizeof h, 1, fp));
        Config c = {(char)h.rule, h.T, h.use_raw != 0, h.comm_n, h.status_every, h.k, h.T_b, h.gate};
        f_seq += c.rule == 'f';
        std::vector<OpRec> ops(h.n_ops);
        std::vector<DecRec> dec(h.n_dec);
        std::vector<EvRec> rec(h.n_rec);
        TEST_ASSERT_EQUAL_INT((int)ops.size(), (int)fread(ops.data(), sizeof(OpRec), ops.size(), fp));
        TEST_ASSERT_EQUAL_INT((int)dec.size(), (int)fread(dec.data(), sizeof(DecRec), dec.size(), fp));
        TEST_ASSERT_EQUAL_INT((int)rec.size(), (int)fread(rec.data(), sizeof(EvRec), rec.size(), fp));
        Col col;
        Fusion f(&col, c);
        for (size_t i = 0; i < ops.size(); i++) {
            const OpRec &o = ops[i];
            uint64_t t = (uint64_t)o.t_ms * 1000ull;
            if (o.type == 1) {
                SlaveIn in;
                in.raw = o.fb & 3;
                in.state = (o.fb >> 2) & 3;
                in.f0 = o.f0;
                in.xflags = (o.fb & 16) ? net::FLAG_AE_READY : 0;
                in.ae = o.ae;
                in.f1_est = in.f1_drop = NAN;
                f.on_report_ex(o.slave, o.sf, in, t);
            }
            else if (o.type == 2) f.on_status(o.slave, o.fb != 0, t);
            else if (o.type == 3) f.advance(o.sf, o.fb != 0, t);
            else f.flush(t);
        }
        char msg[96];
        snprintf(msg, sizeof msg, "sequence %u rule %c: decision count", s, c.rule);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(dec.size(), col.dec.size(), msg);
        snprintf(msg, sizeof msg, "sequence %u rule %c: event count", s, c.rule);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(rec.size(), col.ev.size(), msg);
        for (size_t i = 0; i < dec.size(); i++) {
            const Decision &d = col.dec[i];
            bool ok = d.sf == dec[i].sf && d.cls == dec[i].cls && d.slave == dec[i].slave && d.degraded == dec[i].deg && same_float(d.r[0], dec[i].r1) &&
                      same_float(d.r[1], dec[i].r2);
            if (!ok) {
                mism++;
                if (mism < 5) printf("parity mismatch seq %u decision %u: sf %u/%u cls %d/%d slave %d/%d deg %d/%d r1 %g/%g r2 %g/%g\n", s, (unsigned)i, d.sf, dec[i].sf, d.cls, dec[i].cls,
                                     d.slave, dec[i].slave, d.degraded, dec[i].deg, (double)d.r[0], (double)dec[i].r1, (double)d.r[1], (double)dec[i].r2);
            }
        }
        for (size_t i = 0; i < rec.size(); i++) {
            const Event &e = col.ev[i];
            const EvRec &x = rec[i];
            bool ok = e.kind == x.kind && e.cls == x.cls && e.slave == x.slave && e.degraded == x.deg && e.sf == x.sf && e.t_us == (uint64_t)x.t_ms * 1000ull &&
                      same_float(e.r[0], x.r1) && same_float(e.r[1], x.r2) && same_float(e.base[0], x.b1) && same_float(e.base[1], x.b2) && e.alive_mask == x.alive &&
                      e.closed == x.closed && e.n_events == x.nev;
            if (!ok) {
                mism++;
                if (mism < 5) printf("parity mismatch seq %u event %u: kind %d/%d cls %d/%d sf %u/%u t %llu/%u closed %u/%u\n", s, (unsigned)i, e.kind, x.kind, e.cls, x.cls, e.sf, x.sf,
                                     (unsigned long long)e.t_us, x.t_ms, e.closed, x.closed);
            }
        }
        total_dec += (uint32_t)dec.size();
        total_rec += (uint32_t)rec.size();
    }
    fclose(fp);
    printf("fusion parity: %u sequences (%u rule f), %u decisions, %u events/statuses, %u mismatches\n", nseq, f_seq, total_dec, total_rec, mism);
    TEST_ASSERT_EQUAL_UINT32(0, mism);
    TEST_ASSERT_TRUE(f_seq >= 22);
    TEST_ASSERT_TRUE(total_dec > 1000);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_chosen_constants);
    RUN_TEST(test_commissioning_median_and_precedence);
    RUN_TEST(test_commissioning_waits_for_both_slaves);
    RUN_TEST(test_commissioning_ignores_extra_reports);
    RUN_TEST(test_baseline_floor);
    RUN_TEST(test_rule_a);
    RUN_TEST(test_rule_b);
    RUN_TEST(test_rule_b_building_needs_two_of_three);
    RUN_TEST(test_rule_c);
    RUN_TEST(test_rule_d);
    RUN_TEST(test_threshold_edges);
    RUN_TEST(test_degraded_per_rule);
    RUN_TEST(test_no_data_and_dead);
    RUN_TEST(test_degraded_event_and_recovery);
    RUN_TEST(test_events_only_on_change);
    RUN_TEST(test_debounce_k2);
    RUN_TEST(test_debounce_k3_interrupted_run);
    RUN_TEST(test_debounce_event_to_event_keeps_previous);
    RUN_TEST(test_debounce_degraded_and_k1);
    RUN_TEST(test_status_period);
    RUN_TEST(test_restart_keeps_baselines);
    RUN_TEST(test_gap_is_bounded);
    RUN_TEST(test_missing_superframes_without_advance);
    RUN_TEST(test_flush_and_advance);
    RUN_TEST(test_rejected_reports);
    RUN_TEST(test_listener_forwards);
    RUN_TEST(test_netsim_integration);
    RUN_TEST(test_trace_source);
    RUN_TEST(test_sample_callback_read_only_and_correct);
    RUN_TEST(test_sample_age_after_restart_and_never);
    RUN_TEST(test_json_number_format);
    RUN_TEST(test_json_string_escaping);
    RUN_TEST(test_json_meta_line);
    RUN_TEST(test_json_event_lines);
    RUN_TEST(test_json_sample_and_restart_lines);
    RUN_TEST(test_json_node_line);
    RUN_TEST(test_json_overflow_and_worst_case);
    RUN_TEST(test_json_sink_counts_and_drops);
    RUN_TEST(test_rule_f_or_of_separately_smoothed_flags);
    RUN_TEST(test_rule_f_threshold_gate_and_ready_edges);
    RUN_TEST(test_rule_f_degraded_dead_alive_and_restart);
    RUN_TEST(test_report_pkt_optional_fields_sanitised);
    RUN_TEST(test_damage_begin_end_and_pooling);
    RUN_TEST(test_damage_missing_slave_and_no_report_hold);
    RUN_TEST(test_damage_survives_stream_restart_and_status);
    RUN_TEST(test_json_damage_line_and_sink_order);
    RUN_TEST(test_json_schema3_worst_case_widths);
    RUN_TEST(test_netsim_rule_f_end_to_end);
    RUN_TEST(test_parity_vectors);
    return UNITY_END();
}
