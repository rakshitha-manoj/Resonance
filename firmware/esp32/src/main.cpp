#include <Arduino.h>
#include <Wire.h>
#include <MPU6050.h>

MPU6050 mpu;

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
    Serial.println("ax,ay,az,gx,gy,gz");
}

void loop() {
    int16_t ax, ay, az, gx, gy, gz;
    mpu.getMotion6(&ax, &ay, &az, &gx, &gy, &gz);
    Serial.printf("%d,%d,%d,%d,%d,%d\r\n", ax, ay, az, gx, gy, gz);
    delay(10);
}
