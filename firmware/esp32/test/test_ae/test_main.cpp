#include <stdio.h>
#include <stdint.h>
#include <string.h>
#define _USE_MATH_DEFINES
#include <math.h>
#include <vector>
#include <algorithm>
#include <unity.h>
#include "ae.h"

/* Layout of test/data/ae_vectors.bin (tools/gen_ae_vectors.py):
   "AEV1", int32 n_vec, int32 n_in, int32 n_float; then n_vec x (int8 q_in[n_in], int8 q_out[n_in]);
   then n_float x (float32 x[n_in]) for the first n_float vectors (q_in of vector i = TFLite Quantize of x[i]). */
void setUp(void) {}
void tearDown(void) {}

struct Vec {
    int n, nin, nf;
    std::vector<int8_t> qi, qo;
    std::vector<float> x;
};

static Vec load_vectors() {
    Vec v;
    FILE *fp = fopen(AE_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    char magic[4];
    int32_t h[3];
    TEST_ASSERT_EQUAL(4, fread(magic, 1, 4, fp));
    TEST_ASSERT_EQUAL_INT(0, memcmp(magic, "AEV1", 4));
    TEST_ASSERT_EQUAL(3, fread(h, 4, 3, fp));
    v.n = h[0];
    v.nin = h[1];
    v.nf = h[2];
    TEST_ASSERT_EQUAL_INT(AE_N_IN, v.nin);
    v.qi.resize((size_t)v.n * v.nin);
    v.qo.resize((size_t)v.n * v.nin);
    v.x.resize((size_t)v.nf * v.nin);
    for (int i = 0; i < v.n; i++) {
        TEST_ASSERT_EQUAL((size_t)v.nin, fread(&v.qi[(size_t)i * v.nin], 1, v.nin, fp));
        TEST_ASSERT_EQUAL((size_t)v.nin, fread(&v.qo[(size_t)i * v.nin], 1, v.nin, fp));
    }
    TEST_ASSERT_EQUAL((size_t)v.nf * v.nin, fread(v.x.data(), 4, (size_t)v.nf * v.nin, fp));
    fclose(fp);
    return v;
}

/* C kernel vs the TFLite interpreter (reference kernels) on every stored vector: identical int8 outputs. */
void test_int8_parity_all_vectors(void) {
    Vec v = load_vectors();
    TEST_ASSERT_TRUE(v.n >= 10000);
    long mism = 0, elems = 0;
    int first_bad = -1;
    for (int i = 0; i < v.n; i++) {
        int8_t out[AE_N_IN];
        ae_run_int8(&v.qi[(size_t)i * v.nin], out);
        for (int j = 0; j < v.nin; j++) {
            elems++;
            if (out[j] != v.qo[(size_t)i * v.nin + j]) {
                mism++;
                if (first_bad < 0) first_bad = i;
            }
        }
    }
    printf("AE parity: %d vectors, %ld int8 outputs compared, %ld mismatches (first bad vector %d)\n", v.n, elems, mism, first_bad);
    TEST_ASSERT_EQUAL_INT(0, (int)mism);
}

/* float -> int8 input quantisation equals the Python TFLite-Quantize reference (incl. saturation and rounding boundaries) */
void test_input_quantisation_vectors(void) {
    Vec v = load_vectors();
    TEST_ASSERT_TRUE(v.nf >= 500);
    int bad = 0;
    for (int i = 0; i < v.nf; i++)
        for (int j = 0; j < v.nin; j++)
            if (ae_quant_in(v.x[(size_t)i * v.nin + j]) != v.qi[(size_t)i * v.nin + j]) bad++;
    printf("input quantisation: %d float vectors, %d mismatches\n", v.nf, bad);
    TEST_ASSERT_EQUAL_INT(0, bad);
}

void test_quant_edge_cases(void) {
    TEST_ASSERT_EQUAL_INT8(-128, ae_quant_in(NAN));
    TEST_ASSERT_EQUAL_INT8(127, ae_quant_in(INFINITY));
    TEST_ASSERT_EQUAL_INT8(-128, ae_quant_in(-INFINITY));
    TEST_ASSERT_EQUAL_INT8(127, ae_quant_in(1e30f));
    TEST_ASSERT_EQUAL_INT8(-128, ae_quant_in(-1e30f));
    TEST_ASSERT_EQUAL_INT8(AE_IN_ZP, ae_quant_in(0.0f));
    /* half away from zero: 0.5 steps above/below the zero point */
    float inv = 1.0f / AE_IN_SCALE;
    (void)inv;
    TEST_ASSERT_EQUAL_INT8(AE_IN_ZP + 1, ae_quant_in(0.5001f * AE_IN_SCALE));
    TEST_ASSERT_EQUAL_INT8(AE_IN_ZP - 1, ae_quant_in(-0.5001f * AE_IN_SCALE));
    TEST_ASSERT_EQUAL_INT8(AE_IN_ZP, ae_quant_in(0.4999f * AE_IN_SCALE));
}

/* ae_mul_quant == floor(x * mult * 2^(shift-31) + 0.5), exactly (x small enough for double) */
void test_mul_quant(void) {
    uint32_t s = 12345;
    for (int i = 0; i < 40000; i++) {
        s = s * 1664525u + 1013904223u;
        int32_t x = (int32_t)(s >> 10) - (1 << 21);
        s = s * 1664525u + 1013904223u;
        int32_t m = (int32_t)(s >> 1) | (1 << 30);   /* normalised multiplier in [2^30, 2^31) */
        s = s * 1664525u + 1013904223u;
        int sh = (int)((s >> 20) % 13) - 12;          /* -12 .. 0 */
        double want = floor((double)x * (double)m * ldexp(1.0, sh - 31) + 0.5);
        TEST_ASSERT_TRUE((double)ae_mul_quant(x, m, sh) == want);
    }
    TEST_ASSERT_EQUAL_INT32(1, ae_mul_quant(1, 1 << 30, 0));
    TEST_ASSERT_EQUAL_INT32(0, ae_mul_quant(-1, 1 << 30, 0));        /* -0.5 rounds up to 0 (floor(x + 0.5)) */
}

/* ----- normaliser */
static void fake_base(uint32_t *s, float *b, float level) {
    for (int i = 0; i < AE_NBASE; i++) {
        *s = *s * 1664525u + 1013904223u;
        float u = (float)(*s >> 8) / 16777216.0f;
        b[i] = (i < 16 ? 3.0f + 0.3f * (float)i : 1.0f) * level + 0.2f * u;
    }
}

void test_normaliser_vs_double(void) {
    AeNode nd;
    ae_node_reset(&nd);
    uint32_t s = 99;
    float base[AE_COMM_N][AE_NBASE];
    int done = 0;
    for (int w = 0; w < AE_COMM_N; w++) {
        fake_base(&s, base[w], 1.0f);
        done = ae_node_commission(&nd, base[w]);
        TEST_ASSERT_EQUAL_INT(w == AE_COMM_N - 1 ? 1 : 0, done);
    }
    TEST_ASSERT_EQUAL_INT(0, ae_node_commission(&nd, base[0]));   /* further windows are ignored */
    for (int i = 0; i < AE_N_IN; i++) {
        std::vector<double> col, dev;
        for (int w = 0; w < AE_COMM_N; w++) col.push_back(nd.x[w][i]);
        std::vector<double> c = col;
        std::sort(c.begin(), c.end());
        double med = 0.5 * (c[9] + c[10]);
        for (double x : col) dev.push_back(fabs(x - med));
        std::sort(dev.begin(), dev.end());
        double mad = 0.5 * (dev[9] + dev[10]);
        double sc = std::max(1.4826 * mad, (double)AE_NORM_FLOOR[i]);
        TEST_ASSERT_TRUE(fabs(med - nd.med[i]) <= 1e-5 * fabs(med) + 1e-7);
        TEST_ASSERT_TRUE(fabs(sc - nd.scale[i]) <= 1e-4 * sc + 1e-7);
    }
}

void test_floor_and_constant_input(void) {
    AeNode nd;
    ae_node_reset(&nd);
    float b[AE_NBASE];
    for (int i = 0; i < AE_NBASE; i++) b[i] = 2.0f + (float)i;
    for (int w = 0; w < AE_COMM_N; w++) ae_node_commission(&nd, b);   /* identical windows: MAD 0 */
    for (int i = 0; i < AE_N_IN; i++) TEST_ASSERT_EQUAL_FLOAT(AE_NORM_FLOOR[i], nd.scale[i]);
    float z[AE_N_IN];
    ae_normalise(&nd, nd.x[0], z);
    for (int i = 0; i < AE_N_IN; i++) TEST_ASSERT_EQUAL_FLOAT(0.0f, z[i]);
}

void test_score_and_ratio(void) {
    AeNode nd;
    ae_node_reset(&nd);
    uint32_t s = 7;
    float base[AE_COMM_N][AE_NBASE];
    for (int w = 0; w < AE_COMM_N; w++) {
        fake_base(&s, base[w], 1.0f);
        ae_node_commission(&nd, base[w]);
    }
    TEST_ASSERT_TRUE(nd.ref > 0.0f);
    /* score of z vs an independent double computation from the int8 kernel */
    for (int t = 0; t < 200; t++) {
        float z[AE_N_IN];
        for (int i = 0; i < AE_N_IN; i++) {
            s = s * 1664525u + 1013904223u;
            z[i] = ((float)(s >> 8) / 16777216.0f - 0.5f) * 8.0f;
        }
        int8_t q[AE_N_IN], y[AE_N_IN];
        for (int i = 0; i < AE_N_IN; i++) q[i] = ae_quant_in(z[i]);
        ae_run_int8(q, y);
        double acc = 0;
        for (int i = 0; i < AE_N_IN; i++) {
            double d = (double)z[i] - (double)(y[i] - AE_OUT_ZP) * (double)AE_OUT_SCALE;
            acc += d * d;
        }
        acc /= AE_N_IN;
        float got = ae_score_z(z);
        TEST_ASSERT_TRUE(fabs(acc - (double)got) <= 1e-5 * acc + 1e-9);
    }
    /* the ratio of the commissioning windows themselves has median 1 */
    std::vector<double> r;
    for (int w = 0; w < AE_COMM_N; w++) r.push_back(ae_node_ratio(&nd, base[w], NULL));
    std::sort(r.begin(), r.end());
    TEST_ASSERT_TRUE(fabs(1.0 - 0.5 * (r[9] + r[10])) <= 1e-5);
    /* a strongly different window scores much higher than commissioning ones */
    float odd[AE_NBASE];
    memcpy(odd, base[0], sizeof odd);
    for (int i = 0; i < 16; i++) odd[i] += (i % 2 ? 4.0f : -4.0f);
    TEST_ASSERT_TRUE(ae_node_ratio(&nd, odd, NULL) > 5.0f);
}

/* ae_base_ex (from features_ex outputs) == ae_base (from raw windows), bit for bit */
void test_base_features_consistent(void) {
    uint32_t s = 1;
    for (int t = 0; t < 40; t++) {
        int16_t az[WIN_N], gx[WIN_N];
        for (int i = 0; i < WIN_N; i++) {
            s = s * 1664525u + 1013904223u;
            az[i] = (int16_t)(16384 + (int)((s >> 16) & 0x1ff) - 256 + (int)(300.0 * sin(0.4 * i + t)));
            s = s * 1664525u + 1013904223u;
            gx[i] = (int16_t)((int)((s >> 16) & 0x7f) - 64 + (int)(20.0 * sin(0.9 * i)));
        }
        float f[5], p[64], b1[AE_NBASE], b2[AE_NBASE], dk, dd;
        features_ex(az, gx, f, p);
        feat_std_kurt(gx, &dd, &dk);
        ae_base_ex(f, p, dk, b1);
        ae_base(az, gx, b2);
        TEST_ASSERT_EQUAL_INT(0, memcmp(b1, b2, sizeof b1));
        TEST_ASSERT_TRUE(isfinite(b1[0]) && isfinite(b1[19]));
    }
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_int8_parity_all_vectors);
    RUN_TEST(test_input_quantisation_vectors);
    RUN_TEST(test_quant_edge_cases);
    RUN_TEST(test_mul_quant);
    RUN_TEST(test_normaliser_vs_double);
    RUN_TEST(test_floor_and_constant_input);
    RUN_TEST(test_score_and_ratio);
    RUN_TEST(test_base_features_consistent);
    return UNITY_END();
}
