#pragma once
/* Master-side DAMAGE detector. Arduino-free, no heap, C and C++11.

   Once per superframe the master hands over, for each slave, whether it has a usable report (a valid, commissioned
   f1 estimate) and its relative drop (f1_0 - f1_est) / f1_0 (f1track.h). Pooled drop = mean over the slaves that have
   one; a slave with a non-finite drop is treated as not reporting. No slave usable: the state AND the run counters are
   held unchanged (nothing is learned, nothing is forgotten).
   DAMAGE begins after P consecutive superframes with pooled drop >= delta; it ends after P consecutive superframes
   with pooled drop < delta / 2 (hysteresis; a pooled drop between delta/2 and delta, or a single exception, restarts
   the run that was counting). dmg_update returns the event of this superframe. */
#include <stdint.h>
#include <math.h>

#ifndef DMG_DELTA
#define DMG_DELTA 0.020f  /* relative f1 drop = selection in models/selected/validation/freq_tracking.json (test_shm checks) */
#endif
#ifndef DMG_P
#define DMG_P 32          /* consecutive superframes (1.4 s each), same source */
#endif

#define DMG_EV_NONE 0
#define DMG_EV_BEGIN 1
#define DMG_EV_END 2

typedef struct {
    float delta;
    int p;
    int active;    /* DAMAGE state */
    int up_run;    /* consecutive superframes with pooled >= delta (while not active) */
    int down_run;  /* consecutive superframes with pooled < delta/2 (while active) */
    float pooled;  /* last pooled drop, NAN if no slave was usable */
    int n_pooled;  /* slaves in the last pool */
} dmg_t;

static inline void dmg_init(dmg_t *d, float delta, int p) {
    d->delta = delta;
    d->p = p < 1 ? 1 : p;
    d->active = d->up_run = d->down_run = 0;
    d->pooled = NAN;
    d->n_pooled = 0;
}

static inline void dmg_init_default(dmg_t *d) { dmg_init(d, DMG_DELTA, DMG_P); }

/* ok[i] != 0: slave i reported with a valid commissioned estimate, drop[i] its relative drop. */
static inline int dmg_update(dmg_t *d, int n, const uint8_t *ok, const float *drop) {
    float sum = 0.0f;
    int used = 0;
    for (int i = 0; i < n; i++) {
        float v = drop[i];
        if (ok[i] && v == v && v - v == 0.0f) {
            sum += v;
            used++;
        }
    }
    d->n_pooled = used;
    if (used == 0) {
        d->pooled = NAN;
        return DMG_EV_NONE;
    }
    d->pooled = sum / (float)used;
    if (!d->active) {
        if (d->pooled >= d->delta) {
            if (++d->up_run >= d->p) {
                d->active = 1;
                d->up_run = d->down_run = 0;
                return DMG_EV_BEGIN;
            }
        } else {
            d->up_run = 0;
        }
    } else {
        if (d->pooled < 0.5f * d->delta) {
            if (++d->down_run >= d->p) {
                d->active = 0;
                d->up_run = d->down_run = 0;
                return DMG_EV_END;
            }
        } else {
            d->down_run = 0;
        }
    }
    return DMG_EV_NONE;
}
