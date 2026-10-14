#include <Arduino.h>
#include <Wire.h>
#include <MPU6050.h>
#include "features.h"
#include "smooth.h"

MPU6050 mpu;
int16_t az_buf[WIN_N];
int16_t gx_buf[WIN_N];
int idx = 0;
uint32_t window = 0;
uint32_t next_us;
uint32_t first_us;
int8_t hist[3] = {-1, -1, -1};
int state = 0;

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
    int dlpf = mpu.getDLPFMode();
    int accel = mpu.getFullScaleAccelRange();
    int gyro = mpu.getFullScaleGyroRange();
    if (dlpf != MPU6050_IMU::MPU6050_DLPF_BW_42 || accel != MPU6050_IMU::MPU6050_ACCEL_FS_2 || gyro != MPU6050_IMU::MPU6050_GYRO_FS_250) {
        Serial.printf("config fail dlpf=%d accel=%d gyro=%d\r\n", dlpf, accel, gyro);
        while (true) delay(1000);
    }
    Serial.printf("config ok dlpf=%d accel=%d gyro=%d\r\n", dlpf, accel, gyro);
    Serial.println("window,class,state,feat_us,tree_us,span_us");
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
    state = smooth_update(hist, c, state);
    Serial.printf("%u,%s,%s,%u,%u,%u\r\n", (unsigned)window++, class_names[c], class_names[state], (unsigned)(t1 - t0), (unsigned)(t2 - t1), (unsigned)(now - first_us));
    idx = 0;
    next_us = micros() + 10000;
}
