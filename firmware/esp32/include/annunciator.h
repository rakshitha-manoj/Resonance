#pragma once
// Local alarm outputs on the master: LEDs and a buzzer, driven by the fusion output.
// Portable, Arduino-free and heap-free (C++11); host-tested in test/test_annunciator.
// The firmware (src/net_main.cpp) feeds it from the fusion sink, then calls service(now_ms) from loop()
// and copies the outputs to GPIO / the buzzer tone. It never blocks and has no effect on any decision.
//
//   heartbeat LED : on for HEARTBEAT_MS after every closed superframe (the system is alive)
//   event LED     : on while the class is LOCAL or BUILDING (a vibration event)
//   damage LED    : blinks at 2.5 Hz for BLINK_MS after a DAMAGE BEGIN, then on while DAMAGE is active
//   buzzer        : event begin     -> one 120 ms chirp at 2 kHz
//                   DAMAGE BEGIN    -> three 200 ms beeps at 3 kHz (200 ms gaps)
//                   DAMAGE active   -> one 150 ms reminder beep at 3 kHz every REMIND_MS
//                   DAMAGE END      -> one 400 ms tone at 800 Hz
//   A newer pattern replaces one still playing; the DAMAGE BEGIN pattern cannot be cut short by an event chirp.
#include <stdint.h>

namespace ann {

constexpr uint32_t HEARTBEAT_MS = 60;
constexpr uint32_t BLINK_MS = 10000;
constexpr uint32_t BLINK_HALF_MS = 200;    // 2.5 Hz
constexpr uint32_t REMIND_MS = 30000;

struct Step { uint16_t hz; uint16_t ms; };   // hz 0 = silence

struct Pattern {
    const Step *steps;
    uint8_t n;
    uint8_t prio;      // a pattern of lower priority does not interrupt one of higher priority
};

static const Step S_CHIRP[] = {{2000, 120}};
static const Step S_ALARM[] = {{3000, 200}, {0, 200}, {3000, 200}, {0, 200}, {3000, 200}};
static const Step S_REMIND[] = {{3000, 150}};
static const Step S_CLEAR[] = {{800, 400}};
static const Pattern P_CHIRP = {S_CHIRP, 1, 1};
static const Pattern P_ALARM = {S_ALARM, 5, 3};
static const Pattern P_REMIND = {S_REMIND, 1, 2};
static const Pattern P_CLEAR = {S_CLEAR, 1, 2};

struct Outputs {
    bool heartbeat = false, event = false, damage = false;
    uint16_t tone_hz = 0;   // 0 = buzzer off
};

class Annunciator {
public:
    // One call per closed superframe. cls: fusion::Cls value (2 = LOCAL, 3 = BUILDING); dmg: DAMAGE active after it.
    void on_superframe(uint8_t cls, bool dmg, uint32_t now_ms) {
        hb_until_ = now_ms + HEARTBEAT_MS;
        hb_on_ = true;
        bool ev = (cls == 2 || cls == 3);
        if (ev && !event_) play(P_CHIRP, now_ms);
        event_ = ev;
        if (dmg && !dmg_) {
            dmg_since_ = now_ms;
            next_remind_ = now_ms + REMIND_MS;
            play(P_ALARM, now_ms);
        } else if (!dmg && dmg_) {
            play(P_CLEAR, now_ms);
        }
        dmg_ = dmg;
    }

    // Master restart / reboot of the fusion stream: lights off, nothing playing (DAMAGE state comes back with the next sample).
    void reset() { *this = Annunciator(); }

    // Call often (every loop pass). Returns the outputs to apply now.
    Outputs service(uint32_t now_ms) {
        Outputs o;
        if (hb_on_ && (int32_t)(now_ms - hb_until_) >= 0) hb_on_ = false;
        o.heartbeat = hb_on_;
        o.event = event_;
        if (dmg_) {
            uint32_t since = now_ms - dmg_since_;
            o.damage = since >= BLINK_MS ? true : ((since / BLINK_HALF_MS) % 2) == 0;
            if ((int32_t)(now_ms - next_remind_) >= 0) {
                next_remind_ = now_ms + REMIND_MS;
                play(P_REMIND, now_ms);
            }
        }
        o.tone_hz = tone(now_ms);
        return o;
    }

    bool damage_active() const { return dmg_; }
    bool event_active() const { return event_; }

private:
    void play(const Pattern &p, uint32_t now_ms) {
        if (pat_ && pat_->prio > p.prio && step_ < pat_->n) return;   // do not cut a more important pattern short
        pat_ = &p;
        step_ = 0;
        step_end_ = now_ms + p.steps[0].ms;
    }
    uint16_t tone(uint32_t now_ms) {
        while (pat_ && step_ < pat_->n && (int32_t)(now_ms - step_end_) >= 0) {
            step_++;
            if (step_ < pat_->n) step_end_ += pat_->steps[step_].ms;
        }
        if (!pat_ || step_ >= pat_->n) {
            pat_ = nullptr;
            return 0;
        }
        return pat_->steps[step_].hz;
    }

    bool event_ = false, dmg_ = false, hb_on_ = false;
    uint32_t hb_until_ = 0, dmg_since_ = 0, next_remind_ = 0;
    const Pattern *pat_ = nullptr;
    uint8_t step_ = 0;
    uint32_t step_end_ = 0;
};

}  // namespace ann
