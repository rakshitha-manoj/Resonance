#include <stdio.h>
#include <stdint.h>
#include <string.h>
#define _USE_MATH_DEFINES
#include <math.h>
#include <vector>
#include <unity.h>
#include "features.h"
#include "smooth.h"
#include "spectrum.h"

struct Rec {
    int16_t az[128];
    int16_t gx[128];
    int32_t cls;
    double feat[5];
};

void setUp(void) {}
void tearDown(void) {}

void test_all_windows(void) {
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    std::vector<unsigned char> buf(556);
    int n = 0, bad = 0, domdiff = 0;
    double max_abs[5] = {0};
    while (fread(buf.data(), 1, 556, fp) == 556) {
        Rec r;
        memcpy(r.az, buf.data(), 256);
        memcpy(r.gx, buf.data() + 256, 256);
        memcpy(&r.cls, buf.data() + 512, 4);
        memcpy(r.feat, buf.data() + 516, 40);
        float f[5];
        features(r.az, r.gx, f);
        int c = classify(f);
        if (c != r.cls) {
            bad++;
            printf("class mismatch window %d got=%d want=%d\n", n, c, (int)r.cls);
        }
        for (int j = 0; j < 5; j++) {
            double a = fabs((double)f[j] - r.feat[j]);
            if (a > max_abs[j]) max_abs[j] = a;
            if ((j == 2 || j == 4) && a != 0.0) domdiff++;
        }
        n++;
    }
    fclose(fp);
    const char *names[5] = {"az_std", "az_kurt", "az_domfreq", "gx_std", "gx_domfreq"};
    printf("windows=%d class_match=%d/%d mismatches=%d domfreq_diffs=%d\n", n, n - bad, n, bad, domdiff);
    for (int j = 0; j < 5; j++) printf("max abs diff %s = %.9g\n", names[j], max_abs[j]);
    TEST_ASSERT_EQUAL_INT(6000, n);
    TEST_ASSERT_EQUAL_INT(0, bad);
}

void test_smooth(void) {
    FILE *fp = fopen(SMOOTH_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    uint32_t count = 0;
    TEST_ASSERT_EQUAL_INT(1, (int)fread(&count, 4, 1, fp));
    int bad = 0, total = 0;
    for (uint32_t s = 0; s < count; s++) {
        uint16_t len = 0;
        TEST_ASSERT_EQUAL_INT(1, (int)fread(&len, 2, 1, fp));
        std::vector<int8_t> raw(len), want(len);
        TEST_ASSERT_EQUAL_INT(len, (int)fread(raw.data(), 1, len, fp));
        TEST_ASSERT_EQUAL_INT(len, (int)fread(want.data(), 1, len, fp));
        int8_t hist[3] = {-1, -1, -1};
        int state = 0;
        for (int i = 0; i < len; i++) {
            state = smooth_update(hist, raw[i], state);
            total++;
            if (state != want[i]) {
                bad++;
                printf("smooth mismatch seq %u step %d got=%d want=%d\n", (unsigned)s, i, state, (int)want[i]);
            }
        }
    }
    fclose(fp);
    printf("smooth sequences=%u steps=%d mismatches=%d\n", (unsigned)count, total, bad);
    TEST_ASSERT_EQUAL_INT(2592, (int)count);
    TEST_ASSERT_EQUAL_INT(0, bad);
}

static bool next_rec(FILE *fp, Rec &r) {
    unsigned char buf[556];
    if (fread(buf, 1, 556, fp) != 556) return false;
    memcpy(r.az, buf, 256);
    memcpy(r.gx, buf + 256, 256);
    memcpy(&r.cls, buf + 512, 4);
    memcpy(r.feat, buf + 516, 40);
    return true;
}

void test_spec_power_domfreq(void) {
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    Rec r;
    int n = 0, bad = 0, bad_ex = 0;
    while (next_rec(fp, r)) {
        float p[64];
        spec_power(r.az, p);
        int bk = 0;
        for (int k = 1; k < 64; k++)
            if (p[k] > p[bk]) bk = k;
        float f[5], f2[5], p2[64];
        features(r.az, r.gx, f);
        features_ex(r.az, r.gx, f2, p2);
        if ((bk + 1) * BIN_HZ != f[2] || f[2] != feat_domfreq(r.az)) bad++;
        for (int j = 0; j < 5; j++) if (f[j] != f2[j]) bad_ex++;
        for (int k = 0; k < 64; k++) if (p[k] != p2[k]) bad_ex++;
        n++;
    }
    fclose(fp);
    printf("spec_power argmax vs feat_domfreq: windows=%d mismatches=%d, features_ex diffs=%d\n", n, bad, bad_ex);
    TEST_ASSERT_EQUAL_INT(6000, n);
    TEST_ASSERT_EQUAL_INT(0, bad);
    TEST_ASSERT_EQUAL_INT(0, bad_ex);
}

void test_spec_bands(void) {
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    Rec r;
    int n = 0;
    double worst = 0;
    while (next_rec(fp, r) && n < 600) {
        float p[64], b[16];
        spec_power(r.az, p);
        spec_bands(p, b);
        double mean = 0;
        for (int i = 0; i < 128; i++) mean += r.az[i];
        mean /= 128;
        for (int bb = 0; bb < 16; bb++) {
            double s = 0;
            for (int k = 4 * bb + 1; k <= 4 * bb + 4; k++) {
                double re = 0, im = 0;
                for (int i = 0; i < 128; i++) {
                    double ph = 2 * M_PI * (double)((k * i) & 127) / 128.0;
                    re += (r.az[i] - mean) * cos(ph);
                    im += (r.az[i] - mean) * sin(ph);
                }
                s += re * re + im * im;
            }
            double want = log(s + 1.0);
            double rel = fabs(b[bb] - want) / fabs(want);
            if (rel > worst) worst = rel;
        }
        n++;
    }
    fclose(fp);
    printf("spec_bands max rel err vs double reference (%d windows) = %.3g\n", n, worst);
    TEST_ASSERT_TRUE(worst < 1e-5);
}

static double ref_zoom(const int16_t *x, double f, double *mean_out) {
    double mean = 0;
    for (int i = 0; i < 128; i++) mean += x[i];
    mean /= 128;
    (void)mean_out;
    double re = 0, im = 0;
    for (int i = 0; i < 128; i++) {
        double w = 0.5 - 0.5 * cos(2 * M_PI * i / 127.0);
        double ph = 2 * M_PI * f * i / 100.0;
        re += (x[i] - mean) * w * cos(ph);
        im += (x[i] - mean) * w * sin(ph);
    }
    return re * re + im * im;
}

// Reference one-frequency Goertzel loop (the pre-interleave spec_zoom): spec_zoom now runs four frequencies per pass and must be
// BIT-identical to it.
static void spec_zoom_ref(const int16_t *az, float *z) {
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

void test_spec_zoom_interleaved_bit_identical(void) {
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    std::vector<unsigned char> buf(556);
    int n = 0, bad = 0;
    float za[SPEC_ZOOM_N], zb[SPEC_ZOOM_N];
    while (fread(buf.data(), 1, 556, fp) == 556) {
        int16_t az[128];
        memcpy(az, buf.data(), 256);
        spec_zoom(az, za);
        spec_zoom_ref(az, zb);
        if (memcmp(za, zb, sizeof za) != 0) bad++;
        n++;
    }
    fclose(fp);
    // random windows, incl. extremes and a constant window
    unsigned s = 12345;
    int nr = 0;
    for (int w = 0; w < 5000; w++) {
        int16_t az[128];
        for (int i = 0; i < 128; i++) {
            s = s * 1103515245u + 12345u;
            az[i] = (w % 50 == 0) ? (int16_t)7 : (w % 50 == 1) ? (int16_t)((i & 1) ? 32767 : -32768) : (int16_t)(s >> 16);
        }
        spec_zoom(az, za);
        spec_zoom_ref(az, zb);
        if (memcmp(za, zb, sizeof za) != 0) bad++;
        nr++;
    }
    printf("spec_zoom 4-way vs reference: %d dataset + %d random windows, %d bit mismatches\n", n, nr, bad);
    TEST_ASSERT_EQUAL_INT(6000, n);
    TEST_ASSERT_EQUAL_INT(0, bad);
}

void test_spec_zoom(void) {
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    Rec r;
    int n = 0;
    double worst = 0;
    while (next_rec(fp, r)) {
        if (n++ % 60) continue;  /* 100 windows */
        float z[SPEC_ZOOM_N];
        spec_zoom(r.az, z);
        double ref[SPEC_ZOOM_N], mx = 0;
        for (int j = 0; j < SPEC_ZOOM_N; j++) {
            ref[j] = ref_zoom(r.az, 3.0 + 0.05 * j, NULL);
            if (ref[j] > mx) mx = ref[j];
        }
        for (int j = 0; j < SPEC_ZOOM_N; j++) {
            double e = fabs(z[j] - ref[j]) / mx;
            if (e > worst) worst = e;
        }
    }
    fclose(fp);
    printf("spec_zoom max err relative to spectrum max = %.3g\n", worst);
    TEST_ASSERT_TRUE(worst < 1e-4);
}

void test_spec_peak_sines(void) {
    const double fs[3] = {4.37, 7.0, 9.91};
    for (int t = 0; t < 3; t++) {
        int16_t x[128];
        for (int i = 0; i < 128; i++) x[i] = (int16_t)lrint(500.0 * sin(2 * M_PI * fs[t] * i / 100.0 + 0.3));
        float z[SPEC_ZOOM_N];
        spec_zoom(x, z);
        float hz = spec_peak(z, SPEC_ZOOM_N, SPEC_ZOOM_F0, SPEC_ZOOM_DF);
        printf("spec_peak sine %.2f Hz -> %.4f\n", fs[t], hz);
        TEST_ASSERT_TRUE(fabs(hz - fs[t]) < 0.02);
    }
}

/* Edge cases: the +1 floor of spec_bands (a constant / dead sensor gives exactly 0, never -inf), spec_peak ties
   (first maximum wins), NaN entries (skipped), all-NaN (f0), and feat_domfreq on a tied flat spectrum (first bin). */
void test_spec_edges(void) {
    float p[64];
    for (int i = 0; i < 64; i++) p[i] = 0.0f;
    float b[SPEC_BANDS];
    spec_bands(p, b);
    for (int i = 0; i < SPEC_BANDS; i++) TEST_ASSERT_EQUAL_FLOAT(0.0f, b[i]);
    int16_t c[128];
    for (int i = 0; i < 128; i++) c[i] = 1234;
    float pc[64];
    spec_power(c, pc);
    spec_bands(pc, b);
    for (int i = 0; i < SPEC_BANDS; i++) TEST_ASSERT_TRUE(b[i] == 0.0f);

    float z[SPEC_ZOOM_N];
    for (int i = 0; i < SPEC_ZOOM_N; i++) z[i] = 1.0f;
    z[40] = 5.0f;
    z[120] = 5.0f;  /* tie: first maximum wins; flat neighbours -> non-concave -> grid frequency */
    TEST_ASSERT_EQUAL_FLOAT(SPEC_ZOOM_F0 + 40 * SPEC_ZOOM_DF, spec_peak(z, SPEC_ZOOM_N, SPEC_ZOOM_F0, SPEC_ZOOM_DF));
    z[120] = 1.0f;
    z[0] = NAN;
    z[7] = NAN;
    TEST_ASSERT_EQUAL_FLOAT(SPEC_ZOOM_F0 + 40 * SPEC_ZOOM_DF, spec_peak(z, SPEC_ZOOM_N, SPEC_ZOOM_F0, SPEC_ZOOM_DF));
    for (int i = 0; i < SPEC_ZOOM_N; i++) z[i] = NAN;
    TEST_ASSERT_EQUAL_FLOAT(SPEC_ZOOM_F0, spec_peak(z, SPEC_ZOOM_N, SPEC_ZOOM_F0, SPEC_ZOOM_DF));

    /* a single impulse has a flat |DFT|: every bin ties, the tree's domfreq must be bin 1 (first maximum) */
    int16_t imp[128];
    for (int i = 0; i < 128; i++) imp[i] = 0;
    imp[0] = 1280;
    float pi[64];
    spec_power(imp, pi);
    int k = 0;
    for (int i = 1; i < 64; i++) if (pi[i] > pi[k]) k = i;
    TEST_ASSERT_EQUAL_FLOAT((k + 1) * BIN_HZ, feat_domfreq(imp));
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_spec_edges);
    RUN_TEST(test_all_windows);
    RUN_TEST(test_smooth);
    RUN_TEST(test_spec_power_domfreq);
    RUN_TEST(test_spec_bands);
    RUN_TEST(test_spec_zoom);
    RUN_TEST(test_spec_zoom_interleaved_bit_identical);
    RUN_TEST(test_spec_peak_sines);
    return UNITY_END();
}
