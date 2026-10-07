#pragma once
#include <stdint.h>
#include <math.h>
#include "model.h"

#define WIN_N 128
#define BIN_HZ 0.78125f

static inline void feat_std_kurt(const int16_t *x, float *std, float *kurt) {
    int32_t sum = 0;
    for (int i = 0; i < WIN_N; i++) sum += x[i];
    float mean = (float)sum / WIN_N;
    float s2 = 0.0f, s4 = 0.0f;
    for (int i = 0; i < WIN_N; i++) {
        float d = (float)x[i] - mean;
        float d2 = d * d;
        s2 += d2;
        s4 += d2 * d2;
    }
    float m2 = s2 / WIN_N;
    float m4 = s4 / WIN_N;
    *std = sqrtf(m2);
    *kurt = m2 > 0.0f ? m4 / (m2 * m2) - 3.0f : 0.0f;
}

static inline float feat_std(const int16_t *x) {
    int32_t sum = 0;
    for (int i = 0; i < WIN_N; i++) sum += x[i];
    float mean = (float)sum / WIN_N;
    float s2 = 0.0f;
    for (int i = 0; i < WIN_N; i++) {
        float d = (float)x[i] - mean;
        s2 += d * d;
    }
    return sqrtf(s2 / WIN_N);
}

static inline float feat_domfreq(const int16_t *x) {
    int32_t sum = 0;
    for (int i = 0; i < WIN_N; i++) sum += x[i];
    float mean = (float)sum / WIN_N;
    float d[WIN_N];
    for (int i = 0; i < WIN_N; i++) d[i] = (float)x[i] - mean;
    float best = -1.0f;
    int bk = 1;
    for (int k = 1; k <= WIN_N / 2; k++) {
        float re = 0.0f, im = 0.0f;
        for (int n = 0; n < WIN_N; n++) {
            int j = (k * n) & (WIN_N - 1);
            re += d[n] * tw_cos[j];
            im += d[n] * tw_sin[j];
        }
        float p = re * re + im * im;
        if (p > best) {
            best = p;
            bk = k;
        }
    }
    return bk * BIN_HZ;
}

static inline void features(const int16_t *az, const int16_t *gx, float *f) {
    feat_std_kurt(az, &f[0], &f[1]);
    f[2] = feat_domfreq(az);
    f[3] = feat_std(gx);
    f[4] = feat_domfreq(gx);
}

static inline int classify(const float *f) {
    return predict(f[0], f[1], f[2], f[3], f[4]);
}
