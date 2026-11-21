#pragma once
/* Slave-side first-natural-frequency tracker. Arduino-free, no heap, C and C++11, deterministic (same float
   arithmetic on host and ESP32 with -ffp-contract=off).

   MEMORY: f1t_t is about 23.4 KB at the defaults (M = 32: ring 32 * 181 * 4 B). Declare it STATIC or GLOBAL, never
   as a local of loop()/a task (the Arduino loop stack is 8 KB).

   Feed it every window's spec_zoom spectrum (SPEC_ZOOM_N = 181 values, 3.0..12.0 Hz step 0.05). It keeps the last M
   spectra in a ring; every window the mean spectrum is RECOMPUTED from the ring (181 * M adds, no running sum, so no
   float drift) and the raw estimate is spec_peak(mean) once M windows are in. The raw estimate is VALID unless fewer
   than M windows are in, the mean has no finite positive peak, or the peak sits on a grid edge (index 0 or 180).

   Commissioning (relative detection: the estimator has a per-building bias of about +-0.1 Hz, so f1 is only ever
   compared with the node's OWN f1_0): the last C valid raw estimates are kept; once C are in, f1_0 = their median
   (even count: mean of the two middle values) PROVIDED they are unimodal: (p90 - p10) / median <= spread_max
   (F1T_SPREAD_MAX = 0.10; healthy scatter is about 2-3%). If not (e.g. the peak jumps between mode 1 and mode 2 of a
   low-f1 building seen from a low floor), commissioning is NOT done and the window slides on with every new valid
   estimate until the last C are consistent. Nothing is reported as drop meanwhile.
   After commissioning an estimate outside f1_0 * (1 +- F1T_BAND) (0.20; the damage of interest moves f1 by < 5%) is
   treated as INVALID (a mode jump, not damage): f1_est = NAN, valid = 0, counted in n_rejected.
   drop = (f1_0 - f1_est) / f1_0 (positive = f1 fell); NAN unless commissioned and valid.
   LIMITS: if the real f1 is outside 3..12 Hz (or the first mode is invisible at the chosen floor) the tracker never
   commissions: f1t_not_commissioned() turns true after M + F1T_PATIENCE * C windows; surface it (the master must not
   count such a slave as healthy). A node that commissioned onto a higher mode keeps tracking that mode (consistent,
   but not the first natural frequency); if the first mode reappears it is out of band and silently invalid.
   M, C: compile-time defaults F1T_M / F1T_C = the selection in models/selected/validation/freq_tracking.json
   (test_shm fails if they differ); f1t_init(t, m, c) takes any m <= F1T_RING_MAX, c <= F1T_C_MAX (the host bridge
   and the tests build with larger maxima). */
#include <stdint.h>
#include <math.h>
#include "spectrum.h"

#ifndef F1T_M
#define F1T_M 32
#endif
#ifndef F1T_C
#define F1T_C 64
#endif
#ifndef F1T_RING_MAX
#define F1T_RING_MAX F1T_M
#endif
#ifndef F1T_C_MAX
#define F1T_C_MAX F1T_C
#endif
#ifndef F1T_SPREAD_MAX
#define F1T_SPREAD_MAX 0.10f
#endif
#ifndef F1T_BAND
#define F1T_BAND 0.20f
#endif
#ifndef F1T_PATIENCE
#define F1T_PATIENCE 4
#endif
#define F1T_NZ SPEC_ZOOM_N
typedef char f1t_chk_m[(F1T_M <= F1T_RING_MAX && F1T_C <= F1T_C_MAX) ? 1 : -1];

typedef struct {
    float ring[F1T_RING_MAX][F1T_NZ];
    float comm[F1T_C_MAX];  /* last c valid raw estimates (circular) */
    int m, c;               /* m == 0: unusable (bad init), update() then never produces an estimate */
    float spread_max, band;
    int head;               /* ring slot written next */
    int count;              /* spectra in the ring (<= m) */
    int comm_head, n_comm;  /* commissioning buffer */
    int commissioned;
    int n_windows;          /* windows fed (saturating) */
    int n_rejected;         /* estimates rejected by the band gate */
    int raw_valid;          /* the raw estimate of the last window is valid */
    int valid;              /* the reported estimate is valid (raw valid and, once commissioned, inside the band) */
    float f1_raw;           /* NAN while not raw-valid */
    float f1_est;           /* NAN while not valid */
    float f1_0;             /* NAN until commissioned */
} f1t_t;

static inline int f1t_finite(float v) { return v == v && v - v == 0.0f; }

/* 0 = ok, -1 = m or c out of range (the tracker stays inert). */
static inline int f1t_init(f1t_t *t, int m, int c) {
    t->head = t->count = t->comm_head = t->n_comm = t->commissioned = t->valid = t->raw_valid = 0;
    t->n_windows = t->n_rejected = 0;
    t->f1_raw = t->f1_est = t->f1_0 = NAN;
    t->spread_max = F1T_SPREAD_MAX;
    t->band = F1T_BAND;
    if (m < 1 || m > F1T_RING_MAX || c < 1 || c > F1T_C_MAX) {
        t->m = t->c = 0;
        return -1;
    }
    t->m = m;
    t->c = c;
    return 0;
}

static inline int f1t_init_default(f1t_t *t) { return f1t_init(t, F1T_M, F1T_C); }

/* Override the commissioning consistency bound and the post-commissioning band (tests, evaluation). */
static inline void f1t_set_limits(f1t_t *t, float spread_max, float band) {
    t->spread_max = spread_max;
    t->band = band;
}

/* Raw estimate of one window -> commissioning and gating. f1t_update calls this; the evaluation bridge calls it on
   stored estimates. Returns 1 if the reported estimate is valid. */
static inline int f1t_feed(f1t_t *t, int raw_valid, float est) {
    if (t->m == 0) return 0;
    if (t->n_windows < 0x3fffffff) t->n_windows++;
    t->raw_valid = raw_valid;
    t->f1_raw = raw_valid ? est : NAN;
    t->valid = 0;
    t->f1_est = NAN;
    if (!raw_valid) return 0;
    if (!t->commissioned) {
        t->comm[t->comm_head] = est;
        t->comm_head = (t->comm_head + 1) % t->c;
        if (t->n_comm < t->c) t->n_comm++;
        if (t->n_comm == t->c) {
            float s[F1T_C_MAX];
            for (int i = 0; i < t->c; i++) s[i] = t->comm[i];
            for (int i = 1; i < t->c; i++) {  /* insertion sort */
                float v = s[i];
                int k = i - 1;
                while (k >= 0 && s[k] > v) {
                    s[k + 1] = s[k];
                    k--;
                }
                s[k + 1] = v;
            }
            float med = (t->c & 1) ? s[t->c / 2] : 0.5f * (s[t->c / 2 - 1] + s[t->c / 2]);
            float spread = (s[t->c - 1 - t->c / 10] - s[t->c / 10]) / med;
            if (spread <= t->spread_max) {
                t->f1_0 = med;
                t->commissioned = 1;
            }
        }
    }
    if (t->commissioned && fabsf(est - t->f1_0) > t->band * t->f1_0) {
        if (t->n_rejected < 0x3fffffff) t->n_rejected++;
        return 0;
    }
    t->f1_est = est;
    t->valid = 1;
    return 1;
}

/* One window. Returns 1 if the reported estimate is valid after this window. */
static inline int f1t_update(f1t_t *t, const float *z) {
    if (t->m == 0) return 0;
    for (int j = 0; j < F1T_NZ; j++) t->ring[t->head][j] = z[j];
    t->head = (t->head + 1) % t->m;
    if (t->count < t->m) t->count++;
    if (t->count < t->m) return f1t_feed(t, 0, NAN);
    float mean[F1T_NZ];
    for (int j = 0; j < F1T_NZ; j++) {
        float s = 0.0f;
        for (int k = 0; k < t->m; k++) s += t->ring[k][j];
        mean[j] = s / (float)t->m;
    }
    int im = -1;  /* same argmax rule as spec_peak: NaN skipped, first maximum wins */
    for (int j = 0; j < F1T_NZ; j++)
        if (mean[j] == mean[j] && (im < 0 || mean[j] > mean[im])) im = j;
    if (im <= 0 || im >= F1T_NZ - 1) return f1t_feed(t, 0, NAN);                 /* none, or grid edge */
    if (!(mean[im] > 0.0f) || !f1t_finite(mean[im])) return f1t_feed(t, 0, NAN);  /* flat zero or overflow */
    return f1t_feed(t, 1, spec_peak(mean, F1T_NZ, SPEC_ZOOM_F0, SPEC_ZOOM_DF));
}

/* Relative f1 drop of the current window; NAN unless commissioned and the estimate is valid. */
static inline float f1t_drop(const f1t_t *t) {
    if (!t->commissioned || !t->valid) return NAN;
    return (t->f1_0 - t->f1_est) / t->f1_0;
}

/* 1 when commissioning is overdue (never finished after M + F1T_PATIENCE * C windows): f1 outside 3..12 Hz, first
   mode not visible, or an unstable (multi-modal) estimate. Report it; do not count the node as healthy. */
static inline int f1t_not_commissioned(const f1t_t *t) {
    return !t->commissioned && t->m > 0 && t->n_windows >= t->m + F1T_PATIENCE * t->c;
}
