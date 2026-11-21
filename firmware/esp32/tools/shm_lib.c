/* Python bridge for the SHM tracker and detector (models/training/shm.py): the firmware's own f1track.h and damage.h
   compiled as a shared library. Built larger than the deployed sizes so the evaluation can sweep M and C.
   gcc -O2 -ffp-contract=off -shared -I../include shm_lib.c */
#define F1T_RING_MAX 32
#define F1T_C_MAX 128
#include "f1track.h"
#include "damage.h"

#ifdef _WIN32
#define SHM_API __declspec(dllexport)
#else
#define SHM_API
#endif

/* Run one tracker (new f1t_t) over n windows of zoom spectra (n*181). Per window: est (NAN if invalid), valid, drop
   (NAN unless commissioned and valid). Final f1_0 (NAN if never commissioned) and the window index at which
   commissioning completed (-1 if never). Returns 0, or -1 for bad m / c. Any output pointer may be NULL. */
SHM_API int shm_track(int m, int c, int n, const float *z, float *est, uint8_t *valid, float *drop, float *f10, int *wcomm,
                      float *est_raw, uint8_t *valid_raw) {
    static f1t_t t;
    if (f1t_init(&t, m, c) != 0) return -1;
    int wc = -1;
    for (int w = 0; w < n; w++) {
        int was = t.commissioned;
        f1t_update(&t, z + (long)w * F1T_NZ);
        if (!was && t.commissioned) wc = w;
        if (est_raw) est_raw[w] = t.f1_raw;
        if (valid_raw) valid_raw[w] = (uint8_t)t.raw_valid;
        if (est) est[w] = t.f1_est;
        if (valid) valid[w] = (uint8_t)t.valid;
        if (drop) drop[w] = f1t_drop(&t);
    }
    if (f10) *f10 = t.f1_0;
    if (wcomm) *wcomm = wc;
    return 0;
}

/* Commissioning + gating only, on stored RAW estimates (what the cache holds): the same f1t_feed the tracker calls.
   spread/band < 0 keep the firmware defaults. Outputs as shm_track. */
SHM_API int shm_gate(int c, float spread, float band, int n, const float *est_raw, const uint8_t *valid_raw, float *est,
                     uint8_t *valid, float *drop, float *f10, int *wcomm) {
    static f1t_t t;
    if (f1t_init(&t, 1, c) != 0) return -1;
    if (spread >= 0.0f) t.spread_max = spread;
    if (band >= 0.0f) t.band = band;
    int wc = -1;
    for (int w = 0; w < n; w++) {
        int was = t.commissioned;
        f1t_feed(&t, valid_raw[w], est_raw[w]);
        if (!was && t.commissioned) wc = w;
        if (est) est[w] = t.f1_est;
        if (valid) valid[w] = (uint8_t)t.valid;
        if (drop) drop[w] = f1t_drop(&t);
    }
    if (f10) *f10 = t.f1_0;
    if (wcomm) *wcomm = wc;
    return 0;
}

/* nseq sequences of n superframes with ns slaves each: drop (nseq, n, ns) float, ok (nseq, n, ns) uint8
   (1 = usable). For each sequence: number of DAMAGE begins, index of the first begin at or after `start` (-1 none),
   whether DAMAGE was active when superframe `start` began, and optionally the state after every superframe
   (nseq, n) uint8. The detector starts at superframe 0 of each sequence. */
SHM_API void shm_detect(float delta, int p, int nseq, int n, int ns, const float *drop, const uint8_t *ok, int start,
                        int32_t *n_begin, int32_t *first_begin, int32_t *active_at_start, uint8_t *state) {
    for (int s = 0; s < nseq; s++) {
        dmg_t d;
        dmg_init(&d, delta, p);
        int nb = 0, fb = -1, act = 0;
        for (int w = 0; w < n; w++) {
            if (w == start) act = d.active;
            long o = ((long)s * n + w) * ns;
            int ev = dmg_update(&d, ns, ok + o, drop + o);
            if (ev == DMG_EV_BEGIN) {
                nb++;
                if (w >= start && fb < 0) fb = w;
            }
            if (state) state[(long)s * n + w] = (uint8_t)d.active;
        }
        n_begin[s] = nb;
        first_begin[s] = fb;
        active_at_start[s] = act;
    }
}

/* spec_peak on an arbitrary grid (coarser-grid study): n spectra of nz values. */
SHM_API void shm_peak_grid(int n, int nz, float f0, float df, const float *z, float *hz) {
    for (int w = 0; w < n; w++) hz[w] = spec_peak(z + (long)w * nz, nz, f0, df);
}

SHM_API int shm_ring_max(void) { return F1T_RING_MAX; }
SHM_API int shm_c_max(void) { return F1T_C_MAX; }
SHM_API int shm_zoom_n(void) { return F1T_NZ; }
