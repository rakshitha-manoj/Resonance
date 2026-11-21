#pragma once
/* Slave-side first-natural-frequency tracker. Arduino-free, no heap, C and C++11, deterministic (same float
   arithmetic on host and ESP32 with -ffp-contract=off).

   Feed it every window's spec_zoom spectrum (SPEC_ZOOM_N = 181 values, 3.0..12.0 Hz step 0.05). It keeps the last M
   spectra in a ring; every window the mean spectrum is RECOMPUTED from the ring (181 * M adds, no running sum, so no
   float drift) and f1_est = spec_peak(mean) once M windows are in. The estimate is VALID unless fewer than M windows
   are in, the mean has no finite positive peak, or the peak sits on a grid edge (index 0 or 180: the real peak is
   outside 3..12 Hz or the spectrum is flat/garbage).
   Commissioning: f1_0 = median (even count: mean of the two middle values) of the first C VALID estimates, one taken
   per window after the first M; commissioned once C are collected. drop = (f1_0 - f1_est) / f1_0 (positive = f1 fell).
   Detection is always relative to the node's own commissioned f1_0 (per-building bias of the estimator cancels).
   M, C: compile-time defaults F1T_M / F1T_C (set from the evaluation, models/selected/validation/freq_tracking.md);
   f1t_init(t, m, c) takes any m <= F1T_RING_MAX, c <= F1T_C_MAX (the host bridge and tests build with larger maxima).
   RAM: F1T_RING_MAX * 181 * 4 B for the ring (16: 11.6 KB, 32: 23.2 KB). */
#include <stdint.h>
#include <math.h>
#include "spectrum.h"

#ifndef F1T_M
#define F1T_M 16
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
#define F1T_NZ SPEC_ZOOM_N

typedef struct {
    float ring[F1T_RING_MAX][F1T_NZ];
    float comm[F1T_C_MAX];  /* first C valid estimates (sorted in place when commissioning completes) */
    int m, c;               /* m == 0: unusable (bad init), update() then never produces an estimate */
    int head;               /* ring slot written next */
    int count;              /* spectra in the ring (<= m) */
    int n_comm;             /* valid estimates collected so far (<= c) */
    int commissioned;
    int valid;              /* current estimate valid */
    float f1_est;           /* NAN while not valid */
    float f1_0;             /* NAN until commissioned */
} f1t_t;

static inline int f1t_finite(float v) { return v == v && v - v == 0.0f; }

/* 0 = ok, -1 = m or c out of range (the tracker stays inert). */
static inline int f1t_init(f1t_t *t, int m, int c) {
    t->head = t->count = t->n_comm = t->commissioned = t->valid = 0;
    t->f1_est = NAN;
    t->f1_0 = NAN;
    if (m < 1 || m > F1T_RING_MAX || c < 1 || c > F1T_C_MAX) {
        t->m = t->c = 0;
        return -1;
    }
    t->m = m;
    t->c = c;
    return 0;
}

static inline int f1t_init_default(f1t_t *t) { return f1t_init(t, F1T_M, F1T_C); }

/* One window. Returns 1 if the estimate is valid after this window. */
static inline int f1t_update(f1t_t *t, const float *z) {
    if (t->m == 0) return 0;
    for (int j = 0; j < F1T_NZ; j++) t->ring[t->head][j] = z[j];
    t->head = (t->head + 1) % t->m;
    if (t->count < t->m) t->count++;
    t->valid = 0;
    t->f1_est = NAN;
    if (t->count < t->m) return 0;
    float mean[F1T_NZ];
    for (int j = 0; j < F1T_NZ; j++) {
        float s = 0.0f;
        for (int k = 0; k < t->m; k++) s += t->ring[k][j];
        mean[j] = s / (float)t->m;
    }
    int im = -1;  /* same argmax rule as spec_peak: NaN skipped, first maximum wins */
    for (int j = 0; j < F1T_NZ; j++)
        if (mean[j] == mean[j] && (im < 0 || mean[j] > mean[im])) im = j;
    if (im <= 0 || im >= F1T_NZ - 1) return 0;                 /* none, or grid edge */
    if (!(mean[im] > 0.0f) || !f1t_finite(mean[im])) return 0;  /* flat zero or overflow */
    t->f1_est = spec_peak(mean, F1T_NZ, SPEC_ZOOM_F0, SPEC_ZOOM_DF);
    t->valid = 1;
    if (!t->commissioned) {
        t->comm[t->n_comm++] = t->f1_est;
        if (t->n_comm == t->c) {
            for (int i = 1; i < t->c; i++) {  /* insertion sort */
                float v = t->comm[i];
                int k = i - 1;
                while (k >= 0 && t->comm[k] > v) {
                    t->comm[k + 1] = t->comm[k];
                    k--;
                }
                t->comm[k + 1] = v;
            }
            t->f1_0 = (t->c & 1) ? t->comm[t->c / 2] : 0.5f * (t->comm[t->c / 2 - 1] + t->comm[t->c / 2]);
            t->commissioned = 1;
        }
    }
    return 1;
}

/* Relative f1 drop of the current window; NAN unless commissioned and the estimate is valid. */
static inline float f1t_drop(const f1t_t *t) {
    if (!t->commissioned || !t->valid) return NAN;
    return (t->f1_0 - t->f1_est) / t->f1_0;
}
