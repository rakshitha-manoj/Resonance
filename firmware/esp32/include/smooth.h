#pragma once
#include <stdint.h>

static inline int smooth_update(int8_t *hist, int raw, int state) {
    hist[0] = hist[1];
    hist[1] = hist[2];
    hist[2] = (int8_t)raw;
    for (int c = 0; c < 3; c++) {
        int n = (hist[0] == c) + (hist[1] == c) + (hist[2] == c);
        if (n >= 2) return c;
    }
    return state;
}
