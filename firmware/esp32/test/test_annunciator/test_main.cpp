// Host tests of include/annunciator.h (master LEDs + buzzer patterns). pio test -e native
#include <unity.h>
#include "annunciator.h"

using ann::Annunciator;
using ann::Outputs;

void setUp(void) {}
void tearDown(void) {}

static const uint8_t AMB = 1, LOC = 2, BLD = 3;

void test_idle_is_dark_and_silent(void) {
    Annunciator a;
    Outputs o = a.service(1000);
    TEST_ASSERT_FALSE(o.heartbeat);
    TEST_ASSERT_FALSE(o.event);
    TEST_ASSERT_FALSE(o.damage);
    TEST_ASSERT_EQUAL_UINT16(0, o.tone_hz);
}

void test_heartbeat_pulses_per_superframe(void) {
    Annunciator a;
    a.on_superframe(AMB, false, 1000);
    TEST_ASSERT_TRUE(a.service(1000).heartbeat);
    TEST_ASSERT_TRUE(a.service(1000 + ann::HEARTBEAT_MS - 1).heartbeat);
    TEST_ASSERT_FALSE(a.service(1000 + ann::HEARTBEAT_MS).heartbeat);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(1001).tone_hz);   // ambient: no sound
}

void test_event_led_and_single_chirp(void) {
    Annunciator a;
    a.on_superframe(LOC, false, 0);
    Outputs o = a.service(0);
    TEST_ASSERT_TRUE(o.event);
    TEST_ASSERT_EQUAL_UINT16(2000, o.tone_hz);
    TEST_ASSERT_EQUAL_UINT16(2000, a.service(119).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(120).tone_hz);
    a.on_superframe(LOC, false, 1400);          // event continues: no new chirp
    TEST_ASSERT_EQUAL_UINT16(0, a.service(1400).tone_hz);
    TEST_ASSERT_TRUE(a.service(1400).event);
    a.on_superframe(BLD, false, 2800);          // LOCAL -> BUILDING is still an event: no chirp
    TEST_ASSERT_EQUAL_UINT16(0, a.service(2800).tone_hz);
    a.on_superframe(AMB, false, 4200);
    TEST_ASSERT_FALSE(a.service(4200).event);
    a.on_superframe(BLD, false, 5600);          // a new event after ambient chirps again
    TEST_ASSERT_EQUAL_UINT16(2000, a.service(5600).tone_hz);
}

void test_damage_begin_alarm_blink_then_solid(void) {
    Annunciator a;
    a.on_superframe(AMB, true, 10000);
    // three beeps: 0-200 on, 200-400 off, 400-600 on, 600-800 off, 800-1000 on
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(10000).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(10250).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(10450).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(10650).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(10850).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(11000).tone_hz);
    // blinking 2.5 Hz during the first 10 s
    TEST_ASSERT_TRUE(a.service(10000).damage);
    TEST_ASSERT_FALSE(a.service(10000 + ann::BLINK_HALF_MS).damage);
    TEST_ASSERT_TRUE(a.service(10000 + 2 * ann::BLINK_HALF_MS).damage);
    // solid afterwards
    for (uint32_t t = 10000 + ann::BLINK_MS; t < 10000 + ann::BLINK_MS + 2000; t += 50) TEST_ASSERT_TRUE(a.service(t).damage);
}

void test_damage_reminder_and_clear(void) {
    Annunciator a;
    a.on_superframe(AMB, true, 0);
    a.service(2000);                                         // alarm pattern finished
    TEST_ASSERT_EQUAL_UINT16(0, a.service(ann::REMIND_MS - 1).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(ann::REMIND_MS).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(ann::REMIND_MS + 150).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(2 * ann::REMIND_MS).tone_hz);   // and again 30 s later
    a.on_superframe(AMB, false, 2 * ann::REMIND_MS + 1000);
    Outputs o = a.service(2 * ann::REMIND_MS + 1000);
    TEST_ASSERT_FALSE(o.damage);
    TEST_ASSERT_EQUAL_UINT16(800, o.tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(2 * ann::REMIND_MS + 1400).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(3 * ann::REMIND_MS + 2000).tone_hz);   // no reminders after END
}

void test_event_chirp_does_not_cut_the_damage_alarm(void) {
    Annunciator a;
    a.on_superframe(AMB, true, 0);
    a.on_superframe(LOC, true, 300);                         // event begins while the alarm plays
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(450).tone_hz);   // still the alarm's second beep
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(850).tone_hz);   // and the third
    TEST_ASSERT_TRUE(a.service(850).event);
}

void test_millis_wraparound(void) {
    Annunciator a;
    uint32_t t0 = 0xFFFFFF00u;                               // 256 ms before the 32-bit millis() wrap
    a.on_superframe(AMB, true, t0);
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(t0 + 100).tone_hz);
    TEST_ASSERT_EQUAL_UINT16(0, a.service(t0 + 250).tone_hz);   // crosses the wrap
    TEST_ASSERT_EQUAL_UINT16(3000, a.service(t0 + 450).tone_hz);
    a.on_superframe(AMB, true, t0 + 1400);
    TEST_ASSERT_TRUE(a.service(t0 + 1400).heartbeat);
    TEST_ASSERT_FALSE(a.service(t0 + 1400 + ann::HEARTBEAT_MS).heartbeat);
}

void test_reset_clears_everything(void) {
    Annunciator a;
    a.on_superframe(LOC, true, 0);
    a.reset();
    Outputs o = a.service(10);
    TEST_ASSERT_FALSE(o.event);
    TEST_ASSERT_FALSE(o.damage);
    TEST_ASSERT_EQUAL_UINT16(0, o.tone_hz);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_idle_is_dark_and_silent);
    RUN_TEST(test_heartbeat_pulses_per_superframe);
    RUN_TEST(test_event_led_and_single_chirp);
    RUN_TEST(test_damage_begin_alarm_blink_then_solid);
    RUN_TEST(test_damage_reminder_and_clear);
    RUN_TEST(test_event_chirp_does_not_cut_the_damage_alarm);
    RUN_TEST(test_millis_wraparound);
    RUN_TEST(test_reset_clears_everything);
    return UNITY_END();
}
