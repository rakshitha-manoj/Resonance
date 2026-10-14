#include <Arduino.h>
#include <Wire.h>
#include <MPU6050.h>
#include "features.h"

MPU6050 mpu;
int16_t az_buf[WIN_N];
int16_t gx_buf[WIN_N];
int idx = 0;
uint32_t window = 0;
uint32_t next_us;
uint32_t first_us;

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.printf("boot ok %s %s\r\n", __DATE__, __TIME__);
    Wire.begin(21, 22);
    mpu.initialize();
    if (!mpu.testConnection()) {
        Serial.println("mpu fail");
        while (true) delay(1000);
    }
    mpu.setDLPFMode(MPU6050_IMU::MPU6050_DLPF_BW_42);
    Serial.println("window,class,feat_us,tree_us,span_us");
    next_us = micros() + 10000;
}

void loop() {
    while ((int32_t)(micros() - next_us) < 0) {}
    uint32_t now = micros();
    next_us += 10000;
    int16_t ax, ay, az, gx, gy, gz;
    mpu.getMotion6(&ax, &ay, &az, &gx, &gy, &gz);
    if (idx == 0) first_us = now;
    az_buf[idx] = az;
    gx_buf[idx] = gx;
    if (++idx < WIN_N) return;
    float f[5];
    uint32_t t0 = micros();
    features(az_buf, gx_buf, f);
    uint32_t t1 = micros();
    int c = classify(f);
    uint32_t t2 = micros();
    Serial.printf("%u,%s,%u,%u,%u\r\n", (unsigned)window++, class_names[c], (unsigned)(t1 - t0), (unsigned)(t2 - t1), (unsigned)(now - first_us));
    idx = 0;
    next_us = micros() + 10000;
}
