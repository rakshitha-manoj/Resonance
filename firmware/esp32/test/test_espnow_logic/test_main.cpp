#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <unity.h>
#include "espnow_logic.h"
#include "tiers.h"

using namespace net;
using namespace net::host;

void setUp(void) {}
void tearDown(void) {}

static const uint8_t MAC_M[6] = {0x24, 0x6F, 0x28, 0, 0, 0x10};
static const uint8_t MAC_1[6] = {0x24, 0x6F, 0x28, 0, 0, 0x11};
static const uint8_t MAC_2[6] = {0x24, 0x6F, 0x28, 0, 0, 0x12};
static const uint8_t MAC_X[6] = {0x24, 0x6F, 0x28, 0, 0, 0x99};

static size_t beacon_from(uint8_t src, uint8_t *b, uint32_t sf = 1) {
    return encode_beacon(b, src, ID_BROADCAST, 1, sf, 0x1234u);
}

void test_prefilter() {
    uint8_t b[MAX_PAYLOAD + 4];
    size_t n = beacon_from(0, b);
    TEST_ASSERT_TRUE(prefilter(b, (int)n));
    TEST_ASSERT_FALSE(prefilter(b, (int)(HDR_LEN + CRC_LEN - 1)));
    TEST_ASSERT_FALSE(prefilter(b, (int)MAX_PAYLOAD + 1));
    // exact boundaries (b has MAX_PAYLOAD + 4 bytes and b[0] == MAGIC)
    TEST_ASSERT_TRUE(prefilter(b, (int)(HDR_LEN + CRC_LEN)));      // min valid length accepted
    TEST_ASSERT_TRUE(prefilter(b, (int)MAX_PAYLOAD));              // max valid length accepted
    TEST_ASSERT_FALSE(prefilter(b, (int)(HDR_LEN + CRC_LEN) - 1)); // one below min rejected
    TEST_ASSERT_FALSE(prefilter(b, (int)MAX_PAYLOAD + 1));         // one above max rejected
    TEST_ASSERT_FALSE(prefilter(b, 0));
    TEST_ASSERT_FALSE(prefilter(b, -1));
    TEST_ASSERT_FALSE(prefilter(nullptr, 20));
    b[0] ^= 0xFF;
    TEST_ASSERT_FALSE(prefilter(b, (int)n));
}

void test_tofu_pins_first_mac() {
    RxFilter f(1);
    uint8_t b[64];
    size_t n = beacon_from(0, b);
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_M, b, n));
    TEST_ASSERT_EQUAL(1, f.pins.pins_made());
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_M, b, n));  // same MAC again
    TEST_ASSERT_EQUAL(1, f.pins.pins_made());
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_X, b, n));  // other MAC claiming id 0
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_X, b, n));
    TEST_ASSERT_EQUAL(2, f.count[RX_MAC_MISMATCH]);
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_M, b, n));  // the pinned one still works
    TEST_ASSERT_EQUAL(0, memcmp(f.pins.mac(0), MAC_M, 6));
}

void test_tofu_pins_only_after_valid_crc() {
    RxFilter f(1);
    uint8_t b[64];
    size_t n = beacon_from(0, b);
    b[10] ^= 1;  // corrupt payload, CRC now wrong
    TEST_ASSERT_EQUAL(RX_BAD_FRAME, f.accept(MAC_X, b, n));
    TEST_ASSERT_FALSE(f.pins.known(0));
    TEST_ASSERT_EQUAL(0, f.pins.pins_made());
    b[10] ^= 1;
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_M, b, n));  // the real master can still pin
    TEST_ASSERT_EQUAL(0, memcmp(f.pins.mac(0), MAC_M, 6));
}

void test_pins_per_id_independent() {
    RxFilter f(0);  // master view
    uint8_t b[64], c[64];
    size_t n1 = beacon_from(1, b);
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_1, b, n1));
    size_t n2 = beacon_from(2, c);
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_2, c, n2));
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_2, b, n1));  // MAC of id 2 claiming id 1
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_1, c, n2));
    TEST_ASSERT_EQUAL(2, f.pins.pins_made());
}

void test_bad_id_own_id_bad_mac() {
    RxFilter f(1);
    uint8_t b[64];
    size_t n = beacon_from(7, b);
    TEST_ASSERT_EQUAL(RX_BAD_ID, f.accept(MAC_X, b, n));
    n = beacon_from(ID_BROADCAST, b);
    TEST_ASSERT_EQUAL(RX_BAD_ID, f.accept(MAC_X, b, n));
    n = beacon_from(1, b);  // we are node 1
    TEST_ASSERT_EQUAL(RX_OWN_ID, f.accept(MAC_1, b, n));
    n = beacon_from(0, b);
    const uint8_t bc[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    const uint8_t zero[6] = {0, 0, 0, 0, 0, 0};
    const uint8_t mc[6] = {0x01, 0x00, 0x5E, 0, 0, 1};
    TEST_ASSERT_EQUAL(RX_BAD_MAC, f.accept(bc, b, n));
    TEST_ASSERT_EQUAL(RX_BAD_MAC, f.accept(zero, b, n));
    TEST_ASSERT_EQUAL(RX_BAD_MAC, f.accept(mc, b, n));
    TEST_ASSERT_FALSE(f.pins.known(0));
}

void test_allowlist() {
    RxFilter f(1);
    TEST_ASSERT_TRUE(f.pins.allow(0, MAC_M));
    TEST_ASSERT_FALSE(f.pins.allow(5, MAC_M));  // bad id
    const uint8_t mc[6] = {0x01, 2, 3, 4, 5, 6};
    TEST_ASSERT_FALSE(f.pins.allow(2, mc));  // multicast not allowed in a list
    uint8_t b[64];
    size_t n = beacon_from(0, b);
    // first frame comes from the wrong MAC: with an allowlist it must NOT win the pin
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_X, b, n));
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_M, b, n));
    TEST_ASSERT_EQUAL(0, f.pins.pins_made());  // allowlisted, nothing pinned dynamically
    TEST_ASSERT_TRUE(f.pins.is_fixed(0));
    // id 2 has no entry: still trust on first use
    uint8_t c[64];
    size_t n2 = beacon_from(2, c);
    TEST_ASSERT_EQUAL(RX_OK, f.accept(MAC_2, c, n2));
    TEST_ASSERT_EQUAL(RX_MAC_MISMATCH, f.accept(MAC_X, c, n2));
}

struct NullTx : Transport {
    bool broadcast(const uint8_t *, size_t, uint64_t) override { return true; }
};

void test_filtered_frames_never_reach_state_machine() {
    // end to end: a forged beacon from a second MAC with the master's id must not move the slave
    NullTx tx;
    SlaveNode s(1, &tx);
    RxFilter f(1);
    uint8_t b[64];
    size_t n = encode_beacon(b, 0, ID_BROADCAST, 1, 5, 0xAAAAu);
    if (f.accept(MAC_M, b, n) == RX_OK) s.on_packet(b, n, 1000);
    TEST_ASSERT_TRUE(s.synced());
    TEST_ASSERT_EQUAL(5, s.last_sf());
    n = encode_beacon(b, 0, ID_BROADCAST, 2, 6, 0xBBBBu);  // forged epoch, other MAC
    if (f.accept(MAC_X, b, n) == RX_OK) s.on_packet(b, n, 2000);
    TEST_ASSERT_EQUAL(0xAAAAu, s.epoch());
    TEST_ASSERT_EQUAL(5, s.last_sf());
}

void test_id_bounds_num_nodes() {
    // ids are 0..NUM_SLAVES; NUM_NODES itself is one past the end of the arrays
    TEST_ASSERT_EQUAL(NUM_SLAVES + 1, NUM_NODES);
    PeerPins p(0);
    TEST_ASSERT_TRUE(p.allow(NUM_NODES - 1, MAC_2));
    TEST_ASSERT_FALSE(p.allow(NUM_NODES, MAC_X));
    TEST_ASSERT_FALSE(p.known(NUM_NODES));
    TEST_ASSERT_FALSE(p.is_fixed(NUM_NODES));
    TEST_ASSERT_EQUAL(RX_BAD_ID, p.check(NUM_NODES, MAC_X));
    TEST_ASSERT_EQUAL(RX_OK, p.check(NUM_NODES - 1, MAC_2));
    TEST_ASSERT_EQUAL(RX_BAD_ID, p.check(255, MAC_X));
    // through the full filter: a CRC-valid frame whose src is exactly NUM_NODES
    RxFilter f(1);
    uint8_t b[64];
    size_t n = beacon_from(NUM_NODES, b);
    TEST_ASSERT_EQUAL(RX_BAD_ID, f.accept(MAC_X, b, n));
    TEST_ASSERT_EQUAL(1, f.count[RX_BAD_ID]);
}

void test_rx_reason_names() {
    const char *want[] = {"ok", "not_proto", "bad_frame", "bad_id", "own_id", "bad_mac", "mac_mismatch"};
    TEST_ASSERT_EQUAL(7, RX_REASONS);
    TEST_ASSERT_EQUAL_STRING(want[RX_OK], rx_reason_name(RX_OK));
    TEST_ASSERT_EQUAL_STRING("not_proto", rx_reason_name(RX_NOT_PROTO));
    TEST_ASSERT_EQUAL_STRING("bad_frame", rx_reason_name(RX_BAD_FRAME));
    TEST_ASSERT_EQUAL_STRING("bad_id", rx_reason_name(RX_BAD_ID));
    TEST_ASSERT_EQUAL_STRING("own_id", rx_reason_name(RX_OWN_ID));
    TEST_ASSERT_EQUAL_STRING("bad_mac", rx_reason_name(RX_BAD_MAC));
    TEST_ASSERT_EQUAL_STRING("mac_mismatch", rx_reason_name(RX_MAC_MISMATCH));
    TEST_ASSERT_EQUAL_STRING("?", rx_reason_name(-1));
    TEST_ASSERT_EQUAL_STRING("?", rx_reason_name(RX_REASONS));
}

void test_deadlines_64bit() {
    TEST_ASSERT_EQUAL_UINT64(1000000, sample_deadline(0, 100));
    // t0 just below a 32-bit wrap (micros() would wrap mid-window), one beyond 2^32, one far beyond
    uint64_t starts[3] = {0xFFFFFFFFull - 5000000ull, 0x100000000ull + 123, 0x7FFFFFFFFFFFull};
    for (int k = 0; k < 3; k++) {
        uint64_t t0 = starts[k];
        WindowTimer wt;
        wt.reset(t0);
        for (uint32_t i = 0; i < WIN_SAMPLES; i++) {
            TEST_ASSERT_EQUAL_UINT64(t0 + (uint64_t)i * 10000, sample_deadline(t0, i));
            wt.add(i, sample_deadline(t0, i) + (i == 50 ? 400 : 3));
        }
        TEST_ASSERT_EQUAL(128, wt.n);
        TEST_ASSERT_EQUAL_INT64(400, wt.max_miss);
        TEST_ASSERT_EQUAL_INT64(3, wt.min_miss);
        // first read 3 us late, last 3 us late => span exactly the ideal
        TEST_ASSERT_EQUAL_INT64((int64_t)SAMPLE_SPAN_US, wt.span());
    }
}

void test_timer_early_and_late_edges() {
    WindowTimer wt;
    wt.reset(5000);
    wt.add(0, 4990);  // 10 us early: negative miss
    wt.add(1, 15000 + 250);
    TEST_ASSERT_EQUAL_INT64(250, wt.max_miss);
    TEST_ASSERT_EQUAL_INT64(-10, wt.min_miss);
    TEST_ASSERT_EQUAL_INT64(10260, wt.span());
}

void test_rtt_hist() {
    RttHist h;
    TEST_ASSERT_EQUAL(0, RttHist::bucket(0));
    TEST_ASSERT_EQUAL(0, RttHist::bucket(3999));
    TEST_ASSERT_EQUAL(1, RttHist::bucket(4000));
    TEST_ASSERT_EQUAL(1, RttHist::bucket(5999));
    TEST_ASSERT_EQUAL(2, RttHist::bucket(6000));
    TEST_ASSERT_EQUAL(7, RttHist::bucket(17999));
    TEST_ASSERT_EQUAL(8, RttHist::bucket(18000));
    TEST_ASSERT_EQUAL(8, RttHist::bucket(4000000));
    h.add(5000);
    h.add(5100);
    h.add(20000);
    TEST_ASSERT_EQUAL(3, h.n);
    TEST_ASSERT_EQUAL(2, h.b[1]);
    TEST_ASSERT_EQUAL(1, h.b[8]);
    TEST_ASSERT_EQUAL(5000, h.min_us);
    TEST_ASSERT_EQUAL(20000, h.max_us);
    TEST_ASSERT_EQUAL_UINT64(30100, h.sum_us);
}

static void poll(RttTracker &t, uint8_t slave, uint8_t attempt, uint64_t at) { t.on_poll(slave, attempt, at); }

void test_poll_info_parses_encoded_poll() {
    uint8_t b[MAX_PAYLOAD];
    size_t n = encode_poll(b, ID_MASTER, 2, 7, 33, 0xABCDu, 1);
    uint8_t s = 0, a = 99;
    TEST_ASSERT_TRUE(poll_info(b, n, &s, &a));
    TEST_ASSERT_EQUAL(2, s);
    TEST_ASSERT_EQUAL(1, a);
    n = encode_poll(b, ID_MASTER, 1, 8, 33, 0xABCDu, 0);
    TEST_ASSERT_TRUE(poll_info(b, n, &s, &a));
    TEST_ASSERT_EQUAL(1, s);
    TEST_ASSERT_EQUAL(0, a);
    TEST_ASSERT_FALSE(poll_info(b, n - 1, &s, &a));
    n = beacon_from(0, b);
    TEST_ASSERT_FALSE(poll_info(b, n, &s, &a));
    n = encode_poll(b, ID_MASTER, 0, 8, 33, 1u, 0);  // dst 0 is not a slave
    TEST_ASSERT_FALSE(poll_info(b, n, &s, &a));
    n = encode_poll(b, ID_MASTER, NUM_SLAVES + 1, 8, 33, 1u, 0);
    TEST_ASSERT_FALSE(poll_info(b, n, &s, &a));
}

void test_rtt_late_reply_after_retry_not_hidden() {
    // attempt 0 at t=1,000,000; the retry goes out 18 ms later; the reply to attempt 0 only arrives at
    // 19 ms after attempt 0. Old code timed it from the retry (about 1 ms). Must read 19 ms.
    RttTracker t;
    poll(t, 1, 0, 1000000);
    poll(t, 1, 1, 1000000 + REPLY_TIMEOUT_US);
    TEST_ASSERT_TRUE(t.on_report(1, 1000000 + 19000));
    TEST_ASSERT_EQUAL(1, t.hist.n);
    TEST_ASSERT_EQUAL(19000, t.hist.min_us);
    TEST_ASSERT_EQUAL(19000, t.hist.max_us);
    TEST_ASSERT_EQUAL(1, t.hist.b[8]);  // >= 18 ms bucket
    TEST_ASSERT_EQUAL(1, t.after_retry);
    TEST_ASSERT_EQUAL(1, t.over_timeout);
}

void test_rtt_on_time_reply_and_retry_flags() {
    RttTracker t;
    poll(t, 1, 0, 5000);
    TEST_ASSERT_TRUE(t.on_report(1, 5000 + 6500));  // normal: 6.5 ms, no retry
    TEST_ASSERT_EQUAL(6500, t.hist.max_us);
    TEST_ASSERT_EQUAL(0, t.after_retry);
    TEST_ASSERT_EQUAL(0, t.over_timeout);
    // retry sent, reply arrives just BEFORE the retry time: not counted as after_retry
    poll(t, 2, 0, 100000);
    poll(t, 2, 1, 100000 + REPLY_TIMEOUT_US);
    TEST_ASSERT_TRUE(t.on_report(2, 100000 + REPLY_TIMEOUT_US - 1));
    TEST_ASSERT_EQUAL(0, t.after_retry);
    TEST_ASSERT_EQUAL(0, t.over_timeout);  // 17,999 us < timeout
    // reply exactly at the retry time counts as after the retry; rtt == timeout counts as over
    poll(t, 1, 0, 200000);
    poll(t, 1, 1, 200000 + REPLY_TIMEOUT_US);
    TEST_ASSERT_TRUE(t.on_report(1, 200000 + REPLY_TIMEOUT_US));
    TEST_ASSERT_EQUAL(1, t.after_retry);
    TEST_ASSERT_EQUAL(1, t.over_timeout);
}

void test_rtt_new_superframe_resets_and_unmatched() {
    RttTracker t;
    TEST_ASSERT_FALSE(t.on_report(1, 1000));  // no poll seen
    TEST_ASSERT_EQUAL(1, t.unmatched);
    poll(t, 1, 1, 500);                         // retry without attempt 0 is ignored
    TEST_ASSERT_FALSE(t.on_report(1, 1000));
    poll(t, 1, 0, 10000);
    poll(t, 1, 1, 10000 + REPLY_TIMEOUT_US);
    TEST_ASSERT_TRUE(t.on_report(1, 10000 + 3000));  // sf 1: reply to attempt 0 before retry
    poll(t, 1, 0, 2000000);                           // next superframe: stale retry time must not leak
    TEST_ASSERT_TRUE(t.on_report(1, 2000000 + 4000));
    TEST_ASSERT_EQUAL(4000, t.hist.max_us);
    TEST_ASSERT_EQUAL(0, t.after_retry);
    TEST_ASSERT_FALSE(t.on_report(1, 2000000 + 5000));  // one report per poll round
    TEST_ASSERT_FALSE(t.on_report(0, 1));
    TEST_ASSERT_FALSE(t.on_report(NUM_SLAVES + 1, 1));
    poll(t, NUM_SLAVES + 1, 0, 1);  // ignored, no out-of-bounds write
    TEST_ASSERT_EQUAL(2, t.hist.n);
}

void test_rtt_slaves_independent() {
    RttTracker t;
    poll(t, 1, 0, 1000);
    poll(t, 2, 0, 30000);
    TEST_ASSERT_TRUE(t.on_report(2, 30000 + 5000));
    TEST_ASSERT_TRUE(t.on_report(1, 1000 + 7000));
    TEST_ASSERT_EQUAL(2, t.hist.n);
    TEST_ASSERT_EQUAL(5000, t.hist.min_us);
    TEST_ASSERT_EQUAL(7000, t.hist.max_us);
}

void test_noise_stats() {
    float x[4] = {2, 4, 4, 6};
    NoiseStats s = noise_stats(x, 4);
    TEST_ASSERT_FLOAT_WITHIN(1e-5, 4.0f, s.mean);
    TEST_ASSERT_FLOAT_WITHIN(1e-5, sqrtf(2.0f), s.sd);
    TEST_ASSERT_FLOAT_WITHIN(1e-5, 2.0f, s.mn);
    TEST_ASSERT_FLOAT_WITHIN(1e-5, 6.0f, s.mx);
    NoiseStats z = noise_stats(x, 0);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, z.mean);
}

void test_queue_sizing() {
    TEST_ASSERT_EQUAL(9, MAX_LEGIT_FRAMES_PER_SUPERFRAME);
    // Requirement: 9 legit frames of one superframe + 1 early next beacon + 6 foreign frames = 16.
    TEST_ASSERT_EQUAL(16, RX_QUEUE_MIN_DEPTH);
    TEST_ASSERT_TRUE(RX_QUEUE_DEPTH >= RX_QUEUE_MIN_DEPTH);
    TEST_ASSERT_EQUAL(1 + 2 * NUM_SLAVES * POLL_ATTEMPTS, MAX_LEGIT_FRAMES_PER_SUPERFRAME);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_prefilter);
    RUN_TEST(test_tofu_pins_first_mac);
    RUN_TEST(test_tofu_pins_only_after_valid_crc);
    RUN_TEST(test_pins_per_id_independent);
    RUN_TEST(test_bad_id_own_id_bad_mac);
    RUN_TEST(test_allowlist);
    RUN_TEST(test_filtered_frames_never_reach_state_machine);
    RUN_TEST(test_id_bounds_num_nodes);
    RUN_TEST(test_rx_reason_names);
    RUN_TEST(test_poll_info_parses_encoded_poll);
    RUN_TEST(test_rtt_late_reply_after_retry_not_hidden);
    RUN_TEST(test_rtt_on_time_reply_and_retry_flags);
    RUN_TEST(test_rtt_new_superframe_resets_and_unmatched);
    RUN_TEST(test_rtt_slaves_independent);
    RUN_TEST(test_deadlines_64bit);
    RUN_TEST(test_timer_early_and_late_edges);
    RUN_TEST(test_rtt_hist);
    RUN_TEST(test_noise_stats);
    RUN_TEST(test_queue_sizing);
    return UNITY_END();
}
