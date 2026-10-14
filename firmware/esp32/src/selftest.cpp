#include <Arduino.h>
#include "features.h"
#include "smooth.h"
#include "selftest_data.h"

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.printf("selftest %s %s windows=%d\r\n", __DATE__, __TIME__, ST_N);
    int cls_bad = 0;
    float max_abs[5] = {0}, max_rel[5] = {0};
    uint32_t feat_sum = 0, feat_max = 0, tree_sum = 0, tree_max = 0;
    for (int w = 0; w < ST_N; w++) {
        float f[5];
        uint32_t t0 = micros();
        features(st_az[w], st_gx[w], f);
        uint32_t t1 = micros();
        int c = classify(f);
        uint32_t t2 = micros();
        feat_sum += t1 - t0;
        tree_sum += t2 - t1;
        if (t1 - t0 > feat_max) feat_max = t1 - t0;
        if (t2 - t1 > tree_max) tree_max = t2 - t1;
        if (c != st_cls[w]) {
            cls_bad++;
            Serial.printf("class mismatch w=%d got=%d want=%d\r\n", w, c, st_cls[w]);
        }
        for (int j = 0; j < 5; j++) {
            float a = fabsf(f[j] - st_feat[w][j]);
            float r = a / (fabsf(st_feat[w][j]) + 1e-12f);
            if (a > max_abs[j]) max_abs[j] = a;
            if (r > max_rel[j]) max_rel[j] = r;
            if (j == 2 || j == 4) {
                if (a != 0.0f) Serial.printf("domfreq mismatch w=%d %s got=%.5f want=%.5f\r\n", w, used_features[j], f[j], st_feat[w][j]);
            } else if (j == 1 ? a > 1e-3f : r > 1e-4f) {
                Serial.printf("feature diff w=%d %s got=%.6f want=%.6f\r\n", w, used_features[j], f[j], st_feat[w][j]);
            }
        }
    }
    Serial.printf("selftest %d/%d class match, %d class mismatches\r\n", ST_N - cls_bad, ST_N, cls_bad);
    for (int j = 0; j < 5; j++)
        Serial.printf("max diff %s abs=%.6g rel=%.3g\r\n", used_features[j], max_abs[j], max_rel[j]);
    Serial.printf("feature us avg=%u max=%u, tree us avg=%u max=%u\r\n",
                  (unsigned)(feat_sum / ST_N), (unsigned)feat_max, (unsigned)(tree_sum / ST_N), (unsigned)tree_max);

    Serial.printf("seq windows=%d segments=%d\r\n", SQ_N, SQ_NSEG);
    int8_t hist[3] = {-1, -1, -1};
    int state = 0, prev_raw = 0, prev_state = 0;
    int raw_bad = 0, state_bad = 0, raw_ch = 0, state_ch = 0;
    int raw_got[SQ_N], state_got[SQ_N];
    for (int w = 0; w < SQ_N; w++) {
        float f[5];
        features(sq_az[w], sq_gx[w], f);
        int c = classify(f);
        state = smooth_update(hist, c, state);
        raw_got[w] = c;
        state_got[w] = state;
        if (c != sq_raw[w]) raw_bad++;
        if (state != sq_state[w]) state_bad++;
        if (w > 0 && c != prev_raw) raw_ch++;
        if (w > 0 && state != prev_state) state_ch++;
        if (w == 0 || c != prev_raw || state != prev_state)
            Serial.printf("seq w=%d label=%d raw=%d state=%d want_raw=%d want_state=%d\r\n", w, sq_label[w], c, state, sq_raw[w], sq_state[w]);
        prev_raw = c;
        prev_state = state;
    }
    Serial.printf("seq raw match %d/%d, state match %d/%d\r\n", SQ_N - raw_bad, SQ_N, SQ_N - state_bad, SQ_N);
    Serial.printf("seq raw class changes %d, state changes %d\r\n", raw_ch, state_ch);
    for (int s = 0; s < SQ_NSEG; s++) {
        int b = sq_seg_start[s];
        int e = s + 1 < SQ_NSEG ? sq_seg_start[s + 1] : SQ_N;
        int L = sq_label[b];
        int dr = -1, ds = -1;
        for (int w = b; w < e; w++) {
            if (dr < 0 && raw_got[w] == L) dr = w - b;
            if (ds < 0 && state_got[w] == L) ds = w - b;
        }
        Serial.printf("seq segment %d label=%d start=%d delay raw=%d state=%d\r\n", s, L, b, dr, ds);
    }
    Serial.printf("seq done %s\r\n", raw_bad == 0 && state_bad == 0 ? "PASS" : "FAIL");
}

void loop() {
    delay(1000);
}
