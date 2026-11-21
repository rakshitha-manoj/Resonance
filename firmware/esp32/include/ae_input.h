#pragma once
/* Autoencoder input stage (no model weights here): the per-window base features and the per-node commissioning
   normaliser. Arduino-free, no heap, C and C++11.
   base20 = [16 az log band-powers (spec_bands), az_std, az_kurt, gx_std, gx_kurt]; all but gx_kurt come from the
   features_ex() call the node already makes (gx_kurt is one extra O(128) pass). */
#include <stdint.h>
#include <math.h>
#include "features.h"
#include "spectrum.h"

#define AE_NBASE 20

/* base20 from the outputs of features_ex(): f = its 5 features, az_p = its az power spectrum. */
static inline void ae_base_ex(const float *f, const float *az_p, float gx_kurt, float *b) {
    spec_bands(az_p, b);
    b[16] = f[0];
    b[17] = f[1];
    b[18] = f[3];
    b[19] = gx_kurt;
}

/* Standalone: base20 straight from the raw int16 windows. */
static inline void ae_base(const int16_t *az, const int16_t *gx, float *b) {
    float p[WIN_N / 2], sa, ka, sg, kg;
    spec_power(az, p);
    spec_bands(p, b);
    feat_std_kurt(az, &sa, &ka);
    feat_std_kurt(gx, &sg, &kg);
    b[16] = sa;
    b[17] = ka;
    b[18] = sg;
    b[19] = kg;
}
