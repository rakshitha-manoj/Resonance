/* Python bridge: the firmware's own front-end compiled as a shared library (see models/training/frontend.py).
   gcc -O2 -ffp-contract=off -shared -I../include frontend_lib.c */
#include "features.h"
#include "spectrum.h"

#ifdef _WIN32
#define FRONT_API __declspec(dllexport)
#else
#define FRONT_API
#endif

/* n windows of 128 int16 (az, gx contiguous). Any output pointer may be NULL (f5 needs gx; gx may be NULL
   when f5 is NULL). f5: n*5 (features()), bands16: n*16, zoom181: n*181. */
FRONT_API void front_batch(int n, const int16_t *az, const int16_t *gx, float *f5, float *bands16, float *zoom181) {
    for (int w = 0; w < n; w++) {
        const int16_t *a = az + (long)w * WIN_N;
        float p[WIN_N / 2];
        if (f5 && gx) {
            features_ex(a, gx + (long)w * WIN_N, f5 + (long)w * 5, p);
        } else if (bands16) {
            spec_power(a, p);
        }
        if (bands16) spec_bands(p, bands16 + (long)w * SPEC_BANDS);
        if (zoom181) spec_zoom(a, zoom181 + (long)w * SPEC_ZOOM_N);
    }
}

FRONT_API void front_peak(int n, const float *zoom181, float *hz) {
    for (int w = 0; w < n; w++)
        hz[w] = spec_peak(zoom181 + (long)w * SPEC_ZOOM_N, SPEC_ZOOM_N, SPEC_ZOOM_F0, SPEC_ZOOM_DF);
}

FRONT_API int front_zoom_n(void) { return SPEC_ZOOM_N; }
FRONT_API int front_zoom_f0_mhz(void) { return SPEC_ZOOM_F0_MHZ; }
FRONT_API int front_zoom_df_mhz(void) { return SPEC_ZOOM_DF_MHZ; }
