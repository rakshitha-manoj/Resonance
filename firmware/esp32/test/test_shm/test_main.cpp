/* Host tests for include/f1track.h (slave f1 tracker) and include/damage.h (master DAMAGE detector). */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <vector>
#include <unity.h>
#define F1T_RING_MAX 32
#define F1T_C_MAX 128
#include "f1track.h"
#include "damage.h"

void setUp(void) {}
void tearDown(void) {}

static f1t_t T;  /* ~23 KB: static, not on the test stack */

/* init with the commissioning consistency / band gates opened: the older tests use widely spread synthetic estimates */
static int initR(f1t_t *t, int m, int c) {
    int r = f1t_init(t, m, c);
    f1t_set_limits(t, 1e9f, 1e9f);
    return r;
}

/* Spectrum with a delta-like peak at grid index i (symmetric neighbours => parabolic offset 0 => exactly f0 + i*df). */
static void sym_peak(float *z, int i) {
    for (int j = 0; j < F1T_NZ; j++) z[j] = 0.01f;
    z[i] = 2.0f;
    if (i > 0) z[i - 1] = 1.0f;
    if (i < F1T_NZ - 1) z[i + 1] = 1.0f;
}

static float grid_f(int i) { return SPEC_ZOOM_F0 + i * SPEC_ZOOM_DF; }

/* double reference of spec_peak on a double spectrum */
static double ref_peak(const std::vector<double> &z) {
    int im = 0;
    for (int i = 1; i < (int)z.size(); i++)
        if (z[i] > z[im]) im = i;
    double f = 3.0 + im * 0.05;
    if (im == 0 || im == (int)z.size() - 1) return f;
    double a = log(z[im - 1]), b = log(z[im]), c = log(z[im + 1]);
    double den = a - 2 * b + c;
    if (!(den < 0)) return f;
    double d = 0.5 * (a - c) / den;
    if (d > 0.5) d = 0.5;
    if (d < -0.5) d = -0.5;
    return f + d * 0.05;
}

static uint32_t rs = 12345;
static double rnd() {  /* deterministic xorshift, 0..1 */
    rs ^= rs << 13;
    rs ^= rs >> 17;
    rs ^= rs << 5;
    return (rs >> 8) / 16777216.0;
}

/* noisy resonance at f_hz: smooth peak + multiplicative random scatter, like an averaged-periodogram input */
static void synth(float *z, double f_hz) {
    for (int j = 0; j < F1T_NZ; j++) {
        double f = 3.0 + 0.05 * j;
        double base = 1.0e4 / (1.0 + pow((f - f_hz) / 0.4, 2.0)) + 50.0;
        z[j] = (float)(base * (0.5 + rnd()));
    }
}

void test_ring_average_vs_double(void) {
    for (int M : {16, 32}) {
        TEST_ASSERT_EQUAL_INT(0, initR(&T, M, 8));
        std::vector<std::vector<float> > hist;
        double max_err = 0;
        int checked = 0;
        for (int w = 0; w < 200; w++) {
            std::vector<float> z(F1T_NZ);
            synth(z.data(), 6.0 + 0.5 * sin(w * 0.05));
            hist.push_back(z);
            int v = f1t_update(&T, z.data());
            if (w < M - 1) {
                TEST_ASSERT_EQUAL_INT(0, v);  /* not enough windows */
                TEST_ASSERT_TRUE(isnan(T.f1_est));
                continue;
            }
            TEST_ASSERT_EQUAL_INT(1, v);
            std::vector<double> mean(F1T_NZ, 0.0);
            for (int k = 0; k < M; k++)
                for (int j = 0; j < F1T_NZ; j++) mean[j] += (double)hist[w - k][j];
            for (int j = 0; j < F1T_NZ; j++) mean[j] /= M;
            double e = fabs((double)T.f1_est - ref_peak(mean));
            if (e > max_err) max_err = e;
            checked++;
        }
        printf("M=%d: %d estimates, max |float - double reference| = %.3g Hz\n", M, checked, max_err);
        TEST_ASSERT_TRUE(max_err < 2e-3);
    }
}

void test_no_drift_over_many_windows(void) {
    /* a constant spectrum fed 200000 times must give the SAME estimate as after M windows (no running-sum drift) */
    float z[F1T_NZ];
    synth(z, 7.3);
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 16, 4));
    float first = NAN;
    for (int w = 0; w < 200000; w++) {
        f1t_update(&T, z);
        if (w == 15) first = T.f1_est;
    }
    TEST_ASSERT_TRUE(first == first);
    TEST_ASSERT_TRUE(T.f1_est == first);  /* bit-identical */
    TEST_ASSERT_EQUAL_INT(1, T.valid);
}

void test_edge_invalid_and_warmup(void) {
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 2));
    sym_peak(z, 0);
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    TEST_ASSERT_EQUAL_INT(0, T.valid);
    TEST_ASSERT_TRUE(isnan(f1t_drop(&T)));
    sym_peak(z, F1T_NZ - 1);
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    sym_peak(z, 1);  /* next to the edge is fine */
    TEST_ASSERT_EQUAL_INT(1, f1t_update(&T, z));
    sym_peak(z, F1T_NZ - 2);
    TEST_ASSERT_EQUAL_INT(1, f1t_update(&T, z));
    TEST_ASSERT_EQUAL_INT(2, T.n_comm);  /* the two edge windows were not counted */
    /* flat zero spectrum: peak index 0 => invalid */
    memset(z, 0, sizeof z);
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    /* all NaN => invalid, no crash */
    for (int j = 0; j < F1T_NZ; j++) z[j] = NAN;
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    /* an edge spectrum inside an averaging window invalidates the mean only if the mean peak is at the edge */
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 4, 2));
    for (int w = 0; w < 3; w++) {
        sym_peak(z, 50);
        TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));  /* warm-up: M-1 windows never valid */
    }
    sym_peak(z, 50);
    TEST_ASSERT_EQUAL_INT(1, f1t_update(&T, z));
    TEST_ASSERT_TRUE(fabsf(T.f1_est - grid_f(50)) < 1e-5f);
}

void test_median_commissioning(void) {
    float z[F1T_NZ];
    /* odd count C=5: estimates 6.0, 4.0, 8.0, 5.0, 7.0 (grid 60,20,100,40,80) interleaved with invalid edge windows */
    int idx5[5] = {60, 20, 100, 40, 80};
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 5));
    for (int i = 0; i < 5; i++) {
        sym_peak(z, 0);
        f1t_update(&T, z);  /* invalid: ignored */
        TEST_ASSERT_EQUAL_INT(0, T.commissioned);
        sym_peak(z, idx5[i]);
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - 6.0f) < 1e-5f);
    /* f1_0 is fixed from now on */
    sym_peak(z, 100);
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - 6.0f) < 1e-5f);
    /* even count C=4: 6, 4, 8, 5 -> sorted 4 5 6 8 -> (5+6)/2 = 5.5 */
    int idx4[4] = {60, 20, 100, 40};
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 4));
    for (int i = 0; i < 4; i++) {
        TEST_ASSERT_EQUAL_INT(0, T.commissioned);
        sym_peak(z, idx4[i]);
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - 5.5f) < 1e-5f);
    /* C=2 and C=1 */
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 2));
    sym_peak(z, 20);
    f1t_update(&T, z);
    sym_peak(z, 60);
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - 5.0f) < 1e-5f);
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 1));
    sym_peak(z, 80);
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - 7.0f) < 1e-5f);
    /* commissioning starts only after M windows: M=3, C=1: first estimate at the 3rd window */
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 3, 1));
    sym_peak(z, 80);
    f1t_update(&T, z);
    f1t_update(&T, z);
    TEST_ASSERT_EQUAL_INT(0, T.commissioned);
    f1t_update(&T, z);
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
}

void test_drop_sign_and_validity(void) {
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 1, 1));
    sym_peak(z, 60);  /* 6.0 Hz */
    TEST_ASSERT_TRUE(isnan(f1t_drop(&T)));  /* not commissioned */
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(fabsf(f1t_drop(&T)) < 1e-6f);
    sym_peak(z, 50);  /* 5.5 Hz: f1 fell => positive drop 0.5/6 */
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(fabsf(f1t_drop(&T) - 0.5f / 6.0f) < 1e-5f);
    sym_peak(z, 70);  /* 6.5 Hz: f1 rose => negative */
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(f1t_drop(&T) < 0.0f);
    TEST_ASSERT_TRUE(fabsf(f1t_drop(&T) + 0.5f / 6.0f) < 1e-5f);
    sym_peak(z, 0);  /* invalid window: no drop */
    f1t_update(&T, z);
    TEST_ASSERT_TRUE(isnan(f1t_drop(&T)));
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);  /* commissioning survives an invalid window */
}

void test_init_limits(void) {
    TEST_ASSERT_EQUAL_INT(-1, initR(&T, 0, 4));
    TEST_ASSERT_EQUAL_INT(-1, initR(&T, 33, 4));
    TEST_ASSERT_EQUAL_INT(-1, initR(&T, 16, 0));
    TEST_ASSERT_EQUAL_INT(-1, initR(&T, 16, 129));
    float z[F1T_NZ];
    sym_peak(z, 60);
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));  /* inert after a bad init */
    TEST_ASSERT_EQUAL_INT(0, initR(&T, 32, 128));
    TEST_ASSERT_EQUAL_INT(0, f1t_init_default(&T));
    TEST_ASSERT_EQUAL_INT(F1T_M, T.m);
    TEST_ASSERT_EQUAL_INT(F1T_C, T.c);
}

/* ------------------------------------------------------------------ detector */
static int step1(dmg_t *d, float drop) {
    uint8_t ok = 1;
    return dmg_update(d, 1, &ok, &drop);
}

void test_dmg_begin_after_exactly_p(void) {
    dmg_t d;
    dmg_init(&d, 0.02f, 5);
    for (int i = 0; i < 4; i++) {
        TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, step1(&d, 0.03f));
        TEST_ASSERT_EQUAL_INT(0, d.active);
    }
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.03f));  /* the 5th */
    TEST_ASSERT_EQUAL_INT(1, d.active);
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, step1(&d, 0.03f));  /* no second begin */
    /* exactly delta counts (>=); an interruption restarts the count */
    dmg_init(&d, 0.02f, 3);
    step1(&d, 0.02f);
    step1(&d, 0.02f);
    step1(&d, 0.0199f);
    TEST_ASSERT_EQUAL_INT(0, d.active);
    step1(&d, 0.02f);
    step1(&d, 0.02f);
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.02f));
    /* P = 1 and P < 1 (treated as 1) */
    dmg_init(&d, 0.02f, 1);
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.5f));
    dmg_init(&d, 0.02f, 0);
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.5f));
}

void test_dmg_hysteresis_end(void) {
    dmg_t d;
    dmg_init(&d, 0.02f, 3);
    for (int i = 0; i < 3; i++) step1(&d, 0.05f);
    TEST_ASSERT_EQUAL_INT(1, d.active);
    /* between delta/2 and delta: stays in DAMAGE and resets the down count */
    for (int i = 0; i < 10; i++) TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, step1(&d, 0.015f));
    TEST_ASSERT_EQUAL_INT(1, d.active);
    step1(&d, 0.005f);
    step1(&d, 0.005f);
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, step1(&d, 0.010f));  /* exactly delta/2 is NOT below: run broken */
    TEST_ASSERT_EQUAL_INT(1, d.active);
    step1(&d, 0.005f);
    step1(&d, 0.005f);
    TEST_ASSERT_EQUAL_INT(DMG_EV_END, step1(&d, 0.005f));  /* 3rd consecutive below delta/2 */
    TEST_ASSERT_EQUAL_INT(0, d.active);
    /* after the end a new begin needs P again, and the begin-run starts from zero */
    step1(&d, 0.05f);
    step1(&d, 0.05f);
    TEST_ASSERT_EQUAL_INT(0, d.active);
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.05f));
}

void test_dmg_hold_without_data(void) {
    dmg_t d;
    dmg_init(&d, 0.02f, 3);
    step1(&d, 0.05f);
    step1(&d, 0.05f);
    uint8_t ok[2] = {0, 0};
    float dr[2] = {0.9f, 0.9f};
    for (int i = 0; i < 20; i++) TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));  /* nobody usable */
    TEST_ASSERT_EQUAL_INT(0, d.n_pooled);
    TEST_ASSERT_TRUE(isnan(d.pooled));
    TEST_ASSERT_EQUAL_INT(0, d.active);
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, step1(&d, 0.05f));  /* run counter was held: 2 + 1 = P */
    /* in DAMAGE, silence does not end it */
    for (int i = 0; i < 50; i++) dmg_update(&d, 2, ok, dr);
    TEST_ASSERT_EQUAL_INT(1, d.active);
    step1(&d, 0.0f);
    step1(&d, 0.0f);
    for (int i = 0; i < 50; i++) dmg_update(&d, 2, ok, dr);
    TEST_ASSERT_EQUAL_INT(1, d.active);
    TEST_ASSERT_EQUAL_INT(DMG_EV_END, step1(&d, 0.0f));
    /* n = 0 slaves */
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 0, ok, dr));
}

void test_dmg_pooled_mean(void) {
    dmg_t d;
    dmg_init(&d, 0.02f, 1);
    uint8_t ok[2] = {1, 1};
    float dr[2] = {0.01f, 0.05f};  /* mean 0.03 >= 0.02 although slave 0 is below */
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_EQUAL_INT(2, d.n_pooled);
    TEST_ASSERT_TRUE(fabsf(d.pooled - 0.03f) < 1e-7f);
    /* one slave missing: the mean is over the other one only */
    dmg_init(&d, 0.02f, 1);
    ok[1] = 0;
    dr[0] = 0.01f;
    dr[1] = 0.90f;  /* ignored */
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_EQUAL_INT(1, d.n_pooled);
    TEST_ASSERT_TRUE(fabsf(d.pooled - 0.01f) < 1e-7f);
    ok[0] = 0;
    ok[1] = 1;
    dr[1] = 0.04f;
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_TRUE(fabsf(d.pooled - 0.04f) < 1e-7f);
    /* negative drops (f1 rose) pool normally */
    dmg_init(&d, 0.02f, 1);
    ok[0] = ok[1] = 1;
    dr[0] = -0.01f;
    dr[1] = 0.05f;
    TEST_ASSERT_EQUAL_INT(DMG_EV_BEGIN, dmg_update(&d, 2, ok, dr));  /* mean 0.02 */
}

void test_dmg_nonfinite_rejected(void) {
    dmg_t d;
    dmg_init(&d, 0.02f, 1);
    uint8_t ok[2] = {1, 1};
    float dr[2] = {NAN, 0.01f};
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_EQUAL_INT(1, d.n_pooled);
    TEST_ASSERT_TRUE(fabsf(d.pooled - 0.01f) < 1e-7f);  /* NaN slave dropped from the pool */
    dr[0] = INFINITY;
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_EQUAL_INT(1, d.n_pooled);
    dr[0] = -INFINITY;
    dr[1] = NAN;
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));  /* both rejected = no data */
    TEST_ASSERT_EQUAL_INT(0, d.n_pooled);
    TEST_ASSERT_EQUAL_INT(0, d.active);
    /* a lone +inf must not trigger DAMAGE */
    dr[0] = INFINITY;
    ok[1] = 0;
    TEST_ASSERT_EQUAL_INT(DMG_EV_NONE, dmg_update(&d, 2, ok, dr));
    TEST_ASSERT_EQUAL_INT(0, d.active);
}

void test_tracker_into_detector(void) {
    /* end to end on synthetic spectra: a slave commissions at 6.0 Hz, then f1 falls to 5.7 Hz (5%) */
    float z[F1T_NZ];
    f1t_t *t = &T;
    TEST_ASSERT_EQUAL_INT(0, initR(t, 4, 6));
    dmg_t d;
    dmg_init(&d, 0.03f, 5);
    int begin_at = -1;
    for (int w = 0; w < 60; w++) {
        sym_peak(z, w < 30 ? 60 : 54);
        f1t_update(t, z);
        uint8_t ok = t->commissioned && t->valid;
        float dr = f1t_drop(t);
        if (dmg_update(&d, 1, &ok, &dr) == DMG_EV_BEGIN) begin_at = w;
    }
    printf("DAMAGE begin at window %d (f1 fell at window 30, M=4, P=5)\n", begin_at);
    TEST_ASSERT_TRUE(begin_at >= 30 + 4 && begin_at <= 30 + 4 + 5);
}

/* ------------------------------------------------------------------ robustness gates (post-hoc fix) */
void test_mode_jump_never_damage(void) {
    /* verifier input: C windows peaked at index 170 (11.5 Hz) then index 20 (4 Hz): commissions at 11.5, the 4 Hz
       estimates are out of band = invalid, the master sees no drop and must never raise DAMAGE */
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 8));
    dmg_t d;
    dmg_init(&d, 0.02f, 3);
    int began = 0;
    for (int w = 0; w < 200; w++) {
        sym_peak(z, w < 8 ? 170 : 20);
        f1t_update(&T, z);
        uint8_t ok = T.commissioned && T.valid;
        float dr = f1t_drop(&T);
        if (dmg_update(&d, 1, &ok, &dr) == DMG_EV_BEGIN) began = 1;
    }
    TEST_ASSERT_EQUAL_INT(0, began);
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - grid_f(170)) < 1e-4f);
    TEST_ASSERT_EQUAL_INT(0, T.valid);
    TEST_ASSERT_TRUE(isnan(f1t_drop(&T)));
    TEST_ASSERT_EQUAL_INT(192, T.n_rejected);
    /* the other way round: 4 Hz commissioning, then a jump to 11.5 Hz: no negative drop of -1.9 either */
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 8));
    dmg_init(&d, 0.02f, 3);
    for (int w = 0; w < 60; w++) {
        sym_peak(z, w < 8 ? 20 : 170);
        f1t_update(&T, z);
        uint8_t ok = T.commissioned && T.valid;
        float dr = f1t_drop(&T);
        dmg_update(&d, 1, &ok, &dr);
    }
    TEST_ASSERT_EQUAL_INT(0, d.active);
    TEST_ASSERT_EQUAL_INT(0, d.n_pooled);
}

void test_multimodal_commissioning_refused(void) {
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 8));
    for (int w = 0; w < 40; w++) {  /* alternating mode 1 / mode 2 */
        sym_peak(z, (w & 1) ? 170 : 20);
        f1t_update(&T, z);
        TEST_ASSERT_EQUAL_INT(0, T.commissioned);
        TEST_ASSERT_TRUE(isnan(f1t_drop(&T)));
    }
    TEST_ASSERT_EQUAL_INT(1, f1t_not_commissioned(&T));  /* 40 >= 1 + 4 * 8 */
    /* three windows at the low mode, five at the high mode: the sorted p10/p90 still straddle the modes */
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 8));
    for (int w = 0; w < 8; w++) {
        sym_peak(z, w < 3 ? 20 : 170);
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(0, T.commissioned);
    /* the buffer slides: once the last 8 valid estimates agree, commissioning succeeds (here on the low mode) */
    for (int w = 0; w < 8; w++) {
        sym_peak(z, 20);
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
    TEST_ASSERT_TRUE(fabsf(T.f1_0 - grid_f(20)) < 1e-4f);
    TEST_ASSERT_EQUAL_INT(0, f1t_not_commissioned(&T));
    /* an unmodal-but-noisy set within the bound commissions at the median: 4 Hz +- 2% */
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 6));
    int idx[6] = {20, 21, 19, 20, 21, 19};  /* 4.0, 4.05, 3.95 Hz: spread 0.1/4 = 2.5% */
    for (int i = 0; i < 6; i++) {
        sym_peak(z, idx[i]);
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
}

void test_band_gate(void) {
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 1, 2));
    sym_peak(z, 60);  /* 6.0 Hz */
    f1t_update(&T, z);
    f1t_update(&T, z);
    TEST_ASSERT_EQUAL_INT(1, T.commissioned);
    sym_peak(z, 54);  /* 5.7 Hz = -5%: damage-sized, valid */
    TEST_ASSERT_EQUAL_INT(1, f1t_update(&T, z));
    TEST_ASSERT_TRUE(fabsf(f1t_drop(&T) - 0.05f) < 1e-4f);
    sym_peak(z, 40);  /* 5.0 Hz = -16.7%: inside the +-20% band */
    TEST_ASSERT_EQUAL_INT(1, f1t_update(&T, z));
    sym_peak(z, 36);  /* 4.8 Hz = exactly -20% is not outside; 4.75 Hz is */
    f1t_update(&T, z);
    sym_peak(z, 35);
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    TEST_ASSERT_EQUAL_INT(1, T.raw_valid);  /* the raw estimate is still there */
    TEST_ASSERT_TRUE(T.f1_raw < 5.0f);
    TEST_ASSERT_TRUE(isnan(T.f1_est));
    sym_peak(z, 90);  /* 7.5 Hz = +25% */
    TEST_ASSERT_EQUAL_INT(0, f1t_update(&T, z));
    TEST_ASSERT_TRUE(T.n_rejected >= 2);
}

void test_never_commissions_outside_range(void) {
    float z[F1T_NZ];
    TEST_ASSERT_EQUAL_INT(0, f1t_init(&T, 4, 8));
    for (int w = 0; w < 4 + 4 * 8 + 5; w++) {
        sym_peak(z, w & 1 ? 0 : F1T_NZ - 1);  /* peaks on the grid edges: f1 outside 3..12 Hz */
        f1t_update(&T, z);
    }
    TEST_ASSERT_EQUAL_INT(0, T.commissioned);
    TEST_ASSERT_EQUAL_INT(1, f1t_not_commissioned(&T));
    TEST_ASSERT_EQUAL_INT(0, T.valid);
    TEST_ASSERT_EQUAL_INT(0, f1t_not_commissioned(&T) && T.commissioned);
}

/* The header constants must be the configuration selected in models/selected/validation/freq_tracking.json. */
static double json_sel(const char *txt, const char *key) {
    const char *sel = strstr(txt, "\"selected\"");
    TEST_ASSERT_NOT_NULL(sel);
    char pat[32];
    snprintf(pat, sizeof pat, "\"%s\":", key);
    const char *p = strstr(sel, pat);
    TEST_ASSERT_NOT_NULL(p);
    return strtod(p + strlen(pat), NULL);
}

void test_chosen_constants(void) {
    FILE *fp = fopen(FREQ_JSON, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    std::vector<char> buf(1 << 22);
    size_t n = fread(buf.data(), 1, buf.size() - 1, fp);
    fclose(fp);
    buf[n] = 0;
    TEST_ASSERT_EQUAL_INT(F1T_M, (int)json_sel(buf.data(), "M"));
    TEST_ASSERT_EQUAL_INT(F1T_C, (int)json_sel(buf.data(), "C"));
    TEST_ASSERT_EQUAL_INT(DMG_P, (int)json_sel(buf.data(), "P"));
    TEST_ASSERT_TRUE(fabs(json_sel(buf.data(), "delta") - (double)DMG_DELTA) < 1e-6);
    printf("selected M=%d C=%d delta=%.4f P=%d == header defaults\n", F1T_M, F1T_C, DMG_DELTA, DMG_P);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_ring_average_vs_double);
    RUN_TEST(test_no_drift_over_many_windows);
    RUN_TEST(test_edge_invalid_and_warmup);
    RUN_TEST(test_median_commissioning);
    RUN_TEST(test_drop_sign_and_validity);
    RUN_TEST(test_init_limits);
    RUN_TEST(test_dmg_begin_after_exactly_p);
    RUN_TEST(test_dmg_hysteresis_end);
    RUN_TEST(test_dmg_hold_without_data);
    RUN_TEST(test_dmg_pooled_mean);
    RUN_TEST(test_dmg_nonfinite_rejected);
    RUN_TEST(test_tracker_into_detector);
    RUN_TEST(test_mode_jump_never_damage);
    RUN_TEST(test_multimodal_commissioning_refused);
    RUN_TEST(test_band_gate);
    RUN_TEST(test_never_commissions_outside_range);
    RUN_TEST(test_chosen_constants);
    return UNITY_END();
}
