#include <Wire.h>
#include <MPU6050.h>

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("boot ok");
}

void loop() {
    Serial.println("tick");
    delay(1000);
}