#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <vector>
#include <unity.h>
#include "features.h"

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

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_all_windows);
    return UNITY_END();
}
