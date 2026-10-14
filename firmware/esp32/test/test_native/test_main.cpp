#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <vector>
#include <unity.h>
#include "features.h"
#include "smooth.h"

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

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_all_windows);
    RUN_TEST(test_smooth);
    return UNITY_END();
}
