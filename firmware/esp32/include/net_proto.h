#pragma once
// Wire protocol for the 3-node (1 master + 2 slaves) network, plus every timing
// constant. No Arduino dependency; compiles as C++11 on host and ESP32.
//
// Byte order: little-endian on both supported targets (x86-64, Xtensa ESP32).
// Fields are serialised with memcpy of the scalar into the buffer; this is
// checked at compile time below. No pointer casts onto packet buffers.
//
// Frame layout (all packets):
//   [0] magic  [1] type  [2] version  [3] src id  [4] dst id  [5..6] tx seq (u16)
//   [7..]  type-specific payload
//   [n-2..n-1] CRC-16/CCITT-FALSE over bytes [0..n-3], stored little-endian
// tx seq is the sender's own packet counter (diagnostic only); protocol logic
// uses the superframe sequence in the payload.
//
// Master boot epoch: the master picks a fresh 32-bit epoch every time it (re)starts (random on
// hardware, from the PRNG in the simulator) and puts it in every BEACON, POLL and REPORT. A slave
// that sees a new epoch resyncs at once; reports and polls with a foreign epoch are ignored.
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "net_proto.h requires a little-endian target"
#endif
#endif

namespace net {

// ---------------------------------------------------------------- constants
// All timing in microseconds. Change here only.
constexpr uint32_t SUPERFRAME_US       = 1400000;  // beacon period (beacon at t=0 of each superframe)
constexpr uint32_t SAMPLE_PERIOD_US    = 10000;    // 100 Hz sampling
constexpr uint32_t WIN_SAMPLES         = 128;
constexpr uint32_t SAMPLE_SPAN_US      = (WIN_SAMPLES - 1) * SAMPLE_PERIOD_US;  // first->last sample: 1,270,000
constexpr uint32_t POLL_START_OFFSET_US = 1320000; // master starts polling this long after its beacon
constexpr uint32_t REPLY_TIMEOUT_US    = 18000;    // wait per attempt (round trip + slave turnaround)
constexpr uint32_t POLL_ATTEMPTS       = 2;        // first try + exactly one retry
constexpr uint8_t  NUM_SLAVES          = 2;        // slave ids are 1..NUM_SLAVES
constexpr uint32_t BEACON_GRACE_US     = 5000;     // slave waits this long past the expected beacon before free-running
constexpr uint32_t MAX_MISSED_BEACONS  = 5;        // slave free-runs at most this many consecutive superframes, then goes LOST (silent) until a beacon
constexpr uint32_t DEAD_AFTER_MISSES   = 3;        // master: consecutive superframes without a valid report -> DEAD
// A synced slave in the same epoch accepts a beacon only if sf is in (last_sf, last_sf + MAX_SF_JUMP].
// Normal operation never exceeds a jump of 1 because free-running advances last_sf too (a slave
// that misses k < MAX_MISSED_BEACONS beacons has free-run k superframes and sees sf == last+1
// next). 8 = MAX_MISSED_BEACONS + 3 gives slack over the worst legitimate gap (MAX_MISSED+1) while
// keeping a stray CRC-valid beacon from pushing last_sf far ahead of the master. A LOST or
// freshly booted slave accepts any sf.
constexpr uint32_t MAX_SF_JUMP         = 8;
static_assert(MAX_SF_JUMP > MAX_MISSED_BEACONS + 1, "MAX_SF_JUMP must cover the longest legitimate gap");

// Worst-case end of polling = start + every slave uses every attempt's full timeout.
constexpr uint32_t POLL_WORST_END_US =
    POLL_START_OFFSET_US + NUM_SLAVES * POLL_ATTEMPTS * REPLY_TIMEOUT_US;
static_assert(POLL_START_OFFSET_US > SAMPLE_SPAN_US, "polling must start after the last sample");
static_assert(POLL_WORST_END_US < SUPERFRAME_US, "polling must finish before the next beacon");

constexpr size_t MAX_PAYLOAD = 250;  // ESP-NOW limit

constexpr uint8_t MAGIC   = 0xA7;
constexpr uint8_t VERSION = 3;  // 2: master epoch added to BEACON/POLL/REPORT; 3: REPORT carries ae_ratio, f1_est, f1_drop + 4 flag bits
constexpr uint8_t ID_MASTER    = 0;
constexpr uint8_t ID_BROADCAST = 0xFF;

enum PktType : uint8_t { T_BEACON = 1, T_POLL = 2, T_REPORT = 3 };

// Report flag bits (bits 6..7 reserved, send as 0).
constexpr uint8_t FLAG_LOW_SIGNAL = 0x01;  // noise gate fired (az_std below NOISE_GATE_COUNTS)
constexpr uint8_t FLAG_UNSYNCED   = 0x02;  // no beacon was received for this superframe
// Version 3 (slave analytics, see slave_pipe.h): the three extra floats are meaningful only with their flag.
constexpr uint8_t FLAG_AE_READY          = 0x04;  // ae_ratio is valid (the slave's 20 commissioning windows are done)
constexpr uint8_t FLAG_F1_VALID          = 0x08;  // f1_est is a valid estimate of this window
constexpr uint8_t FLAG_F1_COMMISSIONED   = 0x10;  // the f1 tracker has its f1_0; with F1_VALID, f1_drop is valid
constexpr uint8_t FLAG_F1_NOT_COMMISSIONED = 0x20;  // f1 commissioning is overdue (f1 outside 3..12 Hz, first mode invisible, unstable)
constexpr uint8_t FLAGS_VALID     = FLAG_LOW_SIGNAL | FLAG_UNSYNCED | FLAG_AE_READY | FLAG_F1_VALID | FLAG_F1_COMMISSIONED |
                                    FLAG_F1_NOT_COMMISSIONED;  // any other bit set => report rejected
constexpr uint8_t NUM_CLASSES     = 3;     // raw class and smoothed state must be < NUM_CLASSES

constexpr size_t HDR_LEN = 7;
constexpr size_t CRC_LEN = 2;
constexpr size_t BEACON_LEN = HDR_LEN + 4 + 4 + CRC_LEN;             // sf, epoch
constexpr size_t POLL_LEN   = HDR_LEN + 4 + 4 + 1 + CRC_LEN;         // sf, epoch, attempt
constexpr size_t REPORT_LEN = HDR_LEN + 4 + 4 + 4 + 1 + 1 + 1 + 5 * 4 + 3 * 4 + CRC_LEN;  // sf, epoch, window, raw, state, flags, 5 floats, ae_ratio, f1_est, f1_drop
static_assert(REPORT_LEN == 56, "wire version 3 report is 56 bytes (44 in version 2)");
static_assert(BEACON_LEN <= MAX_PAYLOAD, "beacon too large");
static_assert(POLL_LEN <= MAX_PAYLOAD, "poll too large");
static_assert(REPORT_LEN <= MAX_PAYLOAD, "report too large");
static_assert(sizeof(float) == 4, "float must be 4 bytes");

// ---------------------------------------------------------------- CRC
// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout. check("123456789") = 0x29B1.
static inline uint16_t crc16(const uint8_t *d, size_t n) {
    uint16_t c = 0xFFFF;
    for (size_t i = 0; i < n; i++) {
        c ^= (uint16_t)((uint16_t)d[i] << 8);
        for (int b = 0; b < 8; b++) c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
    }
    return c;
}

// ---------------------------------------------------------------- structs
struct Header {
    uint8_t type, version, src, dst;
    uint16_t seq;
};
struct BeaconPkt {
    Header h;
    uint32_t sf;
    uint32_t epoch;
};
struct PollPkt {
    Header h;
    uint32_t sf;
    uint32_t epoch;
    uint8_t attempt;
};
struct ReportPkt {
    Header h;
    uint32_t sf;      // superframe the window belongs to
    uint32_t epoch;   // master epoch this report answers
    uint32_t window;  // slave's running window counter
    uint8_t raw;      // raw class after the noise gate
    uint8_t state;    // smoothed state
    uint8_t flags;
    float f[5];       // az_std, az_kurt, az_domfreq, gx_std, gx_domfreq
    float ae_ratio;   // AE score / the slave's median commissioning score (valid iff FLAG_AE_READY)
    float f1_est;     // first natural frequency estimate, Hz (valid iff FLAG_F1_VALID)
    float f1_drop;    // (f1_0 - f1_est) / f1_0 (valid iff FLAG_F1_VALID && FLAG_F1_COMMISSIONED)
};

// ---------------------------------------------------------------- encode
// Buffers passed to encode_* must hold at least MAX_PAYLOAD bytes. Return the frame length.
static inline size_t put_header(uint8_t *b, uint8_t type, uint8_t src, uint8_t dst, uint16_t seq) {
    b[0] = MAGIC;
    b[1] = type;
    b[2] = VERSION;
    b[3] = src;
    b[4] = dst;
    memcpy(b + 5, &seq, 2);
    return HDR_LEN;
}
static inline size_t put_crc(uint8_t *b, size_t n) {
    uint16_t c = crc16(b, n);
    memcpy(b + n, &c, 2);
    return n + CRC_LEN;
}
static inline size_t encode_beacon(uint8_t *b, uint8_t src, uint8_t dst, uint16_t seq, uint32_t sf, uint32_t epoch) {
    size_t n = put_header(b, T_BEACON, src, dst, seq);
    memcpy(b + n, &sf, 4);
    n += 4;
    memcpy(b + n, &epoch, 4);
    n += 4;
    return put_crc(b, n);
}
static inline size_t encode_poll(uint8_t *b, uint8_t src, uint8_t dst, uint16_t seq, uint32_t sf, uint32_t epoch,
                                 uint8_t attempt) {
    size_t n = put_header(b, T_POLL, src, dst, seq);
    memcpy(b + n, &sf, 4);
    n += 4;
    memcpy(b + n, &epoch, 4);
    n += 4;
    b[n++] = attempt;
    return put_crc(b, n);
}
static inline size_t encode_report(uint8_t *b, uint8_t src, uint8_t dst, uint16_t seq, const ReportPkt &r) {
    size_t n = put_header(b, T_REPORT, src, dst, seq);
    memcpy(b + n, &r.sf, 4);
    n += 4;
    memcpy(b + n, &r.epoch, 4);
    n += 4;
    memcpy(b + n, &r.window, 4);
    n += 4;
    b[n++] = r.raw;
    b[n++] = r.state;
    b[n++] = r.flags;
    memcpy(b + n, r.f, 20);
    n += 20;
    memcpy(b + n, &r.ae_ratio, 4);
    n += 4;
    memcpy(b + n, &r.f1_est, 4);
    n += 4;
    memcpy(b + n, &r.f1_drop, 4);
    n += 4;
    return put_crc(b, n);
}

// ---------------------------------------------------------------- decode
// Validates length bounds, CRC, magic and version. Does not check addressing.
static inline bool decode_header(const uint8_t *b, size_t len, Header *h) {
    if (b == nullptr || len < HDR_LEN + CRC_LEN || len > MAX_PAYLOAD) return false;
    uint16_t want;
    memcpy(&want, b + len - CRC_LEN, 2);
    if (crc16(b, len - CRC_LEN) != want) return false;
    if (b[0] != MAGIC || b[2] != VERSION) return false;
    h->type = b[1];
    h->version = b[2];
    h->src = b[3];
    h->dst = b[4];
    memcpy(&h->seq, b + 5, 2);
    return true;
}
static inline bool decode_beacon(const uint8_t *b, size_t len, BeaconPkt *p) {
    if (len != BEACON_LEN || !decode_header(b, len, &p->h) || p->h.type != T_BEACON) return false;
    memcpy(&p->sf, b + HDR_LEN, 4);
    memcpy(&p->epoch, b + HDR_LEN + 4, 4);
    return true;
}
static inline bool decode_poll(const uint8_t *b, size_t len, PollPkt *p) {
    if (len != POLL_LEN || !decode_header(b, len, &p->h) || p->h.type != T_POLL) return false;
    memcpy(&p->sf, b + HDR_LEN, 4);
    memcpy(&p->epoch, b + HDR_LEN + 4, 4);
    p->attempt = b[HDR_LEN + 8];
    return true;
}
static inline bool decode_report(const uint8_t *b, size_t len, ReportPkt *r) {
    if (len != REPORT_LEN || !decode_header(b, len, &r->h) || r->h.type != T_REPORT) return false;
    size_t n = HDR_LEN;
    memcpy(&r->sf, b + n, 4);
    n += 4;
    memcpy(&r->epoch, b + n, 4);
    n += 4;
    memcpy(&r->window, b + n, 4);
    n += 4;
    r->raw = b[n++];
    r->state = b[n++];
    r->flags = b[n++];
    memcpy(r->f, b + n, 20);
    n += 20;
    memcpy(&r->ae_ratio, b + n, 4);
    n += 4;
    memcpy(&r->f1_est, b + n, 4);
    n += 4;
    memcpy(&r->f1_drop, b + n, 4);
    return true;
}

}  // namespace net
