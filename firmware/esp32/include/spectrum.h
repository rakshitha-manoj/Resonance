#pragma once
/* Shared spectral front-end, per 128-sample window (100 Hz). Arduino-free, no heap, C and C++11.
   spec_power / spec_bands: coarse spectrum (64 DFT bins -> 16 log bands) for the autoencoder.
   spec_zoom / spec_peak: Hann-windowed Goertzel spectrum 3..12 Hz in 0.05 Hz steps for the f1 tracker.
   Tables come from tools/gen_spectrum.py (hex floats, identical on host and ESP32). */
#include <stdint.h>
#include <math.h>
#include "features.h"
#include "spectrum_tables.h"

#define SPEC_FS 100.0f
#define SPEC_ZOOM_F0 3.0f
#define SPEC_ZOOM_DF 0.05f
#define SPEC_ZOOM_N 181
#define SPEC_BANDS 16

/* p[k-1] = |DFT_k|^2 of the mean-removed window, k = 1..64; argmax(p)+1 times BIN_HZ == feat_domfreq. */
static inline void spec_power(const int16_t *x, float *p) {
    feat_dft_power(x, p);
}

static inline void spec_bands(const float *p, float *out) {
    for (int b = 0; b < SPEC_BANDS; b++)
        out[b] = logf(p[4 * b] + p[4 * b + 1] + p[4 * b + 2] + p[4 * b + 3] + 1.0f);
}

/* Goertzel power of the mean-removed, Hann-windowed az at f_j = F0 + j*DF. */
static inline void spec_zoom(const int16_t *az, float *z) {
    int32_t sum = 0;
    for (int i = 0; i < WIN_N; i++) sum += az[i];
    float mean = (float)sum / WIN_N;
    float d[WIN_N];
    for (int i = 0; i < WIN_N; i++) d[i] = ((float)az[i] - mean) * spec_hann[i];
    for (int j = 0; j < SPEC_ZOOM_N; j++) {
        float c = spec_zoom_coeff[j];
        float s1 = 0.0f, s2 = 0.0f;
        for (int n = 0; n < WIN_N; n++) {
            float s0 = d[n] + c * s1 - s2;
            s2 = s1;
            s1 = s0;
        }
        z[j] = s1 * s1 + s2 * s2 - c * s1 * s2;
    }
}

/* Peak frequency (Hz): argmax + parabolic interpolation in log power over the 3 points around it.
   Edges, or a non-positive neighbour/peak, or a non-concave triple: the grid frequency. */
static inline float spec_peak(const float *z, int n, float f0, float df) {
    int im = 0;
    for (int i = 1; i < n; i++)
        if (z[i] > z[im]) im = i;
    float f = f0 + im * df;
    if (im == 0 || im == n - 1) return f;
    if (!(z[im - 1] > 0.0f && z[im] > 0.0f && z[im + 1] > 0.0f)) return f;
    float a = logf(z[im - 1]), b = logf(z[im]), c = logf(z[im + 1]);
    float den = a - 2.0f * b + c;
    if (!(den < 0.0f)) return f;
    float delta = 0.5f * (a - c) / den;
    if (delta > 0.5f) delta = 0.5f;
    if (delta < -0.5f) delta = -0.5f;
    return f + delta * df;
}
