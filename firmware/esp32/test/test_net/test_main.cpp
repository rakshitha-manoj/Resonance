#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <vector>
#include <string>
#include <unity.h>
#include "features.h"
#include "smooth.h"
#include "net_proto.h"
#include "tiers.h"
#include "netsim.h"

using namespace net;

void setUp(void) {}
void tearDown(void) {}

static const uint32_t EP = 0xC0FFEE01u;   // master epoch used by the rigs
static const uint32_t EP2 = 0x0BADF00Du;  // a different one ("master restarted")

// ------------------------------------------------------------------ helpers
struct Sent {
    uint64_t t;
    std::vector<uint8_t> b;
};
struct FakeTx : Transport {
    std::vector<Sent> sent;
    int fail_next = 0;  // the next N broadcasts fail
    bool broadcast(const uint8_t *buf, size_t len, uint64_t now) override {
        if (fail_next > 0) { fail_next--; return false; }
        Sent s;
        s.t = now;
        s.b.assign(buf, buf + len);
        sent.push_back(s);
        return true;
    }
};

struct Win {
    int16_t az[128], gx[128];
    int32_t cls;
};
static std::vector<Win> g_win;
static void load_windows() {
    if (!g_win.empty()) return;
    FILE *fp = fopen(WINDOWS_BIN, "rb");
    TEST_ASSERT_NOT_NULL(fp);
    std::vector<unsigned char> buf(556);
    while (fread(buf.data(), 1, 556, fp) == 556) {
        Win w;
        memcpy(w.az, buf.data(), 256);
        memcpy(w.gx, buf.data() + 256, 256);
        memcpy(&w.cls, buf.data() + 512, 4);
        g_win.push_back(w);
    }
    fclose(fp);
    TEST_ASSERT_EQUAL_INT(6000, (int)g_win.size());
}

static ReportPkt sample_report() {
    ReportPkt r;
    memset(&r, 0, sizeof r);
    r.h.src = 2;
    r.h.dst = ID_MASTER;
    r.h.seq = 513;
    r.sf = 0x01020304;
    r.epoch = 0x0A0B0C0D;
    r.window = 0xA0B0C0D0;
    r.raw = 2;
    r.state = 1;
    r.flags = FLAG_LOW_SIGNAL | FLAG_UNSYNCED | FLAG_AE_READY | FLAG_F1_VALID | FLAG_F1_COMMISSIONED;
    const float v[5] = {1.5f, -2.25f, 3.125f, 1e6f, 0.78125f};
    for (int i = 0; i < 5; i++) r.f[i] = v[i];
    r.ae_ratio = 7.5f;
    r.f1_est = 5.25f;
    r.f1_drop = 0.0125f;
    return r;
}

// A well-formed report as a slave would send it.
static ReportPkt good_report(uint32_t sf, uint8_t state = 0, uint32_t epoch = EP) {
    ReportPkt r = sample_report();
    r.sf = sf;
    r.epoch = epoch;
    r.state = state;
    r.raw = state;
    r.flags = 0;
    return r;
}
static size_t enc(uint8_t *b, const ReportPkt &r, uint8_t src, uint8_t dst = ID_MASTER) { return encode_report(b, src, dst, 0, r); }
static size_t mk_report(uint8_t *b, uint8_t slave, uint32_t sf, uint8_t state = 0, uint32_t epoch = EP) {
    return enc(b, good_report(sf, state, epoch), slave);
}

// Append `extra` zero bytes before the CRC (or remove -extra bytes) and re-seal with a valid CRC.
static size_t resize_valid_crc(uint8_t *b, size_t n, int extra) {
    size_t body = n - CRC_LEN;
    if (extra > 0) memset(b + body, 0, (size_t)extra);
    size_t nb = body + (size_t)extra;
    uint16_t c = crc16(b, nb);
    memcpy(b + nb, &c, 2);
    return nb + CRC_LEN;
}

// ------------------------------------------------------------------ protocol
void test_crc_check_value(void) {
    TEST_ASSERT_EQUAL_HEX16(0x29B1, crc16((const uint8_t *)"123456789", 9));
}

void test_sizes(void) {
    printf("packet sizes: beacon=%u poll=%u report=%u (max %u)\n", (unsigned)BEACON_LEN, (unsigned)POLL_LEN,
           (unsigned)REPORT_LEN, (unsigned)MAX_PAYLOAD);
    uint8_t b[MAX_PAYLOAD];
    TEST_ASSERT_EQUAL_UINT(BEACON_LEN, encode_beacon(b, 0, ID_BROADCAST, 1, 7, EP));
    TEST_ASSERT_EQUAL_UINT(POLL_LEN, encode_poll(b, 0, 1, 1, 7, EP, 1));
    TEST_ASSERT_EQUAL_UINT(REPORT_LEN, encode_report(b, 1, 0, 1, sample_report()));
    // MAX_SF_JUMP is a tuning constant: keep it above the longest legitimate gap and small.
    TEST_ASSERT_TRUE(MAX_SF_JUMP >= MAX_MISSED_BEACONS + 2 && MAX_SF_JUMP <= 16);
    TEST_ASSERT_EQUAL_UINT(17, BEACON_LEN);
    TEST_ASSERT_EQUAL_UINT(18, POLL_LEN);
    TEST_ASSERT_EQUAL_UINT(56, REPORT_LEN);   // wire version 3: 44 + ae_ratio, f1_est, f1_drop
    TEST_ASSERT_EQUAL_UINT8(3, VERSION);
    TEST_ASSERT_TRUE(REPORT_LEN <= MAX_PAYLOAD);
    // polling still fits: the timing constants are unchanged and a 56-byte reply is far below the 18 ms reply timeout
    TEST_ASSERT_TRUE(POLL_WORST_END_US < SUPERFRAME_US);
    TEST_ASSERT_EQUAL_UINT32(1400000, SUPERFRAME_US);
    TEST_ASSERT_EQUAL_UINT32(18000, REPLY_TIMEOUT_US);
}

void test_roundtrip(void) {
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_beacon(b, 0, ID_BROADCAST, 77, 123456, 0xDEADBEEF);
    BeaconPkt bp;
    TEST_ASSERT_TRUE(decode_beacon(b, n, &bp));
    TEST_ASSERT_EQUAL_UINT32(123456, bp.sf);
    TEST_ASSERT_EQUAL_HEX32(0xDEADBEEF, bp.epoch);
    TEST_ASSERT_EQUAL_UINT8(0, bp.h.src);
    TEST_ASSERT_EQUAL_UINT8(ID_BROADCAST, bp.h.dst);
    TEST_ASSERT_EQUAL_UINT16(77, bp.h.seq);
    TEST_ASSERT_EQUAL_UINT8(T_BEACON, bp.h.type);
    { PollPkt wrong; TEST_ASSERT_FALSE(decode_poll(b, n, &wrong)); }  // wrong type

    n = encode_poll(b, 0, 2, 78, 99, 0x12345678, 1);
    PollPkt pp;
    TEST_ASSERT_TRUE(decode_poll(b, n, &pp));
    TEST_ASSERT_EQUAL_UINT32(99, pp.sf);
    TEST_ASSERT_EQUAL_HEX32(0x12345678, pp.epoch);
    TEST_ASSERT_EQUAL_UINT8(1, pp.attempt);
    TEST_ASSERT_EQUAL_UINT8(2, pp.h.dst);

    ReportPkt r = sample_report();
    n = encode_report(b, 2, 0, 513, r);
    ReportPkt o;
    TEST_ASSERT_TRUE(decode_report(b, n, &o));
    TEST_ASSERT_EQUAL_UINT32(r.sf, o.sf);
    TEST_ASSERT_EQUAL_HEX32(r.epoch, o.epoch);
    TEST_ASSERT_EQUAL_UINT32(r.window, o.window);
    TEST_ASSERT_EQUAL_UINT8(r.raw, o.raw);
    TEST_ASSERT_EQUAL_UINT8(r.state, o.state);
    TEST_ASSERT_EQUAL_UINT8(r.flags, o.flags);
    TEST_ASSERT_EQUAL_UINT8(2, o.h.src);
    TEST_ASSERT_EQUAL_UINT16(513, o.h.seq);
    for (int i = 0; i < 5; i++) TEST_ASSERT_EQUAL_MEMORY(&r.f[i], &o.f[i], 4);
    TEST_ASSERT_EQUAL_MEMORY(&r.ae_ratio, &o.ae_ratio, 4);
    TEST_ASSERT_EQUAL_MEMORY(&r.f1_est, &o.f1_est, 4);
    TEST_ASSERT_EQUAL_MEMORY(&r.f1_drop, &o.f1_drop, 4);
    // the new floats sit at the end of the payload, little endian, before the CRC
    TEST_ASSERT_EQUAL_UINT(56, n);
    float x;
    memcpy(&x, b + n - CRC_LEN - 12, 4);
    TEST_ASSERT_EQUAL_FLOAT(7.5f, x);
    memcpy(&x, b + n - CRC_LEN - 8, 4);
    TEST_ASSERT_EQUAL_FLOAT(5.25f, x);
    memcpy(&x, b + n - CRC_LEN - 4, 4);
    TEST_ASSERT_EQUAL_FLOAT(0.0125f, x);
}

// Version 3 on the wire: a version-2 frame (44 bytes, header version 2) is not accepted, whatever its CRC.
void test_report_v2_frame_rejected_and_flag_bits(void) {
    uint8_t b[MAX_PAYLOAD], c[MAX_PAYLOAD];
    size_t n = encode_report(b, 1, 0, 1, sample_report());
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(b, n, &rp));
    // old-length frame with a valid CRC and the old header version
    size_t m = n - 12;
    memcpy(c, b, m - CRC_LEN);
    c[2] = 2;
    uint16_t crc = crc16(c, m - CRC_LEN);
    memcpy(c + m - CRC_LEN, &crc, 2);
    TEST_ASSERT_EQUAL_UINT(44, m);
    TEST_ASSERT_FALSE(decode_report(c, m, &rp));
    // new length, old version byte
    memcpy(c, b, n);
    c[2] = 2;
    crc = crc16(c, n - CRC_LEN);
    memcpy(c + n - CRC_LEN, &crc, 2);
    TEST_ASSERT_FALSE(decode_report(c, n, &rp));
    // flag bits: six defined, two reserved
    TEST_ASSERT_EQUAL_HEX8(0x3F, FLAGS_VALID);
    TEST_ASSERT_EQUAL_HEX8(0x04, FLAG_AE_READY);
    TEST_ASSERT_EQUAL_HEX8(0x08, FLAG_F1_VALID);
    TEST_ASSERT_EQUAL_HEX8(0x10, FLAG_F1_COMMISSIONED);
    TEST_ASSERT_EQUAL_HEX8(0x20, FLAG_F1_NOT_COMMISSIONED);
    for (int bit = 0; bit < 8; bit++) {
        ReportPkt p = good_report(0);
        p.flags = (uint8_t)(1u << bit);
        TEST_ASSERT_EQUAL_INT(bit < 6 ? 1 : 0, report_fields_ok(p) ? 1 : 0);
    }
}

static void expect_all_rejected(const uint8_t *b, size_t n, int kind) {
    uint8_t c[MAX_PAYLOAD];
    for (size_t i = 0; i < n; i++)
        for (int bit = 0; bit < 8; bit++) {
            memcpy(c, b, n);
            c[i] ^= (uint8_t)(1u << bit);
            bool ok;
            if (kind == 0) { BeaconPkt p; ok = decode_beacon(c, n, &p); }
            else if (kind == 1) { PollPkt p; ok = decode_poll(c, n, &p); }
            else { ReportPkt p; ok = decode_report(c, n, &p); }
            if (ok) { printf("accepted flip byte %u bit %d kind %d\n", (unsigned)i, bit, kind); }
            TEST_ASSERT_FALSE(ok);
        }
}

void test_bitflip_rejected(void) {
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_beacon(b, 0, ID_BROADCAST, 1, 5, EP);
    expect_all_rejected(b, n, 0);
    n = encode_poll(b, 0, 1, 1, 5, EP, 0);
    expect_all_rejected(b, n, 1);
    n = encode_report(b, 1, 0, 1, sample_report());
    expect_all_rejected(b, n, 2);
}

void test_version_and_length(void) {
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_poll(b, 0, 1, 1, 5, EP, 0);
    // Wrong version with a valid CRC must still be rejected.
    b[2] = VERSION + 1;
    uint16_t c = crc16(b, n - 2);
    memcpy(b + n - 2, &c, 2);
    PollPkt p;
    TEST_ASSERT_FALSE(decode_poll(b, n, &p));
    // Wrong magic with a valid CRC.
    n = encode_poll(b, 0, 1, 1, 5, EP, 0);
    b[0] ^= 0xFF;
    c = crc16(b, n - 2);
    memcpy(b + n - 2, &c, 2);
    TEST_ASSERT_FALSE(decode_poll(b, n, &p));
    // Truncation / extension at every length.
    uint8_t r[MAX_PAYLOAD];
    size_t rn = encode_report(r, 1, 0, 1, sample_report());
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(r, rn, &rp));
    for (size_t l = 0; l < rn; l++) TEST_ASSERT_FALSE(decode_report(r, l, &rp));
    r[rn] = 0;
    TEST_ASSERT_FALSE(decode_report(r, rn + 1, &rp));
    TEST_ASSERT_FALSE(decode_report(nullptr, 0, &rp));
    TEST_ASSERT_FALSE(decode_report(r, 251, &rp));
}

// A frame with a VALID CRC but the wrong length must be rejected by length alone.
void test_wrong_length_with_valid_crc(void) {
    uint8_t b[MAX_PAYLOAD + 60], c[MAX_PAYLOAD + 60];
    // decode_header bounds: shortest legal frame is HDR_LEN + CRC_LEN; longest is MAX_PAYLOAD.
    for (size_t len = 2; len < HDR_LEN + CRC_LEN; len++) {
        memset(c, 0, sizeof c);
        c[0] = MAGIC; c[1] = T_BEACON; c[2] = VERSION;  // would pass magic/version if the length let it
        uint16_t cr = crc16(c, len - CRC_LEN);
        memcpy(c + len - CRC_LEN, &cr, 2);
        Header h;
        if (len >= 5) TEST_ASSERT_FALSE_MESSAGE(decode_header(c, len, &h), "short frame with valid CRC accepted");
    }
    for (size_t len = MAX_PAYLOAD + 1; len <= MAX_PAYLOAD + 40; len += 13) {
        memset(c, 0, sizeof c);
        c[0] = MAGIC; c[1] = T_BEACON; c[2] = VERSION;
        uint16_t cr = crc16(c, len - CRC_LEN);
        memcpy(c + len - CRC_LEN, &cr, 2);
        Header h;
        TEST_ASSERT_FALSE_MESSAGE(decode_header(c, len, &h), "over-long frame with valid CRC accepted");
    }
    // Each typed decoder wants its exact length: +/-1..+40 bytes, CRC re-sealed, must fail.
    for (int d = -9; d <= 40; d++) {
        if (d == 0) continue;
        size_t n = encode_beacon(b, 0, ID_BROADCAST, 0, 3, EP);
        if ((int)n + d >= (int)(HDR_LEN + CRC_LEN)) {
            memcpy(c, b, n);
            size_t m = resize_valid_crc(c, n, d);
            BeaconPkt p;
            TEST_ASSERT_FALSE(decode_beacon(c, m, &p));
        }
        n = encode_poll(b, 0, 1, 0, 3, EP, 0);
        if ((int)n + d >= (int)(HDR_LEN + CRC_LEN)) {
            memcpy(c, b, n);
            size_t m = resize_valid_crc(c, n, d);
            PollPkt p;
            TEST_ASSERT_FALSE(decode_poll(c, m, &p));
        }
        n = encode_report(b, 1, 0, 0, sample_report());
        if ((int)n + d >= (int)(HDR_LEN + CRC_LEN)) {
            memcpy(c, b, n);
            size_t m = resize_valid_crc(c, n, d);
            ReportPkt p;
            TEST_ASSERT_FALSE(decode_report(c, m, &p));
        }
    }
}

// A frame of the right length and valid CRC but the wrong type must not decode as another type.
void test_wrong_type_with_valid_crc(void) {
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_beacon(b, 0, ID_BROADCAST, 0, 3, EP);
    b[1] = T_POLL;
    resize_valid_crc(b, n, 0);
    BeaconPkt bp;
    TEST_ASSERT_FALSE(decode_beacon(b, n, &bp));
    n = encode_poll(b, 0, 1, 0, 3, EP, 0);
    b[1] = T_BEACON;
    resize_valid_crc(b, n, 0);
    PollPkt pp;
    TEST_ASSERT_FALSE(decode_poll(b, n, &pp));
    n = encode_report(b, 1, 0, 0, sample_report());
    b[1] = T_POLL;
    resize_valid_crc(b, n, 0);
    ReportPkt rp;
    TEST_ASSERT_FALSE(decode_report(b, n, &rp));
}

// ------------------------------------------------------------------ noise gate
void test_gate_threshold(void) {
    float f[5] = {0, 0, 0, 0, 0};
    uint8_t fl;
    f[0] = nextafterf(NOISE_GATE_COUNTS, 0.0f);
    fl = 0;
    TEST_ASSERT_EQUAL_INT(0, apply_noise_gate(f, 2, &fl));
    TEST_ASSERT_EQUAL_HEX8(FLAG_LOW_SIGNAL, fl);
    f[0] = NOISE_GATE_COUNTS;  // exactly at: not below, passes through
    fl = 0;
    TEST_ASSERT_EQUAL_INT(2, apply_noise_gate(f, 2, &fl));
    TEST_ASSERT_EQUAL_HEX8(0, fl);
    f[0] = nextafterf(NOISE_GATE_COUNTS, 1e9f);
    fl = 0;
    TEST_ASSERT_EQUAL_INT(1, apply_noise_gate(f, 1, &fl));
    TEST_ASSERT_EQUAL_HEX8(0, fl);
    f[0] = 0.0f;
    fl = FLAG_UNSYNCED;
    TEST_ASSERT_EQUAL_INT(0, apply_noise_gate(f, 2, &fl));
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED | FLAG_LOW_SIGNAL, fl);
}

void test_gate_unchanged_on_dataset(void) {
    load_windows();
    int changed = 0, flagged = 0, below = 0;
    float min_std = 1e30f;
    for (size_t i = 0; i < g_win.size(); i++) {
        float f[5];
        features(g_win[i].az, g_win[i].gx, f);
        int raw = classify(f);
        uint8_t fl = 0;
        int g = apply_noise_gate(f, raw, &fl);
        if (g != raw) changed++;
        if (fl) flagged++;
        if (f[0] < NOISE_GATE_COUNTS) below++;
        if (f[0] < min_std) min_std = f[0];
        // Above the gate the tree's result is exactly what comes out (and matches the stored label).
        if (f[0] >= NOISE_GATE_COUNTS) {
            TEST_ASSERT_EQUAL_INT(raw, g);
            TEST_ASSERT_EQUAL_INT(g_win[i].cls, g);
        }
    }
    printf("gate on %u dataset windows: min az_std=%.3f below_gate=%d changed=%d flagged=%d\n", (unsigned)g_win.size(),
           (double)min_std, below, changed, flagged);
    TEST_ASSERT_EQUAL_INT(0, below);
    TEST_ASSERT_EQUAL_INT(0, changed);
    TEST_ASSERT_EQUAL_INT(0, flagged);
}

// ------------------------------------------------------------------ slave
struct SlaveRig {
    FakeTx tx;
    SlaveNode s;
    uint32_t sf;
    SlaveRig() : s(1, &tx), sf(0) {}
    // Deliver a beacon at t, take the window, deliver data completed at t+span+30ms. Returns completion time.
    uint64_t run_window(uint64_t t, const int16_t *az, const int16_t *gx, uint32_t sf_n, uint32_t epoch = EP) {
        uint8_t b[MAX_PAYLOAD];
        size_t n = encode_beacon(b, 0, ID_BROADCAST, 0, sf_n, epoch);
        s.on_packet(b, n, t);
        TEST_ASSERT_TRUE(s.window_requested());
        uint32_t tok = s.take_window_request();
        TEST_ASSERT_NOT_EQUAL_UINT32(0, tok);
        TEST_ASSERT_EQUAL_UINT64(t, s.window_start());
        TEST_ASSERT_FALSE(s.window_requested());
        uint64_t done = t + SAMPLE_SPAN_US + 30000;
        TEST_ASSERT_TRUE(s.deliver_window(tok, az, gx, done));
        return done;
    }
    void beacon(uint64_t t, uint32_t sf_n, uint32_t epoch = EP) {
        uint8_t b[MAX_PAYLOAD];
        size_t n = encode_beacon(b, 0, ID_BROADCAST, 0, sf_n, epoch);
        s.on_packet(b, n, t);
    }
    size_t poll(uint8_t dst, uint32_t sf_n, uint8_t att, uint64_t t, uint32_t epoch = EP) {
        uint8_t b[MAX_PAYLOAD];
        size_t n = encode_poll(b, 0, dst, 0, sf_n, epoch, att);
        size_t before = tx.sent.size();
        s.on_packet(b, n, t);
        return tx.sent.size() - before;
    }
};

void test_slave_addressing_and_ready(void) {
    load_windows();
    SlaveRig r;
    // Poll before any window: silent. Also for the all-zero poll that matches a freshly booted
    // slave's zeroed cache (sf 0, epoch 0): there is no report yet.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, 100));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, 100, 0));
    TEST_ASSERT_EQUAL_UINT32(0, r.s.stats().polls_answered);
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_beacon(b, 0, ID_BROADCAST, 0, 0, EP);
    r.s.on_packet(b, n, 1000);
    uint32_t tok = r.s.take_window_request();
    // Sampling/computing: poll for this superframe is silent.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, 1000 + 500000));
    TEST_ASSERT_TRUE(r.s.deliver_window(tok, g_win[0].az, g_win[0].gx, 1400000));
    // Wrong destination silent, right one answers.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(2, 0, 0, 1400100));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(ID_BROADCAST, 0, 0, 1400100));
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 0, 0, 1400200));
    // Wrong superframe silent.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 1, 0, 1400300));
    // Corrupted poll silent.
    size_t pn = encode_poll(b, 0, 1, 0, 0, EP, 0);
    b[3] ^= 1;
    size_t before = r.tx.sent.size();
    r.s.on_packet(b, pn, 1400400);
    TEST_ASSERT_EQUAL_UINT(before, r.tx.sent.size());
    // Poll "from" a slave (src != master) ignored.
    pn = encode_poll(b, 2, 1, 0, 0, EP, 0);
    r.s.on_packet(b, pn, 1400500);
    TEST_ASSERT_EQUAL_UINT(before, r.tx.sent.size());
    // The answer is a valid report addressed to the master.
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(r.tx.sent[0].b.data(), r.tx.sent[0].b.size(), &rp));
    TEST_ASSERT_EQUAL_UINT8(1, rp.h.src);
    TEST_ASSERT_EQUAL_UINT8(ID_MASTER, rp.h.dst);
    TEST_ASSERT_EQUAL_UINT32(0, rp.sf);
    TEST_ASSERT_EQUAL_HEX32(EP, rp.epoch);
    // A beacon addressed to someone else is ignored.
    SlaveRig r2;
    n = encode_beacon(b, 0, 2, 0, 0, EP);
    r2.s.on_packet(b, n, 0);
    TEST_ASSERT_FALSE(r2.s.window_requested());
}

void test_slave_retry_and_smoothing(void) {
    load_windows();
    SlaveRig r;
    int8_t ref_hist[3] = {-1, -1, -1};
    int ref_state = 0;
    uint64_t t = 0;
    int mismatches_state = 0;
    // 30 windows spread across the dataset (mixed classes), each polled once and retried 0..2 times.
    for (uint32_t k = 0; k < 30; k++) {
        const Win &w = g_win[(k * 199) % g_win.size()];
        uint64_t done = r.run_window(t, w.az, w.gx, k);
        float f[5];
        features(w.az, w.gx, f);
        int raw = classify(f);
        ref_state = smooth_update(ref_hist, raw, ref_state);
        size_t tx0 = r.tx.sent.size();
        uint32_t retries = k % 3;
        for (uint32_t a = 0; a <= retries; a++) TEST_ASSERT_EQUAL_UINT(1, r.poll(1, k, (uint8_t)a, done + 10000 + a * 20000));
        TEST_ASSERT_EQUAL_UINT(tx0 + retries + 1, r.tx.sent.size());
        // Identical content for every answer to the same superframe.
        ReportPkt first, other;
        TEST_ASSERT_TRUE(decode_report(r.tx.sent[tx0].b.data(), r.tx.sent[tx0].b.size(), &first));
        for (uint32_t a = 1; a <= retries; a++) {
            TEST_ASSERT_TRUE(decode_report(r.tx.sent[tx0 + a].b.data(), r.tx.sent[tx0 + a].b.size(), &other));
            TEST_ASSERT_EQUAL_UINT32(first.sf, other.sf);
            TEST_ASSERT_EQUAL_UINT32(first.window, other.window);
            TEST_ASSERT_EQUAL_UINT8(first.raw, other.raw);
            TEST_ASSERT_EQUAL_UINT8(first.state, other.state);
            TEST_ASSERT_EQUAL_UINT8(first.flags, other.flags);
            TEST_ASSERT_EQUAL_MEMORY(first.f, other.f, 20);
        }
        TEST_ASSERT_EQUAL_UINT32(k, first.sf);
        TEST_ASSERT_EQUAL_UINT32(k, first.window);
        TEST_ASSERT_EQUAL_UINT8(raw, first.raw);
        if (first.state != ref_state) mismatches_state++;
        TEST_ASSERT_EQUAL_UINT8(ref_state, first.state);
        TEST_ASSERT_EQUAL_MEMORY(ref_hist, r.s.smooth_hist(), 3);
        TEST_ASSERT_EQUAL_UINT32(k + 1, r.s.windows_done());  // smoothing advanced exactly once per superframe
        t += SUPERFRAME_US;
    }
    TEST_ASSERT_EQUAL_INT(0, mismatches_state);
}

void test_slave_low_signal_flag(void) {
    SlaveRig r;
    int16_t az[128], gx[128];
    for (int i = 0; i < 128; i++) { az[i] = 16384; gx[i] = 0; }
    uint64_t done = r.run_window(0, az, gx, 0);
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 0, 0, done + 1000));
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(r.tx.sent[0].b.data(), r.tx.sent[0].b.size(), &rp));
    TEST_ASSERT_EQUAL_UINT8(0, rp.raw);
    TEST_ASSERT_EQUAL_HEX8(FLAG_LOW_SIGNAL, rp.flags);
}

// The gate must act on the raw class BEFORE it enters the smoothing history. Low-signal windows
// whose ungated class is non-normal would otherwise drive the smoothed state away from normal.
void test_slave_gate_before_smoothing(void) {
    SlaveRig r;
    int16_t az[128], gx[128];
    // Weak 50 Hz-ish alternation: az_std ~ 1 (below the gate); ungated tree class must be non-zero.
    for (int i = 0; i < 128; i++) { az[i] = (int16_t)(16384 + ((i & 1) ? 1 : -1)); gx[i] = (int16_t)((i & 1) ? 2 : -2); }
    float f[5];
    features(az, gx, f);
    TEST_ASSERT_TRUE(f[0] < NOISE_GATE_COUNTS);
    TEST_ASSERT_NOT_EQUAL_INT_MESSAGE(0, classify(f), "test precondition: ungated class must be non-normal");
    uint64_t t = 0;
    for (uint32_t k = 0; k < 4; k++) {
        r.run_window(t, az, gx, k);
        TEST_ASSERT_EQUAL_INT(0, r.s.smooth_state());                       // gated => normal => state stays 0
        TEST_ASSERT_EQUAL_INT8(0, r.s.smooth_hist()[2]);                    // newest history entry is the GATED class
        TEST_ASSERT_EQUAL_UINT8(0, r.s.report().raw);
        TEST_ASSERT_EQUAL_UINT8(0, r.s.report().state);
        TEST_ASSERT_EQUAL_HEX8(FLAG_LOW_SIGNAL, r.s.report().flags);
        t += SUPERFRAME_US;
    }
}

void test_slave_unsynced_free_run_and_limit(void) {
    load_windows();
    SlaveRig r;
    r.run_window(0, g_win[0].az, g_win[0].gx, 0);
    // No beacon for superframe 1: free-runs after expected + grace.
    uint64_t due = SUPERFRAME_US + BEACON_GRACE_US;
    TEST_ASSERT_EQUAL_UINT64(due, r.s.next_wakeup());
    r.s.tick(due - 1);
    TEST_ASSERT_FALSE(r.s.window_requested());
    uint64_t late_tick = due + 3000;  // the host ticked 3 ms late
    r.s.tick(late_tick);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(1, r.s.window_sf());
    TEST_ASSERT_EQUAL_UINT64(late_tick, r.s.window_start());
    uint32_t tok = r.s.take_window_request();
    TEST_ASSERT_TRUE(r.s.deliver_window(tok, g_win[5].az, g_win[5].gx, late_tick + SAMPLE_SPAN_US + 30000));
    // No drift: the next expectation stays on the old grid, not "tick time + period".
    TEST_ASSERT_EQUAL_UINT64(2 * (uint64_t)SUPERFRAME_US + BEACON_GRACE_US, r.s.next_wakeup());
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 1, 0, late_tick + SAMPLE_SPAN_US + 40000));
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(r.tx.sent.back().b.data(), r.tx.sent.back().b.size(), &rp));
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED, rp.flags & FLAG_UNSYNCED);
    // Keep losing beacons: exactly MAX_MISSED_BEACONS free-run windows in total, then LOST.
    uint32_t windows = 1;
    uint64_t expect = 2 * (uint64_t)SUPERFRAME_US + BEACON_GRACE_US;
    for (int k = 2; k < 12; k++) {
        uint64_t when = r.s.next_wakeup();
        if (when == NEVER) break;
        TEST_ASSERT_EQUAL_UINT64(expect, when);  // wake-ups stay exactly one superframe apart
        expect += SUPERFRAME_US;
        r.s.tick(when);
        if (r.s.window_requested()) {
            windows++;
            uint32_t tk = r.s.take_window_request();
            TEST_ASSERT_TRUE(r.s.deliver_window(tk, g_win[7].az, g_win[7].gx, when + 1));
        }
    }
    TEST_ASSERT_EQUAL_UINT32(MAX_MISSED_BEACONS, windows);
    TEST_ASSERT_TRUE(r.s.lost());
    TEST_ASSERT_EQUAL_UINT64(NEVER, r.s.next_wakeup());
    // A beacon brings it back with a normal (synced) window.
    r.beacon(90000000, 40);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_FALSE(r.s.lost());
    TEST_ASSERT_EQUAL_UINT32(40, r.s.window_sf());
}

void test_slave_duplicate_beacon(void) {
    SlaveRig r;
    r.beacon(1000, 7);
    TEST_ASSERT_TRUE(r.s.window_requested());
    uint32_t tok = r.s.take_window_request();
    // Same beacon again (e.g. a retransmission): must not restart the window or move the timing.
    r.beacon(1000 + 4000, 7);
    TEST_ASSERT_FALSE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT64(1000, r.s.window_start());
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().beacons_rejected);
    int16_t az[128] = {0}, gx[128] = {0};
    TEST_ASSERT_TRUE(r.s.deliver_window(tok, az, gx, 2000000));  // the original window is still the live one
    // Old beacon: ignored too.
    r.beacon(3000000, 6);
    TEST_ASSERT_FALSE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(2, r.s.stats().beacons_rejected);
    TEST_ASSERT_EQUAL_UINT64(1000 + SUPERFRAME_US + BEACON_GRACE_US, r.s.next_wakeup());
}

// A slave busy with a new window never transmits, even to a (late) poll whose superframe matches
// the report it still holds from the previous window.
void test_slave_silent_while_busy(void) {
    load_windows();
    SlaveRig r;
    r.run_window(0, g_win[0].az, g_win[0].gx, 4);
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 4, 0, 1300000));
    r.beacon(SUPERFRAME_US + 1000, 5);  // next superframe starts; the window is being sampled
    TEST_ASSERT_TRUE(r.s.busy());
    TEST_ASSERT_TRUE(r.s.has_report());
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 4, 1, SUPERFRAME_US + 3000));  // delayed poll of the previous sf
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 5, 0, SUPERFRAME_US + 4000));
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().polls_answered);
}

void test_slave_late_beacon_resync(void) {
    load_windows();
    SlaveRig r;
    r.run_window(0, g_win[0].az, g_win[0].gx, 0);
    uint64_t due = SUPERFRAME_US + BEACON_GRACE_US;
    r.s.tick(due);  // beacon 1 was late: slave free-runs superframe 1
    TEST_ASSERT_TRUE(r.s.window_requested());
    uint32_t tok = r.s.take_window_request();
    TEST_ASSERT_EQUAL_UINT32(1, r.s.missed_beacons());
    // The real beacon 1 shows up 9 ms late: keep the running window, re-align the timing.
    uint64_t late = SUPERFRAME_US + 9000;
    r.beacon(late, 1);
    TEST_ASSERT_FALSE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(0, r.s.missed_beacons());
    TEST_ASSERT_TRUE(r.s.busy());
    TEST_ASSERT_EQUAL_UINT64(due, r.s.window_start());
    TEST_ASSERT_TRUE(r.s.deliver_window(tok, g_win[1].az, g_win[1].gx, late + 1300000));
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED, r.s.report().flags & FLAG_UNSYNCED);  // started on the slave's own timing: stays UNSYNCED
    TEST_ASSERT_EQUAL_UINT64(late + SUPERFRAME_US + BEACON_GRACE_US, r.s.next_wakeup());
    // A second late copy changes nothing (free_run_pending_ was consumed).
    r.beacon(late + 100, 1);
    TEST_ASSERT_EQUAL_UINT64(late + SUPERFRAME_US + BEACON_GRACE_US, r.s.next_wakeup());
    TEST_ASSERT_FALSE(r.s.window_requested());
    // Late beacon that arrives only AFTER the free-run window was delivered: the finished report
    // keeps its UNSYNCED flag, and that is what the master receives.
    SlaveRig q;
    q.run_window(0, g_win[0].az, g_win[0].gx, 0);
    q.s.tick(due);
    uint32_t tk = q.s.take_window_request();
    TEST_ASSERT_TRUE(q.s.deliver_window(tk, g_win[1].az, g_win[1].gx, due + 1300000));
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED, q.s.report().flags & FLAG_UNSYNCED);
    q.beacon(due + 1400000, 1);
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED, q.s.report().flags & FLAG_UNSYNCED);
    TEST_ASSERT_EQUAL_UINT(1, q.poll(1, 1, 0, due + 1500000));
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(q.tx.sent.back().b.data(), q.tx.sent.back().b.size(), &rp));
    TEST_ASSERT_EQUAL_HEX8(FLAG_UNSYNCED, rp.flags & FLAG_UNSYNCED);
}

void test_slave_epoch_restart(void) {
    load_windows();
    SlaveRig r;
    int8_t ref_hist[3] = {-1, -1, -1};
    int ref_state = 0;
    uint64_t t = 0;
    // Run up to superframe 50 in epoch EP.
    for (uint32_t k = 45; k <= 50; k++) {
        const Win &w = g_win[(k * 37) % g_win.size()];
        r.run_window(t, w.az, w.gx, k);
        float f[5];
        features(w.az, w.gx, f);
        ref_state = smooth_update(ref_hist, classify(f), ref_state);
        t += SUPERFRAME_US;
    }
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 50, 0, t - SUPERFRAME_US + 1400000 - 1000));
    TEST_ASSERT_EQUAL_UINT32(6, r.s.windows_done());
    // The master reboots: new epoch, sf restarts at 0.
    r.beacon(t, 0, EP2);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_EQUAL_HEX32(EP2, r.s.epoch());
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().epoch_resyncs);
    TEST_ASSERT_FALSE_MESSAGE(r.s.has_report(), "cached pre-restart report must be discarded");
    uint32_t tok = r.s.take_window_request();
    TEST_ASSERT_EQUAL_UINT32(0, r.s.window_sf());
    // While the new window is in progress nothing is sent, whatever is asked.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 50, 0, t + 100, EP));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 50, 0, t + 100, EP2));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, t + 100, EP2));
    const Win &w = g_win[999];
    TEST_ASSERT_TRUE(r.s.deliver_window(tok, w.az, w.gx, t + 1300000));
    float f[5];
    features(w.az, w.gx, f);
    ref_state = smooth_update(ref_hist, classify(f), ref_state);
    // After delivery: only the new epoch and new sf are answered.
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 50, 0, t + 1310000, EP));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 50, 0, t + 1310000, EP2));
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, t + 1310000, EP));  // right sf, stale epoch
    size_t before = r.tx.sent.size();
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 0, 0, t + 1310000, EP2));
    ReportPkt rp;
    TEST_ASSERT_TRUE(decode_report(r.tx.sent[before].b.data(), r.tx.sent[before].b.size(), &rp));
    TEST_ASSERT_EQUAL_HEX32(EP2, rp.epoch);
    TEST_ASSERT_EQUAL_UINT32(0, rp.sf);
    // Smoothing history is deliberately kept across a master restart (documented behaviour):
    // the report continues the slave's own window count and smoothing sequence.
    TEST_ASSERT_EQUAL_UINT32(6, rp.window);
    TEST_ASSERT_EQUAL_UINT8(ref_state, rp.state);
    TEST_ASSERT_EQUAL_MEMORY(ref_hist, r.s.smooth_hist(), 3);
}

void test_slave_huge_sf_beacon(void) {
    SlaveRig r;
    r.beacon(0, 10);
    r.s.take_window_request();
    // A CRC-valid beacon with a huge sf in the SAME epoch must not move the slave.
    r.beacon(1000, 0xFFFFFFF0u);
    TEST_ASSERT_FALSE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(10, r.s.last_sf());
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().beacons_rejected);
    // ... so the real next beacon is still accepted (no lock-out).
    r.beacon(SUPERFRAME_US, 11);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(11, r.s.window_sf());
    // Same with a bogus beacon that has a different epoch: it is adopted (cannot be told from a
    // restart), but the real master's next beacon (other epoch again) resyncs the slave at once.
    r.s.take_window_request();
    r.beacon(2 * (uint64_t)SUPERFRAME_US, 0xFFFFFFF0u, 0x13572468u);
    TEST_ASSERT_EQUAL_UINT32(0xFFFFFFF0u, r.s.last_sf());
    r.s.take_window_request();
    r.beacon(3 * (uint64_t)SUPERFRAME_US, 13, EP);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(13, r.s.window_sf());
    TEST_ASSERT_EQUAL_HEX32(EP, r.s.epoch());
}

void test_slave_sf_jump_window(void) {
    SlaveRig r;
    r.beacon(0, 100);
    r.s.take_window_request();
    r.beacon(1000, 100 + MAX_SF_JUMP + 1);  // one too far
    TEST_ASSERT_FALSE(r.s.window_requested());
    r.beacon(2000, 100);  // not ahead
    TEST_ASSERT_FALSE(r.s.window_requested());
    r.beacon(3000, 99);
    TEST_ASSERT_FALSE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(3, r.s.stats().beacons_rejected);
    r.beacon(4000, 100 + MAX_SF_JUMP);  // exactly at the limit
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_EQUAL_UINT32(100 + MAX_SF_JUMP, r.s.last_sf());
    r.s.take_window_request();
    r.beacon(5000, 100 + MAX_SF_JUMP + 1);  // plain next one
    TEST_ASSERT_TRUE(r.s.window_requested());
    // Wrap-around of the 32-bit counter is treated as a normal increment.
    SlaveRig w;
    w.beacon(0, 0xFFFFFFFEu);
    w.s.take_window_request();
    w.beacon(1000, 1);
    TEST_ASSERT_TRUE(w.s.window_requested());
}

// A few lost beacons must not cause rejection: the slave free-runs (advancing last_sf) and then
// sees the master's counter exactly as many superframes ahead as it free-ran.
void test_slave_survives_lost_beacons(void) {
    load_windows();
    for (uint32_t lost_n = 1; lost_n <= MAX_MISSED_BEACONS; lost_n++) {
        SlaveRig r;
        r.run_window(0, g_win[0].az, g_win[0].gx, 20);
        uint64_t t = 0;
        for (uint32_t k = 0; k < lost_n; k++) {  // beacons 21.. never arrive
            uint64_t when = r.s.next_wakeup();
            TEST_ASSERT_NOT_EQUAL_UINT64(NEVER, when);
            r.s.tick(when);
            uint32_t tk = r.s.take_window_request();
            TEST_ASSERT_TRUE(r.s.deliver_window(tk, g_win[1].az, g_win[1].gx, when + 1));
            t = when;
        }
        r.beacon(t + 1000, 20 + lost_n + 1);
        TEST_ASSERT_TRUE_MESSAGE(r.s.window_requested(), "beacon after lost beacons was rejected");
        TEST_ASSERT_EQUAL_UINT32(0, r.s.stats().beacons_rejected);
        TEST_ASSERT_FALSE(r.s.lost());
    }
}

void test_slave_lost_accepts_any_beacon(void) {
    load_windows();
    SlaveRig r;
    r.run_window(0, g_win[0].az, g_win[0].gx, 5);
    for (int k = 0; k < 20 && r.s.next_wakeup() != NEVER; k++) {
        r.s.tick(r.s.next_wakeup());
        if (r.s.window_requested()) { uint32_t tk = r.s.take_window_request(); r.s.deliver_window(tk, g_win[1].az, g_win[1].gx, 1); }
    }
    TEST_ASSERT_TRUE(r.s.lost());
    // Same epoch, sf far in the past or the future: a LOST slave takes whatever comes first.
    r.beacon(100000000, 2);
    TEST_ASSERT_TRUE(r.s.window_requested());
    TEST_ASSERT_FALSE(r.s.lost());
    TEST_ASSERT_TRUE(r.s.synced());
    TEST_ASSERT_EQUAL_UINT32(2, r.s.last_sf());
    // Power-on likewise: any sf.
    SlaveRig f;
    f.beacon(0, 0xFFFFFFF0u);
    TEST_ASSERT_TRUE(f.s.window_requested());
}

void test_slave_window_token(void) {
    load_windows();
    SlaveRig r;
    int16_t *az = g_win[0].az, *gx = g_win[0].gx;
    r.beacon(0, 0);
    uint32_t t0 = r.s.take_window_request();
    TEST_ASSERT_NOT_EQUAL_UINT32(0, t0);
    TEST_ASSERT_EQUAL_UINT32(0, r.s.take_window_request());  // nothing pending now => token 0
    // The next beacon arrives while the host is still computing window 0: it is abandoned.
    r.beacon(SUPERFRAME_US, 1);
    uint32_t t1 = r.s.take_window_request();
    TEST_ASSERT_NOT_EQUAL_UINT32(t0, t1);
    // The late delivery of the abandoned window is rejected and changes nothing.
    TEST_ASSERT_FALSE(r.s.deliver_window(t0, az, gx, SUPERFRAME_US + 10));
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().windows_rejected);
    TEST_ASSERT_FALSE(r.s.has_report());
    TEST_ASSERT_EQUAL_UINT32(0, r.s.windows_done());
    TEST_ASSERT_TRUE(r.s.busy());
    TEST_ASSERT_EQUAL_INT8(-1, r.s.smooth_hist()[2]);
    // Token 0 and made-up tokens are rejected too.
    TEST_ASSERT_FALSE(r.s.deliver_window(0, az, gx, 0));
    TEST_ASSERT_FALSE(r.s.deliver_window(t1 + 1000, az, gx, 0));
    // The right token works exactly once.
    TEST_ASSERT_TRUE(r.s.deliver_window(t1, az, gx, 2 * SUPERFRAME_US));
    TEST_ASSERT_EQUAL_UINT32(1, r.s.report().sf);
    TEST_ASSERT_EQUAL_UINT32(1, r.s.windows_done());
    TEST_ASSERT_FALSE(r.s.deliver_window(t1, az, gx, 2 * SUPERFRAME_US));
    TEST_ASSERT_EQUAL_UINT32(1, r.s.windows_done());
    // A token from before reset() never matches a window started after it (even the very first
    // window after power-on: the token counter survives reset()).
    SlaveRig q;
    q.beacon(0, 2);
    uint32_t t2 = q.s.take_window_request();
    q.s.reset();
    q.beacon(SUPERFRAME_US, 2);
    uint32_t t3 = q.s.take_window_request();
    TEST_ASSERT_NOT_EQUAL_UINT32(t2, t3);
    TEST_ASSERT_FALSE(q.s.deliver_window(t2, az, gx, 0));
    TEST_ASSERT_TRUE(q.s.deliver_window(t3, az, gx, 0));
}

void test_slave_tx_failure(void) {
    load_windows();
    SlaveRig r;
    uint64_t done = r.run_window(0, g_win[0].az, g_win[0].gx, 0);
    r.tx.fail_next = 1;
    TEST_ASSERT_EQUAL_UINT(0, r.poll(1, 0, 0, done + 1000));
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT32(0, r.s.stats().polls_answered);
    TEST_ASSERT_EQUAL_UINT(1, r.poll(1, 0, 1, done + 20000));  // the retry still works, same content
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().polls_answered);
    TEST_ASSERT_EQUAL_UINT32(1, r.s.stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT32(1, r.s.windows_done());
}

// ------------------------------------------------------------------ master
struct Rec : MasterListener {
    std::vector<uint8_t> rep_slave;
    std::vector<uint32_t> rep_sf;
    std::vector<StatusEvent> st;
    void on_report(uint8_t s, const ReportPkt &r, uint64_t) override { rep_slave.push_back(s); rep_sf.push_back(r.sf); }
    void on_status(uint8_t s, bool a, uint64_t t) override { StatusEvent e; e.t_us = t; e.sf = 0; e.slave = s; e.alive = a; st.push_back(e); }
};

struct MasterRig {
    FakeTx tx;
    Rec rec;
    Master m;
    size_t seen;
    MasterRig() : m(&tx, &rec), seen(0) { m.reset(0, EP); }
    // Run one superframe. answer[s] = first poll attempt that slave s+1 answers (-1 = never).
    // Returns number of POLLs sent per slave in out_polls.
    void run_sf(uint32_t sf, int ans0, int ans1, int out_polls[2] = nullptr) {
        uint64_t end = (uint64_t)(sf + 1) * SUPERFRAME_US;
        int ans[2] = {ans0, ans1};
        if (out_polls) out_polls[0] = out_polls[1] = 0;
        for (;;) {
            uint64_t t = m.next_wakeup();
            if (t >= end) break;
            m.tick(t);
            while (seen < tx.sent.size()) {
                const Sent &s = tx.sent[seen++];
                PollPkt p;
                if (!decode_poll(s.b.data(), s.b.size(), &p)) continue;
                int idx = p.h.dst - 1;
                if (out_polls) out_polls[idx]++;
                if (ans[idx] >= 0 && p.attempt >= ans[idx]) {
                    uint8_t b[MAX_PAYLOAD];
                    size_t n = mk_report(b, p.h.dst, p.sf);
                    m.on_packet(b, n, s.t + 1000);
                }
            }
        }
    }
    // Advance to the start of polling of superframe 0 (slave 1's first poll has just been sent).
    void to_polling() {
        m.tick(0);
        m.tick(POLL_START_OFFSET_US);
    }
    void inject(const ReportPkt &r, uint8_t src, uint64_t t, uint8_t dst = ID_MASTER) {
        uint8_t b[MAX_PAYLOAD];
        size_t n = enc(b, r, src, dst);
        m.on_packet(b, n, t);
    }
};

void test_master_retry_exactly_once(void) {
    MasterRig r;
    int polls[2];
    r.run_sf(0, -1, -1, polls);  // nobody answers
    TEST_ASSERT_EQUAL_INT(2, polls[0]);
    TEST_ASSERT_EQUAL_INT(2, polls[1]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().retries[0]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().retries[1]);
    // Timing: attempts spaced by the reply timeout, polling finishes before the next beacon.
    TEST_ASSERT_EQUAL_UINT64(POLL_START_OFFSET_US, r.tx.sent[1].t);
    TEST_ASSERT_EQUAL_UINT64(POLL_START_OFFSET_US + REPLY_TIMEOUT_US, r.tx.sent[2].t);
    TEST_ASSERT_TRUE(r.tx.sent.back().t + REPLY_TIMEOUT_US < SUPERFRAME_US);
    // Answer on retry only: one retry, one report.
    r.run_sf(1, 1, 0, polls);
    TEST_ASSERT_EQUAL_INT(2, polls[0]);
    TEST_ASSERT_EQUAL_INT(1, polls[1]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[0]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[1]);
}

void test_master_duplicates_and_stale(void) {
    MasterRig r;
    r.run_sf(0, 0, 0);
    TEST_ASSERT_EQUAL_UINT(2, r.rec.rep_sf.size());
    uint8_t b[MAX_PAYLOAD];
    size_t n = mk_report(b, 1, 0);
    r.m.on_packet(b, n, 1500000);
    TEST_ASSERT_EQUAL_UINT(2, r.rec.rep_sf.size());
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_duplicate);
    r.run_sf(1, 0, 0);
    TEST_ASSERT_EQUAL_UINT(4, r.rec.rep_sf.size());
    n = mk_report(b, 1, 0);  // old superframe
    r.m.on_packet(b, n, 2900000);
    TEST_ASSERT_EQUAL_UINT(4, r.rec.rep_sf.size());
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_stale);
    n = mk_report(b, 1, 9);  // future
    r.m.on_packet(b, n, 2900000);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_future);
    // Corrupt and misaddressed packets.
    n = mk_report(b, 1, 1);
    b[10] ^= 4;
    r.m.on_packet(b, n, 2900000);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_invalid);
    TEST_ASSERT_EQUAL_UINT(4, r.rec.rep_sf.size());
    // Latest report per slave is kept.
    TEST_ASSERT_TRUE(r.m.has_report(1));
    TEST_ASSERT_EQUAL_UINT32(1, r.m.latest(1).sf);
}

void test_master_dead_and_recovery(void) {
    MasterRig r;
    r.run_sf(0, 0, 0);
    r.run_sf(1, -1, 0);  // slave 1 miss #1
    TEST_ASSERT_TRUE(r.m.alive(1));
    r.run_sf(2, -1, 0);  // miss #2
    TEST_ASSERT_TRUE(r.m.alive(1));
    TEST_ASSERT_EQUAL_UINT(0, r.rec.st.size());
    r.run_sf(3, -1, 0);  // miss #3 -> DEAD
    TEST_ASSERT_FALSE(r.m.alive(1));
    TEST_ASSERT_TRUE(r.m.alive(2));
    TEST_ASSERT_EQUAL_UINT(1, r.rec.st.size());
    TEST_ASSERT_EQUAL_UINT8(1, r.rec.st[0].slave);
    TEST_ASSERT_FALSE(r.rec.st[0].alive);
    // Timestamped inside superframe 3's poll phase, end of it (after the second attempt times out).
    TEST_ASSERT_TRUE(r.rec.st[0].t_us >= 3 * (uint64_t)SUPERFRAME_US + POLL_START_OFFSET_US + 2 * REPLY_TIMEOUT_US);
    TEST_ASSERT_TRUE(r.rec.st[0].t_us < 4 * (uint64_t)SUPERFRAME_US);
    r.run_sf(4, -1, 0);  // still dead, no repeated event
    TEST_ASSERT_EQUAL_UINT(1, r.rec.st.size());
    r.run_sf(5, 0, 0);  // valid report -> ALIVE
    TEST_ASSERT_TRUE(r.m.alive(1));
    TEST_ASSERT_EQUAL_UINT(2, r.rec.st.size());
    TEST_ASSERT_TRUE(r.rec.st[1].alive);
    TEST_ASSERT_TRUE(r.rec.st[1].t_us >= 5 * (uint64_t)SUPERFRAME_US);
    TEST_ASSERT_EQUAL_UINT32(2, r.m.event_count());
    StatusEvent e;
    TEST_ASSERT_TRUE(r.m.event_at(0, &e));
    TEST_ASSERT_FALSE(e.alive);
    TEST_ASSERT_TRUE(r.m.event_at(1, &e));
    TEST_ASSERT_TRUE(e.alive);
    // Non-consecutive misses do not accumulate.
    MasterRig q;
    for (uint32_t k = 0; k < 12; k++) q.run_sf(k, (k % 3 == 2) ? 0 : -1, 0);  // 2 misses then a hit, repeating
    TEST_ASSERT_TRUE(q.m.alive(1));
    TEST_ASSERT_EQUAL_UINT(0, q.rec.st.size());
}

// A report that arrives after the polling phase closed is too late even if it is for this sf.
void test_master_rejects_after_polling_closed(void) {
    MasterRig r;
    r.run_sf(0, -1, 0);  // slave 1 never answered; polling is over
    TEST_ASSERT_FALSE(r.m.polling_active());
    uint32_t before = r.m.stats().rx_stale;
    r.inject(good_report(0), 1, SUPERFRAME_US - 1000);
    TEST_ASSERT_EQUAL_UINT32(before + 1, r.m.stats().rx_stale);
    TEST_ASSERT_EQUAL_UINT32(0, r.m.stats().reports[0]);
    TEST_ASSERT_FALSE(r.m.has_report(1));
    // And before polling opened (WAIT_START).
    MasterRig w;
    w.m.tick(0);
    w.inject(good_report(0), 1, 1000);
    TEST_ASSERT_EQUAL_UINT32(1, w.m.stats().rx_stale);
    TEST_ASSERT_EQUAL_UINT32(0, w.m.stats().reports[0]);
}

void test_master_source_and_destination(void) {
    MasterRig r;
    r.to_polling();
    uint32_t inv0 = r.m.stats().rx_invalid;
    // src 0 would index slot -1, src 3.. is no slave: counted invalid, never stored.
    r.inject(good_report(0), 0, 100);
    r.inject(good_report(0), NUM_SLAVES + 1, 100);
    r.inject(good_report(0), 0xFF, 100);
    TEST_ASSERT_EQUAL_UINT32(inv0 + 3, r.m.stats().rx_invalid);
    TEST_ASSERT_EQUAL_UINT32(0, r.m.stats().reports[0] + r.m.stats().reports[1]);
    TEST_ASSERT_EQUAL_UINT(0, r.rec.rep_sf.size());
    // Not addressed to the master: ignored.
    uint32_t ign0 = r.m.stats().rx_ignored;
    r.inject(good_report(0), 1, 100, 2);
    r.inject(good_report(0), 1, 100, ID_BROADCAST);
    TEST_ASSERT_EQUAL_UINT32(ign0 + 2, r.m.stats().rx_ignored);
    TEST_ASSERT_EQUAL_UINT(0, r.rec.rep_sf.size());
    // A beacon/poll frame addressed to the master is no report either.
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_beacon(b, 1, ID_MASTER, 0, 0, EP);
    r.m.on_packet(b, n, 100);
    TEST_ASSERT_EQUAL_UINT32(ign0 + 3, r.m.stats().rx_ignored);
    // The legitimate one is accepted.
    r.inject(good_report(0), 1, 100);
    TEST_ASSERT_EQUAL_UINT(1, r.rec.rep_sf.size());
}

void test_master_epoch(void) {
    MasterRig r;
    r.to_polling();
    r.inject(good_report(0, 0, EP2), 1, 100);  // report of another master epoch (e.g. cached pre-restart)
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_epoch);
    TEST_ASSERT_EQUAL_UINT(0, r.rec.rep_sf.size());
    TEST_ASSERT_FALSE(r.m.has_report(1));
    r.inject(good_report(0, 0, EP), 1, 200);
    TEST_ASSERT_EQUAL_UINT(1, r.rec.rep_sf.size());
    TEST_ASSERT_EQUAL_HEX32(EP, r.m.latest(1).epoch);
    // Beacons and polls carry the epoch.
    BeaconPkt bp;
    TEST_ASSERT_TRUE(decode_beacon(r.tx.sent[0].b.data(), r.tx.sent[0].b.size(), &bp));
    TEST_ASSERT_EQUAL_HEX32(EP, bp.epoch);
    PollPkt pp;
    TEST_ASSERT_TRUE(decode_poll(r.tx.sent[1].b.data(), r.tx.sent[1].b.size(), &pp));
    TEST_ASSERT_EQUAL_HEX32(EP, pp.epoch);
    // reset() with a new epoch restarts everything under it.
    r.m.reset(5000000, EP2);
    TEST_ASSERT_EQUAL_HEX32(EP2, r.m.epoch());
    TEST_ASSERT_EQUAL_UINT64(5000000, r.m.next_wakeup());
    TEST_ASSERT_FALSE(r.m.has_report(1));
    TEST_ASSERT_EQUAL_UINT32(0, r.m.stats().rx_epoch);
    r.m.tick(5000000);
    TEST_ASSERT_TRUE(decode_beacon(r.tx.sent.back().b.data(), r.tx.sent.back().b.size(), &bp));
    TEST_ASSERT_EQUAL_HEX32(EP2, bp.epoch);
    TEST_ASSERT_EQUAL_UINT32(0, bp.sf);
}

void test_master_field_validation(void) {
    MasterRig r;
    r.to_polling();
    uint32_t bad = 0;
    // Class and state outside {0,1,2}.
    for (int v = 3; v <= 255; v += 126) {
        ReportPkt p = good_report(0); p.raw = (uint8_t)v;   r.inject(p, 1, 100); bad++;
        p = good_report(0);           p.state = (uint8_t)v; r.inject(p, 1, 100); bad++;
    }
    // Reserved flag bits (every one of them, alone).
    for (int bit = 6; bit < 8; bit++) {
        ReportPkt p = good_report(0); p.flags = (uint8_t)(1u << bit); r.inject(p, 1, 100); bad++;
    }
    // Non-finite feature values, each slot, NaN / +inf / -inf.
    const uint32_t bits[3] = {0x7FC00000u, 0x7F800000u, 0xFF800000u};
    for (int i = 0; i < 5; i++)
        for (int k = 0; k < 3; k++) {
            ReportPkt p = good_report(0);
            memcpy(&p.f[i], &bits[k], 4);
            r.inject(p, 1, 100); bad++;
        }
    TEST_ASSERT_EQUAL_UINT32(bad, r.m.stats().rx_bad_field);
    TEST_ASSERT_EQUAL_UINT(0, r.rec.rep_sf.size());
    TEST_ASSERT_FALSE(r.m.has_report(1));
    // Every legal combination is still accepted (slave 2 is not polled yet, so use slave 1 in turn).
    ReportPkt ok = good_report(0);
    ok.raw = 2; ok.state = 1; ok.flags = FLAG_LOW_SIGNAL | FLAG_UNSYNCED;
    ok.f[0] = -0.0f; ok.f[1] = 3.4e38f; ok.f[2] = 1e-45f;  // extremes but finite (incl. denormal)
    r.inject(ok, 1, 100);
    TEST_ASSERT_EQUAL_UINT(1, r.rec.rep_sf.size());
    TEST_ASSERT_EQUAL_UINT32(bad, r.m.stats().rx_bad_field);
}

void test_master_only_polled_slaves(void) {
    MasterRig r;
    r.to_polling();  // slave 1's first poll is out; slave 2 has not been polled
    r.inject(good_report(0), 2, 100);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_unpolled);
    TEST_ASSERT_EQUAL_UINT32(0, r.m.stats().reports[1]);
    TEST_ASSERT_FALSE(r.m.has_report(2));
    // Slave 1's first attempt times out; the retry goes out; a LATE reply to the first poll is fine.
    r.m.tick(r.m.next_wakeup());
    r.inject(good_report(0), 1, POLL_START_OFFSET_US + REPLY_TIMEOUT_US + 500);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[0]);
    // Now slave 2 has been polled; its report is taken, once.
    r.inject(good_report(0), 2, POLL_START_OFFSET_US + REPLY_TIMEOUT_US + 600);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[1]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().rx_unpolled);
    // Next superframe the flags are cleared again: nothing is "polled" before the polls go out.
    r.m.tick(SUPERFRAME_US);
    r.inject(good_report(1), 1, SUPERFRAME_US + 10);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[0]);
    r.m.tick(SUPERFRAME_US + POLL_START_OFFSET_US);  // polling of superframe 1: slave 1 is out, slave 2 is not
    r.inject(good_report(1), 2, SUPERFRAME_US + POLL_START_OFFSET_US + 100);
    TEST_ASSERT_EQUAL_UINT32(2, r.m.stats().rx_unpolled);  // slave 2 WAS polled last superframe: that must not count
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().reports[1]);
}

void test_master_tx_failure(void) {
    MasterRig r;
    // Beacon send fails: counted, superframe still runs.
    r.tx.fail_next = 1;
    r.m.tick(0);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT(0, r.tx.sent.size());
    TEST_ASSERT_TRUE(r.m.polling_active());
    // First poll attempt fails: it counts as a used attempt; the retry follows at once.
    r.tx.fail_next = 1;
    r.m.tick(POLL_START_OFFSET_US);
    TEST_ASSERT_EQUAL_UINT32(2, r.m.stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT64(POLL_START_OFFSET_US, r.m.next_wakeup());  // due right now, no 18 ms wait
    r.m.tick(POLL_START_OFFSET_US);
    TEST_ASSERT_EQUAL_UINT(1, r.tx.sent.size());
    PollPkt pp;
    TEST_ASSERT_TRUE(decode_poll(r.tx.sent[0].b.data(), r.tx.sent[0].b.size(), &pp));
    TEST_ASSERT_EQUAL_UINT8(1, pp.h.dst);
    TEST_ASSERT_EQUAL_UINT8(1, pp.attempt);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().polls_tx[0]);
    TEST_ASSERT_EQUAL_UINT32(1, r.m.stats().retries[0]);
    // Both attempts fail: slave 1 is skipped immediately and slave 2 gets polled, no time wasted.
    MasterRig q;
    q.m.tick(0);
    q.tx.fail_next = 2;
    q.m.tick(POLL_START_OFFSET_US);  // attempt 1 to slave 1 fails
    q.m.tick(POLL_START_OFFSET_US);  // attempt 2 fails
    q.m.tick(POLL_START_OFFSET_US);  // out of attempts: on to slave 2, right away
    TEST_ASSERT_EQUAL_UINT32(2, q.m.stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT(2, q.tx.sent.size());  // beacon + first poll to slave 2
    TEST_ASSERT_TRUE(decode_poll(q.tx.sent[1].b.data(), q.tx.sent[1].b.size(), &pp));
    TEST_ASSERT_EQUAL_UINT8(2, pp.h.dst);
    // A reply to a poll that was never sent is not accepted.
    q.inject(good_report(0), 1, POLL_START_OFFSET_US + 100);
    TEST_ASSERT_EQUAL_UINT32(1, q.m.stats().rx_unpolled);
    // The superframe ends with slave 1 counted as missed.
    for (int i = 0; i < 20 && q.m.polling_active(); i++) q.m.tick(q.m.next_wakeup());
    TEST_ASSERT_EQUAL_UINT32(1, q.m.missed(1));
}

void test_master_fixed_beacon_grid(void) {
    // Ticked late by up to 8 ms every time: beacon k stays within [k*T, k*T + 8 ms] and the mean
    // period is exactly the superframe (no accumulation of lateness).
    FakeTx tx;
    Master m(&tx);
    m.reset(0, EP);
    netsim::Prng g(5);
    const int N = 1000;
    int beacons = 0;
    uint64_t first = 0, last = 0;
    while (beacons < N) {
        uint64_t w = m.next_wakeup();
        uint64_t now = w + g.below_incl(8000);
        size_t before = tx.sent.size();
        m.tick(now);
        for (size_t i = before; i < tx.sent.size(); i++) {
            BeaconPkt bp;
            if (decode_beacon(tx.sent[i].b.data(), tx.sent[i].b.size(), &bp)) {
                TEST_ASSERT_EQUAL_UINT32((uint32_t)beacons, bp.sf);
                TEST_ASSERT_TRUE(now >= (uint64_t)beacons * SUPERFRAME_US);
                TEST_ASSERT_TRUE(now <= (uint64_t)beacons * SUPERFRAME_US + 8000);
                if (beacons == 0) first = now;
                last = now;
                beacons++;
            }
        }
    }
    double mean = (double)(last - first) / (N - 1);
    printf("beacon mean period with 0..8 ms late ticks: %.3f us (nominal %u)\n", mean, (unsigned)SUPERFRAME_US);
    TEST_ASSERT_TRUE(fabs(mean - (double)SUPERFRAME_US) < 20.0);
    TEST_ASSERT_EQUAL_UINT32(0, m.stats().beacons_skipped);
}

// Ticked 3 ms late at every poll deadline: the 4th attempt would end after the next beacon, so it is
// not sent at all (the guard), and nothing is polled whose reply window crosses the beacon.
void test_master_poll_guard_when_late(void) {
    FakeTx tx;
    Master m(&tx);
    m.reset(0, EP);
    m.tick(0);
    uint64_t now = POLL_START_OFFSET_US;
    for (int i = 0; i < 12 && m.polling_active(); i++) {
        m.tick(now);
        uint64_t w = m.next_wakeup();
        now = (w > now ? w : now) + 3000;
        if (now >= SUPERFRAME_US) break;
    }
    int polls = 0;
    for (size_t i = 1; i < tx.sent.size(); i++) {
        PollPkt p;
        if (!decode_poll(tx.sent[i].b.data(), tx.sent[i].b.size(), &p)) continue;
        polls++;
        TEST_ASSERT_TRUE(tx.sent[i].t + REPLY_TIMEOUT_US <= SUPERFRAME_US);
    }
    TEST_ASSERT_EQUAL_INT(3, polls);  // attempts at 1.320, 1.341, 1.362; the 4th (1.383 + 18 ms) would cross 1.400
}

void test_master_skips_forward_when_far_behind(void) {
    FakeTx tx;
    Master m(&tx);
    m.reset(0, EP);
    m.tick(0);
    TEST_ASSERT_EQUAL_UINT32(0, m.stats().beacons_skipped);
    // The host stalls for 2.07 superframes: one beacon now (not a burst of three), grid points
    // 2T and 3T are recorded as skipped, the next beacon is the first grid point after now.
    uint64_t late = 3 * (uint64_t)SUPERFRAME_US + 100000;
    m.tick(late);
    TEST_ASSERT_EQUAL_UINT32(2, m.stats().beacons);
    TEST_ASSERT_EQUAL_UINT32(2, m.stats().beacons_skipped);
    TEST_ASSERT_TRUE(m.next_wakeup() > late);
    {   // the late beacon carries the grid index (3), not the next counter value (1)
        BeaconPkt bp;
        bool found = false;
        for (size_t i = tx.sent.size(); i-- > 0;)
            if (decode_beacon(tx.sent[i].b.data(), tx.sent[i].b.size(), &bp)) { found = true; break; }
        TEST_ASSERT_TRUE(found);
        TEST_ASSERT_EQUAL_UINT32(3, bp.sf);
    }
    TEST_ASSERT_EQUAL_UINT32(1, m.missed(1));  // the interrupted superframe 0 was closed out, not forgotten
    TEST_ASSERT_EQUAL_UINT32(1, m.missed(2));
    m.tick(late + 1);  // nothing more is due
    TEST_ASSERT_EQUAL_UINT32(2, m.stats().beacons);
    uint64_t grid_next = 4 * (uint64_t)SUPERFRAME_US;
    // Skipping to the grid: the next beacon is at 4T (the first grid point after `late`), polling
    // for the late beacon was cut short by the guard but nothing bursts.
    while (m.next_wakeup() < grid_next) m.tick(m.next_wakeup());
    TEST_ASSERT_EQUAL_UINT64(grid_next, m.next_wakeup());
    m.tick(grid_next);
    TEST_ASSERT_EQUAL_UINT32(3, m.stats().beacons);
    // Exactly one superframe behind counts as behind too (boundary): late by 1T exactly.
    FakeTx tx2;
    Master m2(&tx2);
    m2.reset(0, EP);
    m2.tick(SUPERFRAME_US);  // first beacon is a whole superframe late
    TEST_ASSERT_EQUAL_UINT32(1, m2.stats().beacons_skipped);
    uint64_t t2 = 0;
    while (m2.stats().beacons < 2) { t2 = m2.next_wakeup(); m2.tick(t2); }
    TEST_ASSERT_EQUAL_UINT64(2 * (uint64_t)SUPERFRAME_US, t2);  // next beacon is on the grid, 2T
}

// ------------------------------------------------------------------ simulator
static netsim::SimConfig base_cfg(uint32_t seed, uint32_t sfs) {
    netsim::SimConfig c;
    c.seed = seed;
    c.superframes = sfs;
    return c;
}
static std::string join(const netsim::SimResult &r) {
    std::string s;
    for (size_t i = 0; i < r.log.size(); i++) { s += r.log[i]; s += '\n'; }
    return s + r.summary();
}
static std::string net_lines(const netsim::SimResult &r) {
    std::string s;
    for (size_t i = 0; i < r.log.size(); i++)
        if (r.log[i].find(" net ") != std::string::npos) { s += r.log[i]; s += '\n'; }
    return s;
}

void test_sim_determinism(void) {
    netsim::SimConfig c = base_cfg(7, 40);
    c.loss = 0.2; c.corrupt = 0.05; c.jitter_us = 4000;
    netsim::Sim a(c), b(c);
    a.run(); b.run();
    TEST_ASSERT_TRUE(join(a.res) == join(b.res));
    TEST_ASSERT_TRUE(a.res.log.size() > 100);
    netsim::SimConfig d = c;
    d.seed = 8;
    netsim::Sim e(d);
    e.run();
    TEST_ASSERT_TRUE(join(a.res) != join(e.res));
}

// The seed must reach the NETWORK random stream (loss/jitter/corruption), not only the sensor data.
void test_sim_seed_drives_network(void) {
    netsim::SimConfig c = base_cfg(1, 40);
    c.loss = 0.3; c.corrupt = 0.1;
    netsim::Sim a(c);
    a.run();
    netsim::SimConfig d = c;
    d.seed = 2;
    netsim::Sim b(d);
    b.run();
    std::string na = net_lines(a.res), nb = net_lines(b.res);
    TEST_ASSERT_TRUE(na.size() > 200);
    TEST_ASSERT_TRUE(na != nb);
    // And the same seed reproduces the network exactly.
    netsim::Sim a2(c);
    a2.run();
    TEST_ASSERT_TRUE(na == net_lines(a2.res));
}

void test_sim_lossless(void) {
    netsim::Sim s(base_cfg(1, 50));
    s.run();
    for (int n = 1; n <= 2; n++) {
        TEST_ASSERT_EQUAL_UINT32(50, s.res.reports_delivered[n]);
        TEST_ASSERT_EQUAL_UINT32(0, s.res.retries[n]);
        TEST_ASSERT_EQUAL_UINT32(50, s.res.polls_tx[n]);
    }
    TEST_ASSERT_EQUAL_UINT32(0, s.res.unsynced_reports);
    TEST_ASSERT_EQUAL_UINT32(0, s.res.low_signal_reports);
    TEST_ASSERT_EQUAL_UINT32(0, s.res.dropped);
    // Bytes count every packet transmitted.
    TEST_ASSERT_EQUAL_UINT64(50 * BEACON_LEN + 100 * POLL_LEN, s.res.bytes_tx[0]);
    TEST_ASSERT_EQUAL_UINT64(50 * REPORT_LEN, s.res.bytes_tx[1]);
    TEST_ASSERT_EQUAL_UINT64(50 * REPORT_LEN, s.res.bytes_tx[2]);
    uint64_t sum = 0;
    for (size_t i = 0; i < s.res.tx.size(); i++) sum += s.res.tx[i].len;
    TEST_ASSERT_EQUAL_UINT64(s.res.bytes_tx[0] + s.res.bytes_tx[1] + s.res.bytes_tx[2], sum);
}

void test_sim_bytes_count_retries(void) {
    netsim::SimConfig c = base_cfg(4, 120);
    c.loss = 0.4;
    netsim::Sim s(c);
    s.run();
    uint32_t polls = s.res.polls_tx[1] + s.res.polls_tx[2];
    uint32_t retries = s.res.retries[1] + s.res.retries[2];
    TEST_ASSERT_TRUE(retries > 20);
    TEST_ASSERT_TRUE(polls > 120 * 2);
    TEST_ASSERT_EQUAL_UINT32(polls, s.master().stats().polls_tx[0] + s.master().stats().polls_tx[1]);
    TEST_ASSERT_EQUAL_UINT32(retries, s.master().stats().retries[0] + s.master().stats().retries[1]);
    TEST_ASSERT_EQUAL_UINT64((uint64_t)s.master().stats().beacons * BEACON_LEN + (uint64_t)polls * POLL_LEN, s.res.bytes_tx[0]);
    TEST_ASSERT_EQUAL_UINT64((uint64_t)s.res.pkts_tx[1][T_REPORT] * REPORT_LEN, s.res.bytes_tx[1]);
    TEST_ASSERT_EQUAL_UINT32(s.res.pkts_tx[0][T_POLL], polls);
}

void test_sim_corruption(void) {
    netsim::SimConfig c = base_cfg(9, 40);
    c.corrupt = 1.0;  // every delivered copy has one flipped bit
    netsim::Sim s(c);
    s.run();
    uint32_t copies = (uint32_t)s.res.tx.size() * NUM_SLAVES;
    TEST_ASSERT_EQUAL_UINT32(copies, s.res.corrupted);
    TEST_ASSERT_TRUE(s.res.rx_rejected > 0);
    TEST_ASSERT_TRUE(s.res.rx_rejected <= s.res.corrupted);
    TEST_ASSERT_TRUE(s.res.rx_rejected + 4 >= s.res.corrupted);  // all but those landing beyond the horizon
    TEST_ASSERT_EQUAL_UINT32(0, s.res.reports_delivered[1] + s.res.reports_delivered[2]);
    // Partial corruption: roughly the requested fraction, and clean traffic still gets through.
    netsim::SimConfig d = base_cfg(9, 200);
    d.corrupt = 0.2;
    netsim::Sim t(d);
    t.run();
    double frac = (double)t.res.corrupted / ((double)t.res.tx.size() * NUM_SLAVES);
    TEST_ASSERT_TRUE(frac > 0.15 && frac < 0.25);
    TEST_ASSERT_TRUE(t.res.reports_delivered[1] > 50);
    TEST_ASSERT_TRUE(t.res.reports_delivered[1] < 200);
}

void test_sim_jitter(void) {
    netsim::SimConfig c = base_cfg(2, 40);
    c.jitter_us = 0;
    netsim::Sim a(c);
    a.run();
    TEST_ASSERT_EQUAL_UINT32(c.delay_us, a.res.min_delivery_us);
    TEST_ASSERT_EQUAL_UINT32(c.delay_us, a.res.max_delivery_us);
    c.jitter_us = 5000;
    netsim::Sim b(c);
    b.run();
    TEST_ASSERT_TRUE(b.res.min_delivery_us >= c.delay_us);
    TEST_ASSERT_TRUE(b.res.max_delivery_us <= c.delay_us + c.jitter_us);
    TEST_ASSERT_TRUE(b.res.max_delivery_us - b.res.min_delivery_us > 3500);
    // The extreme setting must not divide by zero (below(UINT32_MAX + 1) would).
    netsim::Prng g(1);
    uint32_t hi = 0;
    for (int i = 0; i < 100; i++) { uint32_t v = g.below_incl(0xFFFFFFFFu); if (v > hi) hi = v; }
    TEST_ASSERT_TRUE(hi > 0x80000000u);
    TEST_ASSERT_EQUAL_UINT32(0, g.below_incl(0));
    netsim::SimConfig e = base_cfg(2, 6);
    e.jitter_us = 0xFFFFFFFFu;
    netsim::Sim x(e);
    x.run();
    TEST_ASSERT_TRUE(x.res.max_delivery_us > 0x80000000u);
}

void test_sim_kill_and_revive(void) {
    netsim::SimConfig c = base_cfg(3, 60);
    netsim::Action k = {1, 10}, v = {1, 30};
    c.kills.push_back(k);
    c.revives.push_back(v);
    netsim::Sim s(c);
    s.run();
    TEST_ASSERT_EQUAL_UINT(1, s.res.death.size());
    TEST_ASSERT_TRUE(s.res.death[0].detected);
    uint64_t lat = s.res.death[0].detect_t - s.res.death[0].cause_t;
    printf("death latency %llu us, recovery latency %llu us\n", (unsigned long long)lat,
           (unsigned long long)(s.res.recovery[0].detect_t - s.res.recovery[0].cause_t));
    TEST_ASSERT_TRUE(lat < 3ull * SUPERFRAME_US);                       // bound: within 3 superframes of the kill
    TEST_ASSERT_TRUE(lat > 2ull * SUPERFRAME_US);                       // and not before the third miss
    TEST_ASSERT_TRUE(s.res.recovery[0].detected);
    TEST_ASSERT_TRUE(s.res.recovery[0].detect_t - s.res.recovery[0].cause_t < (uint64_t)SUPERFRAME_US);  // next report
    TEST_ASSERT_EQUAL_UINT32(10 + 30, s.res.reports_delivered[1]);     // sf 0..9 and 30..59
    TEST_ASSERT_EQUAL_UINT32(60, s.res.reports_delivered[2]);          // unaffected
    TEST_ASSERT_EQUAL_UINT32(0, s.res.retries[2]);
    TEST_ASSERT_TRUE(s.master().alive(1));
    TEST_ASSERT_TRUE(s.master().alive(2));
    // Killed slave transmits nothing while dead.
    for (size_t i = 0; i < s.res.tx.size(); i++)
        if (s.res.tx[i].node == 1) TEST_ASSERT_TRUE(s.res.tx[i].t < 10ull * SUPERFRAME_US || s.res.tx[i].t >= 30ull * SUPERFRAME_US);
}

// A revived slave is a rebooted slave: all state is gone.
void test_sim_revive_resets_slave(void) {
    netsim::SimConfig c = base_cfg(3, 36);
    netsim::Action k = {1, 10}, v = {1, 30};
    c.kills.push_back(k);
    c.revives.push_back(v);
    netsim::Sim s(c);
    s.run();
    // Windows after the revive: superframes 30..35 => at most 6 (fresh counter), not 10 + 6.
    TEST_ASSERT_EQUAL_UINT32(6, s.slave(1).windows_done());
    TEST_ASSERT_EQUAL_UINT32(36, s.slave(2).windows_done());  // the untouched slave kept counting
}

void test_sim_slave_access(void) {
    netsim::Sim s(base_cfg(1, 3));
    TEST_ASSERT_NULL(s.slave_ptr(0));
    TEST_ASSERT_NULL(s.slave_ptr(3));
    TEST_ASSERT_NULL(s.slave_ptr(-1));
    TEST_ASSERT_TRUE(s.slave_ptr(1) != s.slave_ptr(2));
    TEST_ASSERT_EQUAL_UINT8(1, s.slave(1).id());
    TEST_ASSERT_EQUAL_UINT8(2, s.slave(2).id());
}

void test_sim_quiet_keeps_no_log(void) {
    netsim::SimConfig c = base_cfg(1, 20);
    c.loss = 0.2;
    netsim::Sim loud(c);
    loud.run();
    c.quiet = true;
    netsim::Sim q(c);
    q.run();
    TEST_ASSERT_TRUE(loud.res.log.size() > 50);
    TEST_ASSERT_EQUAL_UINT(0, q.res.log.size());
    // Quiet changes nothing else.
    TEST_ASSERT_TRUE(loud.res.summary() == q.res.summary());
    TEST_ASSERT_EQUAL_UINT32(loud.res.reports_delivered[1], q.res.reports_delivered[1]);
}

void test_sim_loss_30(void) {
    netsim::SimConfig c = base_cfg(11, 400);
    c.loss = 0.30;
    netsim::Sim s(c);
    s.run();
    // A report needs poll and reply through (0.7 each); two attempts; beacon loss only makes
    // the slave free-run (still reports). Expected delivery 1-(1-0.49)^2 = 0.7399, first-attempt
    // failure (a retry is sent) 0.51.
    double slots = 800;
    double delivered = (s.res.reports_delivered[1] + s.res.reports_delivered[2]) / slots;
    double retries = (s.res.retries[1] + s.res.retries[2]) / slots;
    printf("30%% loss: delivered=%.3f (expect ~0.740) retry_rate=%.3f (expect ~0.510) unsynced=%u\n", delivered,
           retries, s.res.unsynced_reports);
    TEST_ASSERT_TRUE(delivered > 0.68 && delivered < 0.80);
    TEST_ASSERT_TRUE(retries > 0.44 && retries < 0.58);
    TEST_ASSERT_TRUE(s.res.unsynced_reports > 0);
}

static void check_invariants_from_log(const netsim::SimResult &r) {
    // Recomputed independently from the transmission record.
    size_t slave_tx = 0, polls = 0;
    for (size_t i = 0; i < r.tx.size(); i++) {
        const netsim::TxRec &t = r.tx[i];
        if (t.node > 0) {
            slave_tx++;
            for (size_t j = 0; j < r.sampling[t.node].size(); j++) {
                const netsim::Interval &iv = r.sampling[t.node][j];
                if (t.t >= iv.start && t.t <= iv.end) TEST_FAIL_MESSAGE("slave transmitted during sampling");
            }
        }
        if (t.type == T_POLL) {
            polls++;
            uint64_t b = t.t / SUPERFRAME_US * SUPERFRAME_US;  // beacon of this superframe
            TEST_ASSERT_TRUE(t.t >= b + POLL_START_OFFSET_US);
            TEST_ASSERT_TRUE(t.t + REPLY_TIMEOUT_US <= b + SUPERFRAME_US);
        }
    }
    TEST_ASSERT_TRUE(slave_tx > 0);
    TEST_ASSERT_TRUE(polls > 0);
    TEST_ASSERT_TRUE(r.audited);
    TEST_ASSERT_EQUAL_UINT32(0, r.viol_tx_in_sampling);
    TEST_ASSERT_EQUAL_UINT32(0, r.viol_poll_overrun);
}

void test_sim_invariants(void) {
    struct Case { double loss, corrupt; uint32_t jitter, compute; };
    const Case cases[] = {
        {0.0, 0.0, 0, 30000},
        {0.30, 0.0, 3000, 30000},
        {0.30, 0.10, 6000, 30000},
        {0.15, 0.05, 9000, 30000},   // round trip can exceed the reply timeout: late replies
        {0.10, 0.0, 2000, 60000},    // slow compute: first poll may find the slave not ready
        {0.45, 0.10, 8000, 25000},
    };
    for (size_t k = 0; k < sizeof cases / sizeof cases[0]; k++) {
        netsim::SimConfig c = base_cfg(100 + (uint32_t)k, 150);
        c.loss = cases[k].loss; c.corrupt = cases[k].corrupt; c.jitter_us = cases[k].jitter; c.compute_us = cases[k].compute;
        netsim::Action a = {2, 40}, b = {2, 70}, a2 = {1, 100};
        c.kills.push_back(a); c.revives.push_back(b); c.kills.push_back(a2);
        netsim::Sim s(c);
        s.run();
        check_invariants_from_log(s.res);
    }
}

// The audit itself must catch violations (negative control on hand-made records).
void test_sim_audit_detects_violations(void) {
    netsim::SimResult r;
    r.tx.push_back({1000, 1, T_REPORT, REPORT_LEN});
    r.sampling[1].push_back({500, 1000});  // transmit exactly at the last sample: inside
    r.audit();
    TEST_ASSERT_EQUAL_UINT32(1, r.viol_tx_in_sampling);
    TEST_ASSERT_EQUAL_UINT32(0, r.viol_poll_overrun);
    netsim::SimResult q;
    q.beacon_times.push_back(0);
    q.beacon_times.push_back(SUPERFRAME_US);
    q.tx.push_back({SUPERFRAME_US - REPLY_TIMEOUT_US + 1, 0, T_POLL, POLL_LEN});  // reply window crosses the next beacon
    q.tx.push_back({SUPERFRAME_US - REPLY_TIMEOUT_US, 0, T_POLL, POLL_LEN});      // ends exactly at it: fine
    q.audit();
    TEST_ASSERT_EQUAL_UINT32(0, q.viol_tx_in_sampling);
    TEST_ASSERT_EQUAL_UINT32(1, q.viol_poll_overrun);
    // A master restart in between excuses it.
    q.restarts.push_back({SUPERFRAME_US - 1000, 5});
    q.audit();
    TEST_ASSERT_EQUAL_UINT32(0, q.viol_poll_overrun);
}

// Abandoned windows: the host still computing when the next beacon arrives must never have its
// late result accepted as the new superframe's data.
void test_sim_abandoned_window_rejected(void) {
    netsim::SimConfig c = base_cfg(5, 20);
    c.compute_us = 200000;  // done 1.47 s after window start; the next beacon (1.4 s) abandons it
    netsim::Sim s(c);
    s.run();
    TEST_ASSERT_EQUAL_UINT32(0, s.res.reports_delivered[1] + s.res.reports_delivered[2]);
    TEST_ASSERT_TRUE(s.res.windows_rejected >= 2 * 17);
    TEST_ASSERT_EQUAL_UINT32(0, s.slave(1).windows_done());
}

void test_sim_tx_failures(void) {
    netsim::SimConfig c = base_cfg(6, 30);
    netsim::FailWindow f1 = {1, 5 * (uint64_t)SUPERFRAME_US, 8 * (uint64_t)SUPERFRAME_US};  // slave 1 cannot send in sf 5..7
    netsim::FailWindow f0 = {0, 12 * (uint64_t)SUPERFRAME_US, 13 * (uint64_t)SUPERFRAME_US};  // master cannot send in sf 12
    c.fail_windows.push_back(f1);
    c.fail_windows.push_back(f0);
    netsim::Sim s(c);
    s.run();
    TEST_ASSERT_EQUAL_UINT32(3 * 2, s.res.tx_failed[1]);  // sf 5..7: reply to the poll and to its retry
    TEST_ASSERT_EQUAL_UINT32(1 + 2 * 2, s.res.tx_failed[0]);  // sf 12: the beacon and both attempts for both slaves
    TEST_ASSERT_EQUAL_UINT32(s.res.tx_failed[0], s.master().stats().tx_failed);
    TEST_ASSERT_EQUAL_UINT32(30 - 3 - 1, s.res.reports_delivered[1]);
    TEST_ASSERT_EQUAL_UINT32(30 - 1, s.res.reports_delivered[2]);
    // Failed sends are not on the air: not in the byte count.
    uint64_t sum = 0;
    for (size_t i = 0; i < s.res.tx.size(); i++) sum += s.res.tx[i].len;
    TEST_ASSERT_EQUAL_UINT64(s.res.bytes_tx[0] + s.res.bytes_tx[1] + s.res.bytes_tx[2], sum);
    // Random failures: deterministic, and the network keeps working.
    netsim::SimConfig d = base_cfg(6, 100);
    d.tx_fail = 0.2;
    netsim::Sim a(d), b(d);
    a.run(); b.run();
    TEST_ASSERT_TRUE(a.res.summary() == b.res.summary());
    TEST_ASSERT_TRUE(a.res.tx_failed[0] + a.res.tx_failed[1] + a.res.tx_failed[2] > 30);
    TEST_ASSERT_TRUE(a.res.reports_delivered[1] > 50);
    TEST_ASSERT_EQUAL_UINT32(0, a.res.viol_poll_overrun);
}

// ------------------------------------------------------------------ restart / recovery
static void check_master_restart(uint32_t N, bool hard_gap_us) {
    netsim::SimConfig c = base_cfg(N + 1, N + 6);
    if (!hard_gap_us) {
        netsim::Action k = {0, N}, v = {0, N};
        c.kills.push_back(k);
        c.revives.push_back(v);
    } else {  // down for 0.55 s, restart mid-superframe
        netsim::TimedAction k = {0, N * (uint64_t)SUPERFRAME_US + 300000}, v = {0, N * (uint64_t)SUPERFRAME_US + 850000};
        c.kills_us.push_back(k);
        c.revives_us.push_back(v);
    }
    netsim::Sim s(c);
    s.run();
    TEST_ASSERT_EQUAL_UINT(1, s.res.restarts.size());
    const uint64_t tr = s.res.restarts[0].t;
    const uint32_t ep = s.res.restarts[0].epoch;
    TEST_ASSERT_TRUE(ep != 0 && ep != s.res.reports[0].epoch);
    uint32_t pre_max_window[3] = {0, 0, 0};
    uint64_t first_fresh[3] = {0, 0, 0};
    uint32_t fresh_count[3] = {0, 0, 0};
    for (size_t i = 0; i < s.res.reports.size(); i++) {
        const netsim::ReportRec &r = s.res.reports[i];
        if (r.t < tr) {
            if (r.window + 1 > pre_max_window[r.slave]) pre_max_window[r.slave] = r.window + 1;
            continue;
        }
        // Every report accepted after the restart belongs to the new epoch and the new numbering.
        TEST_ASSERT_EQUAL_HEX32(ep, r.epoch);
        TEST_ASSERT_EQUAL_HEX32(ep, r.master_epoch);
        TEST_ASSERT_EQUAL_UINT32(r.master_sf, r.sf);
        TEST_ASSERT_TRUE(r.sf < 8);
        // ... and is a NEW window (a stale cached report would have an old window number).
        TEST_ASSERT_TRUE(r.window >= pre_max_window[r.slave]);
        if (!fresh_count[r.slave]) first_fresh[r.slave] = r.t;
        fresh_count[r.slave]++;
    }
    for (int sl = 1; sl <= 2; sl++) {
        TEST_ASSERT_TRUE_MESSAGE(fresh_count[sl] > 0, "no fresh report after master restart");
        uint64_t dt = first_fresh[sl] - tr;
        printf("restart at sf %u%s: slave%d first fresh report %.3f s after restart (%u fresh reports)\n", (unsigned)N,
               hard_gap_us ? " (mid-sf, 0.55 s down)" : "", sl, (double)dt / 1e6, (unsigned)fresh_count[sl]);
        TEST_ASSERT_TRUE(dt < 2ull * SUPERFRAME_US);  // target: within 2 superframes of the restart
    }
    // Nothing from the old epoch sneaks in afterwards and nobody got stuck.
    TEST_ASSERT_TRUE(s.master().alive(1) && s.master().alive(2));
    TEST_ASSERT_EQUAL_UINT32(0, s.master().stats().rx_epoch);
    TEST_ASSERT_EQUAL_UINT32(0, s.slave(1).stats().beacons_rejected);
    TEST_ASSERT_EQUAL_UINT32(1, s.slave(1).stats().epoch_resyncs);
    TEST_ASSERT_EQUAL_UINT32(1, s.slave(2).stats().epoch_resyncs);
}

void test_master_restart_recovery(void) {
    const uint32_t at[3] = {10, 50, 200};
    for (int i = 0; i < 3; i++) {
        check_master_restart(at[i], false);
        check_master_restart(at[i], true);
    }
}

void test_master_restart_under_loss(void) {
    // 20% loss: recovery is probabilistic but must always happen quickly.
    uint32_t worst = 0;
    for (uint32_t seed = 1; seed <= 12; seed++) {
        netsim::SimConfig c = base_cfg(seed, 30);
        c.loss = 0.2;
        c.quiet = true;
        netsim::Action k = {0, 15}, v = {0, 15};
        c.kills.push_back(k);
        c.revives.push_back(v);
        netsim::Sim s(c);
        s.run();
        TEST_ASSERT_EQUAL_UINT(1, s.res.restarts.size());
        const uint64_t tr = s.res.restarts[0].t;
        for (int sl = 1; sl <= 2; sl++) {
            bool got = false;
            for (size_t i = 0; i < s.res.reports.size(); i++) {
                const netsim::ReportRec &r = s.res.reports[i];
                if (r.slave != sl || r.t < tr) continue;
                TEST_ASSERT_EQUAL_HEX32(s.res.restarts[0].epoch, r.epoch);  // never a stale one
                if (!got) {
                    got = true;
                    uint32_t sfs = (uint32_t)((r.t - tr) / SUPERFRAME_US);
                    if (sfs > worst) worst = sfs;
                }
            }
            TEST_ASSERT_TRUE(got);
        }
    }
    printf("restart under 20%% loss: worst first-fresh-report delay over 24 slave-runs = %u superframes\n", (unsigned)worst);
    TEST_ASSERT_TRUE(worst <= 6);
}

void test_sim_lost_slaves_resync(void) {
    // Master cannot send for 9 superframes (beacons and polls fail): both slaves free-run 5
    // superframes and go LOST. When sending works again they resync on the very next beacon.
    netsim::SimConfig c = base_cfg(8, 30);
    netsim::FailWindow f = {0, 10 * (uint64_t)SUPERFRAME_US, 19 * (uint64_t)SUPERFRAME_US};
    c.fail_windows.push_back(f);
    netsim::Sim s(c);
    s.run();
    // Superframes 19.. were beaconed normally: every one has a report from both slaves.
    uint32_t after[3] = {0, 0, 0};
    uint64_t first_after[3] = {0, 0, 0};
    for (size_t i = 0; i < s.res.reports.size(); i++) {
        const netsim::ReportRec &r = s.res.reports[i];
        if (r.t >= 19 * (uint64_t)SUPERFRAME_US) {
            if (!after[r.slave]) first_after[r.slave] = r.t;
            after[r.slave]++;
        }
    }
    TEST_ASSERT_EQUAL_UINT32(11, after[1]);  // sf 19..29
    TEST_ASSERT_EQUAL_UINT32(11, after[2]);
    TEST_ASSERT_TRUE(first_after[1] < 20 * (uint64_t)SUPERFRAME_US);  // the very first superframe after the outage
    TEST_ASSERT_TRUE(first_after[2] < 20 * (uint64_t)SUPERFRAME_US);
    TEST_ASSERT_TRUE(s.slave(1).stats().free_runs >= MAX_MISSED_BEACONS);
    TEST_ASSERT_FALSE(s.slave(1).lost());
    TEST_ASSERT_FALSE(s.slave(2).lost());
    TEST_ASSERT_EQUAL_UINT32(0, s.slave(1).stats().beacons_rejected);
}

void test_sim_timed_kill_revive(void) {
    // Kill slave 1 during its sampling window (sf 20 + 0.3 s), revive it at sf 22 + 0.5 s.
    netsim::SimConfig c = base_cfg(12, 30);
    netsim::TimedAction k = {1, 20 * (uint64_t)SUPERFRAME_US + 300000}, v = {1, 22 * (uint64_t)SUPERFRAME_US + 500000};
    c.kills_us.push_back(k);
    c.revives_us.push_back(v);
    netsim::Sim s(c);
    s.run();
    for (size_t i = 0; i < s.res.tx.size(); i++)
        if (s.res.tx[i].node == 1) TEST_ASSERT_TRUE(s.res.tx[i].t < k.t_us || s.res.tx[i].t >= v.t_us);
    uint32_t got[40] = {0};
    for (size_t i = 0; i < s.res.reports.size(); i++)
        if (s.res.reports[i].slave == 1) got[s.res.reports[i].sf]++;
    TEST_ASSERT_EQUAL_UINT32(1, got[19]);
    TEST_ASSERT_EQUAL_UINT32(0, got[20]);   // died mid-window
    TEST_ASSERT_EQUAL_UINT32(0, got[21]);
    TEST_ASSERT_EQUAL_UINT32(0, got[22]);   // revived after this beacon: waits for the next
    TEST_ASSERT_EQUAL_UINT32(1, got[23]);
    TEST_ASSERT_EQUAL_UINT32(1, s.res.death.size());
    TEST_ASSERT_EQUAL_UINT32(k.t_us, (uint32_t)s.res.death[0].cause_t);
    TEST_ASSERT_EQUAL_UINT32(0, s.res.viol_tx_in_sampling);
}

// Master late by up to 8 ms on EVERY wakeup: mean beacon period stays 1.4 s, and the effect on
// the slaves (false free-running => UNSYNCED flag) is measured.
void test_sim_master_tick_lateness(void) {
    netsim::SimConfig c = base_cfg(21, 1500);
    c.quiet = true;
    c.master_late_max_us = 8000;
    netsim::Sim s(c);
    s.run();
    const std::vector<uint64_t> &bt = s.res.beacon_times;
    TEST_ASSERT_EQUAL_UINT(1500, bt.size());
    double mean = (double)(bt.back() - bt.front()) / (double)(bt.size() - 1);
    uint32_t rep = s.res.reports_delivered[1] + s.res.reports_delivered[2];
    double unsynced_rate = (double)s.res.unsynced_reports / (double)rep;
    double false_free_run_rate = (double)(s.slave(1).stats().free_runs + s.slave(2).stats().free_runs) / (2.0 * 1500.0);
    printf("master ticks 0..8 ms late: mean period %.1f us, delivered %u/%u, UNSYNCED rate %.4f, false free-run rate %.4f, DEAD events %u, overrun %u\n", mean, rep,
           2u * 1500u, unsynced_rate, false_free_run_rate, s.master().event_count(), s.res.viol_poll_overrun);
    TEST_ASSERT_TRUE(fabs(mean - (double)SUPERFRAME_US) < 10.0);
    TEST_ASSERT_EQUAL_UINT32(0, s.master().stats().beacons_skipped);
    TEST_ASSERT_EQUAL_UINT32(0, s.res.viol_poll_overrun);
    TEST_ASSERT_EQUAL_UINT32(0, s.res.viol_tx_in_sampling);
    TEST_ASSERT_TRUE(rep >= 2u * 1500u * 99u / 100u);
    TEST_ASSERT_EQUAL_UINT32(0, s.master().event_count());
    TEST_ASSERT_TRUE(false_free_run_rate > 0.03 && false_free_run_rate < 0.12);  // 5 ms grace < 8 ms lateness: documented limit
    // Every false free-run stays visible to the master as UNSYNCED (lossless: each window is delivered).
    uint32_t fr = s.slave(1).stats().free_runs + s.slave(2).stats().free_runs;
    TEST_ASSERT_TRUE(s.res.unsynced_reports + 2 >= fr && s.res.unsynced_reports <= fr);
    TEST_ASSERT_TRUE(unsynced_rate > 0.03 && unsynced_rate < 0.12);
}

// Master host stalls (blocked loop) of 2.8-8.5 s used to leave its superframe number behind the
// slaves' free-run count, so its beacons were rejected as stale: healthy slaves went DEAD, or a
// stale window was reported. With the number kept on the grid, recovery is prompt for any stall.
void test_sim_master_stall(void) {
    const uint64_t stalls_ms[] = {500, 2900, 3500, 4300, 6000, 8400, 9000};
    for (size_t i = 0; i < sizeof(stalls_ms) / sizeof(stalls_ms[0]); i++) {
        netsim::SimConfig c = base_cfg(7, 30);
        c.quiet = true;
        uint64_t from = 10ull * SUPERFRAME_US + 1000;  // just after beacon 10
        uint64_t until = from + stalls_ms[i] * 1000ull;
        c.master_stall_from_us = from;
        c.master_stall_until_us = until;
        netsim::Sim s(c);
        s.run();
        uint32_t rejected = s.slave(1).stats().beacons_rejected + s.slave(2).stats().beacons_rejected;
        // DEAD can legitimately fire for stalls longer than 3 missed superframes (the master really
        // polled nobody); it must never fire for a shorter stall.
        bool short_stall = stalls_ms[i] * 1000ull < 2ull * SUPERFRAME_US;
        printf("stall %llu ms: beacons rejected %u, status events %u, skipped %u\n", (unsigned long long)stalls_ms[i], rejected,
               s.master().event_count(), s.master().stats().beacons_skipped);
        TEST_ASSERT_EQUAL_UINT32(0, rejected);
        if (short_stall) TEST_ASSERT_EQUAL_UINT32(0, s.master().event_count());
        // Both slaves report again within 2 superframes after the stall ends, and every accepted
        // report after the stall that is not UNSYNCED was sampled from a beacon of its superframe.
        for (int sl = 1; sl <= 2; sl++) {
            uint64_t first = 0;
            for (size_t k = 0; k < s.res.reports.size(); k++) {
                const netsim::ReportRec &rr = s.res.reports[k];
                if (rr.slave == sl && rr.t >= until) { first = rr.t; break; }
            }
            TEST_ASSERT_TRUE(first != 0);
            TEST_ASSERT_TRUE(first - until <= 2ull * SUPERFRAME_US);
        }
        for (size_t k = 0; k < s.res.reports.size(); k++) {
            const netsim::ReportRec &rr = s.res.reports[k];
            TEST_ASSERT_EQUAL_UINT32(rr.master_sf, rr.sf);  // never a stale superframe
        }
    }
}

// ------------------------------------------------------------------ wire version 3: master validation of the optional fields
void test_master_optional_fields_dropped_but_report_kept(void) {
    const uint32_t nanb = 0x7FC00000u, infb = 0x7F800000u;
    float nanf, inff;
    memcpy(&nanf, &nanb, 4);
    memcpy(&inff, &infb, 4);
    const uint8_t ALL = FLAG_AE_READY | FLAG_F1_VALID | FLAG_F1_COMMISSIONED;
    struct Case { uint8_t flags; float ae, f1, drop; uint8_t want; uint32_t ae_bad, f1_bad; };
    const Case cases[] = {
        {ALL, 7.5f, 5.25f, 0.0125f, ALL, 0, 0},                                            // all good: untouched
        {ALL, nanf, 5.25f, 0.0125f, (uint8_t)(ALL & ~FLAG_AE_READY), 1, 0},                // NaN ae
        {ALL, inff, 5.25f, 0.0125f, (uint8_t)(ALL & ~FLAG_AE_READY), 1, 0},                // inf ae
        {ALL, -0.5f, 5.25f, 0.0125f, (uint8_t)(ALL & ~FLAG_AE_READY), 1, 0},               // a negative ratio cannot exist
        {ALL, 7.5f, nanf, 0.0125f, (uint8_t)(ALL & ~FLAG_F1_VALID), 0, 1},                 // NaN f1: valid cleared (the drop is moot)
        {ALL, 7.5f, inff, nanf, (uint8_t)(ALL & ~FLAG_F1_VALID), 0, 1},
        {ALL, 7.5f, 150.0f, 0.0125f, (uint8_t)(ALL & ~FLAG_F1_VALID), 0, 1},               // above any plausible f1
        {ALL, 7.5f, -1.0f, 0.0125f, (uint8_t)(ALL & ~FLAG_F1_VALID), 0, 1},
        {ALL, 7.5f, 5.25f, nanf, (uint8_t)(ALL & ~FLAG_F1_COMMISSIONED), 0, 1},            // valid f1, unusable drop
        {ALL, 7.5f, 5.25f, 1.5f, (uint8_t)(ALL & ~FLAG_F1_COMMISSIONED), 0, 1},            // |drop| > 1
        {ALL, nanf, nanf, nanf, FLAG_F1_COMMISSIONED, 1, 1},                            // everything bad: report still kept (commissioned = f1_0 exists)
        {0, nanf, nanf, nanf, 0, 0, 0},                                                     // flags clear: the floats are ignored, not counted
        {FLAG_F1_VALID, 0.0f, 5.25f, nanf, FLAG_F1_VALID, 0, 0},                            // valid without commissioned: the drop is not used
        {FLAG_F1_COMMISSIONED, 0.0f, nanf, nanf, FLAG_F1_COMMISSIONED, 0, 0},               // commissioned but this window invalid
        {FLAG_F1_NOT_COMMISSIONED | FLAG_LOW_SIGNAL, 0.0f, nanf, nanf, FLAG_F1_NOT_COMMISSIONED | FLAG_LOW_SIGNAL, 0, 0},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        MasterRig r;
        r.to_polling();
        ReportPkt p = good_report(0);
        p.flags = cases[i].flags;
        p.ae_ratio = cases[i].ae;
        p.f1_est = cases[i].f1;
        p.f1_drop = cases[i].drop;
        p.f[0] = 123.0f;
        r.inject(p, 1, 100);
        char msg[48];
        snprintf(msg, sizeof msg, "case %u", (unsigned)i);
        TEST_ASSERT_EQUAL_UINT_MESSAGE(1, r.rec.rep_sf.size(), msg);         // the report itself is kept
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0, r.m.stats().rx_bad_field, msg);
        TEST_ASSERT_TRUE_MESSAGE(r.m.has_report(1), msg);
        TEST_ASSERT_EQUAL_FLOAT_MESSAGE(123.0f, r.m.latest(1).f[0], msg);    // rule b's input intact
        TEST_ASSERT_EQUAL_HEX8_MESSAGE(cases[i].want, r.m.latest(1).flags, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(cases[i].ae_bad, r.m.stats().rx_ae_bad, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(cases[i].f1_bad, r.m.stats().rx_f1_bad, msg);
    }
    // a non-finite CORE feature is still a rejected report, whatever the optional fields say
    MasterRig q;
    q.to_polling();
    ReportPkt bad = good_report(0);
    bad.f[0] = nanf;
    q.inject(bad, 1, 100);
    TEST_ASSERT_EQUAL_UINT32(1, q.m.stats().rx_bad_field);
    TEST_ASSERT_EQUAL_UINT(0, q.rec.rep_sf.size());
}

// ------------------------------------------------------------------ slave analytics (slave_pipe.h through SlaveNode)
static void gen_sine_window(int16_t *az, int16_t *gx, uint32_t seed, double hz, double amp) {
    uint32_t x = seed * 2654435761u + 1u;
    for (int i = 0; i < 128; i++) {
        x = x * 1664525u + 1013904223u;
        double nz = ((x >> 8) & 0xFFFF) / 65535.0 - 0.5;
        az[i] = (int16_t)(16384.0 + amp * sin(6.283185307179586 * hz * i * 0.01 + 0.3) + 40.0 * nz);
        x = x * 1664525u + 1013904223u;
        gx[i] = (int16_t)(60.0 * sin(6.283185307179586 * (hz + 1.0) * i * 0.01) + 20.0 * (((x >> 8) & 0xFFFF) / 65535.0 - 0.5));
    }
}

void test_slave_pipeline_flags_and_commissioning(void) {
    static SlaveAnalytics pa;
    SlaveRig r;
    r.s.attach(&pa);
    TEST_ASSERT_EQUAL_UINT(0, (unsigned)pa.ae.n);
    int16_t az[128], gx[128];
    uint32_t first_ae = 0, first_f1 = 0, first_comm = 0;
    for (uint32_t w = 0; w < 110; w++) {
        gen_sine_window(az, gx, w, 6.0, 600.0);
        r.run_window((uint64_t)w * SUPERFRAME_US, az, gx, w);
        const ReportPkt &rp = r.s.report();
        if ((rp.flags & FLAG_AE_READY) && !first_ae) first_ae = w + 1;
        if ((rp.flags & FLAG_F1_VALID) && !first_f1) first_f1 = w + 1;
        if ((rp.flags & FLAG_F1_COMMISSIONED) && !first_comm) first_comm = w + 1;
        if (!(rp.flags & FLAG_AE_READY)) TEST_ASSERT_EQUAL_FLOAT(0.0f, rp.ae_ratio);
        else TEST_ASSERT_TRUE(rp.ae_ratio >= 0.0f && rp.ae_ratio < 1e6f);
        if (rp.flags & FLAG_F1_VALID) TEST_ASSERT_FLOAT_WITHIN(0.3f, 6.0f, rp.f1_est);
        else TEST_ASSERT_TRUE(rp.f1_est != rp.f1_est);                     // NAN on the wire when not valid
        if (rp.flags & FLAG_F1_COMMISSIONED) TEST_ASSERT_TRUE(fabsf(rp.f1_drop) < 0.05f);
        if (!((rp.flags & FLAG_F1_VALID) && (rp.flags & FLAG_F1_COMMISSIONED))) TEST_ASSERT_TRUE(rp.f1_drop != rp.f1_drop);
        TEST_ASSERT_EQUAL_HEX8(0, rp.flags & FLAG_F1_NOT_COMMISSIONED);     // a clean tone commissions in time
        TEST_ASSERT_EQUAL_HEX8(0, rp.flags & ~FLAGS_VALID);
    }
    printf("slave pipeline: AE_READY from window %u, F1_VALID from %u, F1_COMMISSIONED from %u\n", (unsigned)first_ae, (unsigned)first_f1, (unsigned)first_comm);
    TEST_ASSERT_EQUAL_UINT32(21, first_ae);       // windows 1..20 commission, the ratio starts with the 21st
    TEST_ASSERT_EQUAL_UINT32(32, first_f1);       // M = 32 spectra in the ring
    TEST_ASSERT_TRUE(first_comm > 0 && first_comm <= 32 + 64);   // M + C - 1 = 95 windows on a clean tone
    TEST_ASSERT_EQUAL_UINT32(0, r.s.stats().windows_rejected);
    // reset() is a power-on: both recommission
    r.s.reset();
    TEST_ASSERT_EQUAL_UINT(0, (unsigned)pa.ae.n);
    TEST_ASSERT_EQUAL_INT(0, pa.f1.commissioned);
    TEST_ASSERT_EQUAL_INT(0, pa.f1.n_windows);
    TEST_ASSERT_TRUE(r.s.analytics() == &pa);
}

void test_slave_pipeline_not_commissioned_flag(void) {
    static SlaveAnalytics pa;
    SlaveRig r;
    r.s.attach(&pa);
    int16_t az[128], gx[128];
    uint32_t first = 0;
    for (int i = 0; i < 128; i++) { az[i] = 16384; gx[i] = 0; }       // a flat signal has no peak: the tracker never commissions
    for (uint32_t w = 0; w < 300; w++) {
        r.run_window((uint64_t)w * SUPERFRAME_US, az, gx, w);
        const ReportPkt &rp = r.s.report();
        TEST_ASSERT_EQUAL_HEX8(0, rp.flags & (FLAG_F1_VALID | FLAG_F1_COMMISSIONED));
        if ((rp.flags & FLAG_F1_NOT_COMMISSIONED) && !first) first = w + 1;
    }
    TEST_ASSERT_EQUAL_UINT32(32 + 4 * 64, first);    // overdue after M + F1T_PATIENCE * C windows
}

void test_slave_pipeline_does_not_change_tree_outputs(void) {
    load_windows();
    static SlaveAnalytics pa;
    SlaveRig with, without;
    with.s.attach(&pa);
    for (uint32_t w = 0; w < 60; w++) {
        const Win &x = g_win[w * 97 % g_win.size()];
        with.run_window((uint64_t)w * SUPERFRAME_US, x.az, x.gx, w);
        without.run_window((uint64_t)w * SUPERFRAME_US, x.az, x.gx, w);
        const ReportPkt &a = with.s.report(), &b = without.s.report();
        TEST_ASSERT_EQUAL_UINT8(b.raw, a.raw);
        TEST_ASSERT_EQUAL_UINT8(b.state, a.state);
        TEST_ASSERT_EQUAL_HEX8(b.flags, a.flags & (FLAG_LOW_SIGNAL | FLAG_UNSYNCED));
        for (int i = 0; i < 5; i++) TEST_ASSERT_EQUAL_MEMORY(&b.f[i], &a.f[i], 4);   // features_ex == features, bit for bit
        // no pipeline attached: the slave says nothing about the AE or f1
        TEST_ASSERT_EQUAL_HEX8(0, b.flags & ~(FLAG_LOW_SIGNAL | FLAG_UNSYNCED));
        TEST_ASSERT_EQUAL_FLOAT(0.0f, b.ae_ratio);
        TEST_ASSERT_TRUE(b.f1_est != b.f1_est && b.f1_drop != b.f1_drop);
    }
}

void test_sim_analytics_reports(void) {
    netsim::SimConfig c = base_cfg(3, 60);
    c.quiet = true;
    netsim::Sim on(c);
    on.run();
    c.analytics = false;
    netsim::Sim off(c);
    off.run();
    TEST_ASSERT_EQUAL_UINT32(on.res.reports_delivered[1], off.res.reports_delivered[1]);
    uint32_t ready = 0;
    for (size_t i = 0; i < on.res.reports.size(); i++) {
        const netsim::ReportRec &rr = on.res.reports[i];
        TEST_ASSERT_EQUAL_HEX8(0, rr.flags & ~FLAGS_VALID);
        if (rr.flags & FLAG_AE_READY) { ready++; TEST_ASSERT_TRUE(rr.ae_ratio >= 0.0f); }
        else TEST_ASSERT_EQUAL_FLOAT(0.0f, rr.ae_ratio);
        if (rr.window < 20) TEST_ASSERT_EQUAL_HEX8(0, rr.flags & FLAG_AE_READY);
        if (rr.window >= 20) TEST_ASSERT_EQUAL_HEX8(FLAG_AE_READY, rr.flags & FLAG_AE_READY);
    }
    TEST_ASSERT_TRUE(ready > 60);                  // both slaves, windows 20..59
    for (size_t i = 0; i < off.res.reports.size(); i++) TEST_ASSERT_EQUAL_HEX8(0, off.res.reports[i].flags & ~(FLAG_LOW_SIGNAL | FLAG_UNSYNCED));
    // the pipeline's host time is measured, not modelled
    TEST_ASSERT_TRUE(on.res.host_windows >= 100);
    TEST_ASSERT_TRUE(on.res.host_window_ns_total > 0);
    // a revived slave is a power-on: it recommissions (AE not ready again)
    netsim::SimConfig k = base_cfg(3, 60);
    k.quiet = true;
    k.kills.push_back({2, 30});
    k.revives.push_back({2, 32});
    netsim::Sim s(k);
    s.run();
    bool after = false;
    for (size_t i = 0; i < s.res.reports.size(); i++) {
        const netsim::ReportRec &rr = s.res.reports[i];
        if (rr.slave == 2 && rr.sf >= 33) {   // the revived slave's window counter and AE restart: ready again only from its 21st window
            if (rr.window < 20) { after = true; TEST_ASSERT_EQUAL_HEX8(0, rr.flags & FLAG_AE_READY); }
            else TEST_ASSERT_EQUAL_HEX8(FLAG_AE_READY, rr.flags & FLAG_AE_READY);
        }
    }
    TEST_ASSERT_TRUE(after);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_crc_check_value);
    RUN_TEST(test_sizes);
    RUN_TEST(test_roundtrip);
    RUN_TEST(test_bitflip_rejected);
    RUN_TEST(test_version_and_length);
    RUN_TEST(test_report_v2_frame_rejected_and_flag_bits);
    RUN_TEST(test_wrong_length_with_valid_crc);
    RUN_TEST(test_wrong_type_with_valid_crc);
    RUN_TEST(test_master_optional_fields_dropped_but_report_kept);
    RUN_TEST(test_slave_pipeline_flags_and_commissioning);
    RUN_TEST(test_slave_pipeline_not_commissioned_flag);
    RUN_TEST(test_slave_pipeline_does_not_change_tree_outputs);
    RUN_TEST(test_sim_analytics_reports);
    RUN_TEST(test_gate_threshold);
    RUN_TEST(test_gate_unchanged_on_dataset);
    RUN_TEST(test_slave_addressing_and_ready);
    RUN_TEST(test_slave_retry_and_smoothing);
    RUN_TEST(test_slave_low_signal_flag);
    RUN_TEST(test_slave_gate_before_smoothing);
    RUN_TEST(test_slave_unsynced_free_run_and_limit);
    RUN_TEST(test_slave_duplicate_beacon);
    RUN_TEST(test_slave_silent_while_busy);
    RUN_TEST(test_slave_late_beacon_resync);
    RUN_TEST(test_slave_epoch_restart);
    RUN_TEST(test_slave_huge_sf_beacon);
    RUN_TEST(test_slave_sf_jump_window);
    RUN_TEST(test_slave_survives_lost_beacons);
    RUN_TEST(test_slave_lost_accepts_any_beacon);
    RUN_TEST(test_slave_window_token);
    RUN_TEST(test_slave_tx_failure);
    RUN_TEST(test_master_retry_exactly_once);
    RUN_TEST(test_master_duplicates_and_stale);
    RUN_TEST(test_master_dead_and_recovery);
    RUN_TEST(test_master_rejects_after_polling_closed);
    RUN_TEST(test_master_source_and_destination);
    RUN_TEST(test_master_epoch);
    RUN_TEST(test_master_field_validation);
    RUN_TEST(test_master_only_polled_slaves);
    RUN_TEST(test_master_tx_failure);
    RUN_TEST(test_master_fixed_beacon_grid);
    RUN_TEST(test_master_poll_guard_when_late);
    RUN_TEST(test_master_skips_forward_when_far_behind);
    RUN_TEST(test_sim_determinism);
    RUN_TEST(test_sim_seed_drives_network);
    RUN_TEST(test_sim_lossless);
    RUN_TEST(test_sim_bytes_count_retries);
    RUN_TEST(test_sim_corruption);
    RUN_TEST(test_sim_jitter);
    RUN_TEST(test_sim_kill_and_revive);
    RUN_TEST(test_sim_revive_resets_slave);
    RUN_TEST(test_sim_slave_access);
    RUN_TEST(test_sim_quiet_keeps_no_log);
    RUN_TEST(test_sim_loss_30);
    RUN_TEST(test_sim_invariants);
    RUN_TEST(test_sim_audit_detects_violations);
    RUN_TEST(test_sim_abandoned_window_rejected);
    RUN_TEST(test_sim_tx_failures);
    RUN_TEST(test_master_restart_recovery);
    RUN_TEST(test_master_restart_under_loss);
    RUN_TEST(test_sim_lost_slaves_resync);
    RUN_TEST(test_sim_timed_kill_revive);
    RUN_TEST(test_sim_master_tick_lateness);
    RUN_TEST(test_sim_master_stall);
    return UNITY_END();
}
