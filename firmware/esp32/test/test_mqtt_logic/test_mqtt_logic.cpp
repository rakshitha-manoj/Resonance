#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <string>
#include <vector>
#include <unity.h>
#include "mqtt_logic.h"
#include "fusion_json.h"   // real schema-3 lines from the firmware formatter

using namespace mqtt;

void setUp(void) {}
void tearDown(void) {}

#define LINE(s) s, strlen(s)

static const char *META = "{\"type\":\"meta\",\"schema\":2,\"source\":\"master\"}";
static const char *RESTART = "{\"type\":\"restart\",\"t_us\":5,\"epoch\":1,\"prev_epoch\":null}";
static const char *SAMPLE = "{\"type\":\"sample\",\"t_us\":%d,\"sf\":%d}";
static const char *BEGIN = "{\"type\":\"begin\",\"t_us\":9,\"sf\":3}";
static const char *CHANGE = "{\"type\":\"change\",\"t_us\":9,\"sf\":4}";
static const char *STATUS = "{\"type\":\"status\",\"t_us\":9,\"sf\":5}";
static const char *NODE = "{\"type\":\"node\",\"t_us\":9,\"sf\":5,\"slave\":2,\"state\":\"DEAD\"}";
static const char *DAMAGE = "{\"type\":\"damage\",\"t_us\":9,\"sf\":5,\"ae\":0.31,\"f1\":7.25}";

static std::string sample(int i) {
    char b[96];
    snprintf(b, sizeof b, SAMPLE, i * 1000, i);
    return b;
}

static std::string topic(const char *line) {
    Route r = route_of(line, strlen(line));
    char t[TOPIC_MAX];
    size_t n = make_topic(t, sizeof t, "rsn-0123abcd", r.leaf);
    return n ? std::string(t) : std::string();
}

// ---------------------------------------------------------------- topics
void test_topic_mapping_known_types() {
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/meta", topic(META).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/restart", topic(RESTART).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/sample", topic(sample(1).c_str()).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/begin", topic(BEGIN).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/change", topic(CHANGE).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/node", topic(NODE).c_str());
}

void test_topic_unknown_future_type_routes_by_name() {
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage", topic(DAMAGE).c_str());
    Route r = route_of(LINE(DAMAGE));
    TEST_ASSERT_EQUAL(K_DAMAGE, r.kind);
    TEST_ASSERT_FALSE(r.to_state);
    TEST_ASSERT_TRUE(r.to_damage_state);   // latest damage line is kept on .../damage_state
    TEST_ASSERT_FALSE(r.retain);
    TEST_ASSERT_EQUAL(K_UNKNOWN_TYPE, route_of(LINE("{\"type\":\"freq_track-2\"}")).kind);
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/freq_track-2",
                             topic("{\"type\":\"freq_track-2\",\"x\":1}").c_str());
}

void test_topic_reserved_leaf_names_do_not_collide() {
    // the fusion `status` line must not land on the retained online/offline topic
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/status_line", topic(STATUS).c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/state_line", topic("{\"type\":\"state\",\"x\":1}").c_str());
}

void test_topic_flags() {
    Route m = route_of(LINE(META));
    TEST_ASSERT_TRUE(m.retain);
    TEST_ASSERT_FALSE(m.to_state);
    TEST_ASSERT_TRUE(route_of(LINE(BEGIN)).to_state);
    TEST_ASSERT_TRUE(route_of(LINE(CHANGE)).to_state);
    TEST_ASSERT_TRUE(route_of(LINE(STATUS)).to_state);
    TEST_ASSERT_TRUE(route_of(sample(1).c_str(), sample(1).size()).to_state);
    TEST_ASSERT_FALSE(route_of(LINE(NODE)).to_state);
    TEST_ASSERT_FALSE(route_of(LINE(RESTART)).to_state);
    TEST_ASSERT_FALSE(route_of(LINE(RESTART)).retain);
}

void test_topic_unusable_lines_go_to_other() {
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("R,1,2,3").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"x\":1}").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"type\":\"\"}").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"type\":\"a/b\"}").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"type\":\"a+b\"}").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"type\":\"#\"}").c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic("{\"type\":\"unterminated").c_str());
    std::string longt = "{\"type\":\"" + std::string(TYPE_MAX + 1, 'a') + "\"}";
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/other", topic(longt.c_str()).c_str());
    std::string okt = "{\"type\":\"" + std::string(TYPE_MAX, 'a') + "\"}";
    TEST_ASSERT_EQUAL_STRING(("resonance/rsn-0123abcd/" + std::string(TYPE_MAX, 'a')).c_str(), topic(okt.c_str()).c_str());
    char out[8];
    TEST_ASSERT_FALSE(json_type(nullptr, 5, out, sizeof out));
    TEST_ASSERT_FALSE(json_type("", 0, out, sizeof out));
}

void test_site_validation_and_topic_bounds() {
    TEST_ASSERT_TRUE(site_ok("rsn-0123abcd"));
    TEST_ASSERT_TRUE(site_ok("a"));
    TEST_ASSERT_FALSE(site_ok(""));
    TEST_ASSERT_FALSE(site_ok(nullptr));
    TEST_ASSERT_FALSE(site_ok("a/b"));
    TEST_ASSERT_FALSE(site_ok("a+"));
    TEST_ASSERT_FALSE(site_ok("A"));
    TEST_ASSERT_FALSE(site_ok("a b"));
    TEST_ASSERT_TRUE(site_ok(std::string(SITE_MAX, 'a').c_str()));
    TEST_ASSERT_FALSE(site_ok(std::string(SITE_MAX + 1, 'a').c_str()));
    char t[TOPIC_MAX];
    TEST_ASSERT_EQUAL(0, make_topic(t, sizeof t, "bad/site", "x"));
    TEST_ASSERT_EQUAL(0, make_topic(t, 10, "site", "x"));
    TEST_ASSERT_EQUAL(0, make_topic(t, sizeof t, "site", ""));
    // exact fit: "resonance/s/x" = 13 chars + NUL
    TEST_ASSERT_EQUAL(13, make_topic(t, 14, "s", "x"));
    TEST_ASSERT_EQUAL(0, make_topic(t, 13, "s", "x"));
    // longest site and longest type fit TOPIC_MAX
    TEST_ASSERT_TRUE(make_topic(t, sizeof t, std::string(SITE_MAX, 'a').c_str(), std::string(TYPE_MAX + 7, 'b').c_str()) > 0);
}

// ---------------------------------------------------------------- queue
static std::vector<std::string> drain(LineQueue<4> &q) {
    std::vector<std::string> v;
    const char *l;
    size_t n;
    Kind k;
    while (q.peek(&l, &n, &k)) { v.push_back(std::string(l, n)); q.pop(); }
    return v;
}

void test_queue_fifo_and_counters() {
    LineQueue<4> q;
    TEST_ASSERT_TRUE(q.empty());
    TEST_ASSERT_TRUE(q.push(LINE(META), K_META));
    TEST_ASSERT_TRUE(q.push(LINE(BEGIN), K_EVENT));
    std::string s = sample(1);
    TEST_ASSERT_TRUE(q.push(s.c_str(), s.size(), K_SAMPLE));
    TEST_ASSERT_EQUAL(3, q.size());
    std::vector<std::string> v = drain(q);
    TEST_ASSERT_EQUAL(3, v.size());
    TEST_ASSERT_EQUAL_STRING(META, v[0].c_str());
    TEST_ASSERT_EQUAL_STRING(BEGIN, v[1].c_str());
    TEST_ASSERT_EQUAL_STRING(s.c_str(), v[2].c_str());
    TEST_ASSERT_EQUAL(3, q.stats().pushed);
    TEST_ASSERT_EQUAL(3, q.stats().popped);
    q.pop();  // empty pop is harmless
    TEST_ASSERT_EQUAL(3, q.stats().popped);
}

void test_queue_drops_oldest_sample_first_keeps_events() {
    LineQueue<4> q;
    q.push(LINE(BEGIN), K_EVENT);
    std::string s1 = sample(1), s2 = sample(2), s3 = sample(3), s4 = sample(4);
    q.push(s1.c_str(), s1.size(), K_SAMPLE);
    q.push(LINE(NODE), K_NODE);
    q.push(s2.c_str(), s2.size(), K_SAMPLE);
    TEST_ASSERT_EQUAL(4, q.size());
    // full: new sample evicts the OLDEST sample (s1), order of the rest is kept
    TEST_ASSERT_TRUE(q.push(s3.c_str(), s3.size(), K_SAMPLE));
    TEST_ASSERT_EQUAL(1, q.stats().dropped_samples);
    // full: a new event evicts the oldest remaining sample (s2), not the begin / node
    TEST_ASSERT_TRUE(q.push(LINE(CHANGE), K_EVENT));
    TEST_ASSERT_EQUAL(2, q.stats().dropped_samples);
    TEST_ASSERT_EQUAL(0, q.stats().dropped_other);
    std::vector<std::string> v = drain(q);
    TEST_ASSERT_EQUAL(4, v.size());
    TEST_ASSERT_EQUAL_STRING(BEGIN, v[0].c_str());
    TEST_ASSERT_EQUAL_STRING(NODE, v[1].c_str());
    TEST_ASSERT_EQUAL_STRING(s3.c_str(), v[2].c_str());
    TEST_ASSERT_EQUAL_STRING(CHANGE, v[3].c_str());
    (void)s4;
}

void test_queue_all_non_samples_full() {
    LineQueue<4> q;
    q.push(LINE(META), K_META);
    q.push(LINE(RESTART), K_RESTART);
    q.push(LINE(BEGIN), K_EVENT);
    q.push(LINE(NODE), K_NODE);
    std::string s = sample(7);
    // a sample is refused (nothing to sacrifice), counted
    TEST_ASSERT_FALSE(q.push(s.c_str(), s.size(), K_SAMPLE));
    TEST_ASSERT_EQUAL(1, q.stats().dropped_samples);
    TEST_ASSERT_EQUAL(4, q.size());
    // a new non-sample evicts the OLDEST line (meta), counted as dropped_other
    TEST_ASSERT_TRUE(q.push(LINE(DAMAGE), K_DAMAGE));
    TEST_ASSERT_EQUAL(1, q.stats().dropped_other);
    std::vector<std::string> v = drain(q);
    TEST_ASSERT_EQUAL_STRING(RESTART, v[0].c_str());
    TEST_ASSERT_EQUAL_STRING(DAMAGE, v[3].c_str());
}

void test_queue_sample_flood_never_evicts_events() {
    LineQueue<8> q;
    q.push(LINE(BEGIN), K_EVENT);
    q.push(LINE(NODE), K_NODE);
    for (int i = 0; i < 1000; i++) {
        std::string s = sample(i);
        q.push(s.c_str(), s.size(), K_SAMPLE);
    }
    TEST_ASSERT_EQUAL(8, q.size());
    TEST_ASSERT_EQUAL(0, q.stats().dropped_other);
    TEST_ASSERT_EQUAL(1000 - 6, q.stats().dropped_samples);
    const char *l;
    size_t n;
    Kind k;
    TEST_ASSERT_TRUE(q.peek(&l, &n, &k));
    TEST_ASSERT_EQUAL_STRING(BEGIN, std::string(l, n).c_str());
    q.pop();
    q.peek(&l, &n, &k);
    TEST_ASSERT_EQUAL_STRING(NODE, std::string(l, n).c_str());
    q.pop();
    // the 6 newest samples remain, in order
    for (int i = 994; i < 1000; i++) {
        TEST_ASSERT_TRUE(q.peek(&l, &n, &k));
        TEST_ASSERT_EQUAL_STRING(sample(i).c_str(), std::string(l, n).c_str());
        q.pop();
    }
    TEST_ASSERT_TRUE(q.empty());
}

void test_queue_wraparound_and_limits() {
    LineQueue<4> q;
    for (int round = 0; round < 50; round++) {
        std::string a = sample(round * 2), b = sample(round * 2 + 1);
        q.push(a.c_str(), a.size(), K_SAMPLE);
        q.push(b.c_str(), b.size(), K_SAMPLE);
        const char *l;
        size_t n;
        Kind k;
        q.peek(&l, &n, &k);
        TEST_ASSERT_EQUAL_STRING(a.c_str(), std::string(l, n).c_str());
        q.pop();
        q.peek(&l, &n, &k);
        TEST_ASSERT_EQUAL_STRING(b.c_str(), std::string(l, n).c_str());
        q.pop();
    }
    TEST_ASSERT_TRUE(q.empty());
    // exactly LINE_CAP is accepted, one more and empty are refused and counted
    std::string big(LINE_CAP, 'x');
    TEST_ASSERT_TRUE(q.push(big.c_str(), big.size(), K_EVENT));
    std::string bigger(LINE_CAP + 1, 'x');
    TEST_ASSERT_FALSE(q.push(bigger.c_str(), bigger.size(), K_EVENT));
    TEST_ASSERT_FALSE(q.push("", 0, K_EVENT));
    TEST_ASSERT_FALSE(q.push(nullptr, 5, K_EVENT));
    TEST_ASSERT_EQUAL(3, q.stats().too_long);
    TEST_ASSERT_EQUAL(1, q.size());
    const char *l;
    size_t n;
    Kind k;
    q.peek(&l, &n, &k);
    TEST_ASSERT_EQUAL(LINE_CAP, n);
    TEST_ASSERT_EQUAL(0, memcmp(l, big.c_str(), n));
}

// ---------------------------------------------------------------- backoff / link
void test_backoff_exponential_with_cap() {
    Backoff b;  // 1 s, cap 60 s
    uint32_t want[] = {1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000};
    uint32_t now = 100;
    TEST_ASSERT_TRUE(b.due(now));
    for (size_t i = 0; i < sizeof want / sizeof want[0]; i++) {
        TEST_ASSERT_EQUAL_UINT32(want[i], b.failure(now));
        TEST_ASSERT_FALSE(b.due(now + want[i] - 1));
        TEST_ASSERT_TRUE(b.due(now + want[i]));
        now += want[i];
    }
    b.success();
    TEST_ASSERT_EQUAL(0, b.fails());
    TEST_ASSERT_TRUE(b.due(now));
    TEST_ASSERT_EQUAL_UINT32(1000, b.failure(now));  // restarts at the base
}

void test_backoff_never_overflows_and_jitter_bounded() {
    Backoff b(1000, 60000);
    for (int i = 0; i < 5000; i++) {
        uint32_t d = b.failure(0, 0xFFFF);
        TEST_ASSERT_TRUE(d <= 60000);
    }
    Backoff c(1000, 60000);
    uint32_t d0 = c.failure(0, 0);
    Backoff e(1000, 60000);
    uint32_t d1 = e.failure(0, 0xFFFF);
    TEST_ASSERT_EQUAL_UINT32(1000, d0);
    TEST_ASSERT_EQUAL_UINT32(1250, d1);  // +25% at most
    Backoff odd(5000, 100);                // cap below base is raised to base
    TEST_ASSERT_EQUAL_UINT32(5000, odd.failure(0));
}

void test_backoff_clock_wrap() {
    Backoff b;
    uint32_t now = 0xFFFFFF00u;
    uint32_t d = b.failure(now);  // next = now + 1000 wraps past 0
    TEST_ASSERT_EQUAL_UINT32(1000, d);
    TEST_ASSERT_FALSE(b.due(now + 999));
    TEST_ASSERT_TRUE(b.due(now + 1000));
}

void test_link_happy_path_and_wifi_retry() {
    Link l;
    uint32_t t = 0;
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(t, false, false));
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 5000, false, false));    // still joining
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 19999, false, false));
    // join timeout at 20 s: failure, 1 s backoff, so no immediate retry
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 20000, false, false));
    TEST_ASSERT_EQUAL(1, l.wifi_fails());
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(t + 21000, false, false));
    // Wi-Fi comes up, MQTT must be connected
    TEST_ASSERT_EQUAL(ACT_MQTT_CONNECT, l.poll(t + 23000, true, false));
    l.mqtt_result(t + 23000, true);
    TEST_ASSERT_EQUAL(1, l.connects());
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 24000, true, true));
    TEST_ASSERT_EQUAL(0, l.wifi_fails());
}

void test_link_mqtt_failures_back_off_then_recover() {
    Link l;
    uint32_t t = 1000;
    TEST_ASSERT_EQUAL(ACT_MQTT_CONNECT, l.poll(t, true, false));
    l.mqtt_result(t, false);            // 1 s
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 999, true, false));
    TEST_ASSERT_EQUAL(ACT_MQTT_CONNECT, l.poll(t + 1000, true, false));
    l.mqtt_result(t + 1000, false);     // 2 s
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 2999, true, false));
    TEST_ASSERT_EQUAL(ACT_MQTT_CONNECT, l.poll(t + 3000, true, false));
    l.mqtt_result(t + 3000, true);
    TEST_ASSERT_EQUAL(0, l.mqtt_fails());
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 3100, true, true));
}

void test_link_drop_after_up_waits_one_base_delay() {
    Link l;
    uint32_t t = 0;
    l.poll(t, true, false);
    l.mqtt_result(t, true);
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 10, true, true));
    // broker connection lost, Wi-Fi still up
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 5000, true, false));
    TEST_ASSERT_EQUAL(1, l.drops());
    TEST_ASSERT_EQUAL(ACT_MQTT_CONNECT, l.poll(t + 6000, true, false));
    l.mqtt_result(t + 6000, true);
    TEST_ASSERT_EQUAL(2, l.connects());
    // Wi-Fi lost too: a new drop, a Wi-Fi rejoin is offered at once (first failure of the Wi-Fi counter), then we wait
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(t + 9000, false, false));
    TEST_ASSERT_EQUAL(2, l.drops());
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(t + 9001, false, false));
}

void test_link_flapping_does_not_reset_backoff_early() {
    Link l;
    uint32_t t = 0;
    uint32_t delays[4];
    for (int i = 0; i < 4; i++) {
        // find the first poll time at which a connect is offered
        uint32_t s = t;
        while (l.poll(s, true, false) != ACT_MQTT_CONNECT) s++;
        delays[i] = s - t;
        l.mqtt_result(s, false);
        t = s;
    }
    TEST_ASSERT_EQUAL_UINT32(0, delays[0]);
    TEST_ASSERT_EQUAL_UINT32(1000, delays[1]);
    TEST_ASSERT_EQUAL_UINT32(2000, delays[2]);
    TEST_ASSERT_EQUAL_UINT32(4000, delays[3]);
}

// ---------------------------------------------------------------- bridge
struct Pub {
    std::string topic, payload;
    bool retain;
};
struct FakeIo : Io {
    std::vector<Pub> pubs;
    int fail_after = -1;  // fail every publish once this many have succeeded
    bool publish(const char *t, const uint8_t *p, size_t n, bool retain) override {
        if (fail_after >= 0 && (int)pubs.size() >= fail_after) return false;
        Pub x;
        x.topic = t;
        x.payload = std::string((const char *)p, n);
        x.retain = retain;
        pubs.push_back(x);
        return true;
    }
};

static std::vector<Pub> only(const std::vector<Pub> &v, const std::string &leaf) {
    std::vector<Pub> r;
    for (size_t i = 0; i < v.size(); i++)
        if (v[i].topic == "resonance/rsn-0123abcd/" + leaf) r.push_back(v[i]);
    return r;
}

static Bridge<8> *make_bridge() {
    static Bridge<8> b;
    b = Bridge<8>();
    b.set_site("rsn-0123abcd");
    return &b;
}

void test_bridge_publishes_in_order_with_state_and_online() {
    Bridge<8> &b = *make_bridge();
    std::string s1 = sample(1), s2 = sample(2);
    b.push(LINE(META));
    b.push(LINE(RESTART));
    b.push(s1.c_str(), s1.size());
    b.push(LINE(BEGIN));
    b.push(s2.c_str(), s2.size());
    b.push(LINE(NODE));
    b.push(LINE(DAMAGE));
    FakeIo io;
    TEST_ASSERT_TRUE(b.on_connected(io));
    TEST_ASSERT_EQUAL(1, io.pubs.size());   // online only: meta is queued, not duplicated
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/status", io.pubs[0].topic.c_str());
    TEST_ASSERT_EQUAL_STRING("{\"state\":\"online\",\"epoch\":null}", io.pubs[0].payload.c_str());   // no epoch set
    TEST_ASSERT_TRUE(io.pubs[0].retain);
    TEST_ASSERT_TRUE(b.pump(io, 100));
    TEST_ASSERT_TRUE(b.queue().empty());
    // main stream byte-identical and in order (skipping the retained state copies and status)
    std::vector<std::string> main_payloads, main_topics;
    for (size_t i = 1; i < io.pubs.size(); i++) {
        if (io.pubs[i].topic == "resonance/rsn-0123abcd/state" || io.pubs[i].topic == "resonance/rsn-0123abcd/damage_state") continue;
        main_payloads.push_back(io.pubs[i].payload);
        main_topics.push_back(io.pubs[i].topic);
    }
    const char *want[] = {META, RESTART, s1.c_str(), BEGIN, s2.c_str(), NODE, DAMAGE};
    const char *leaf[] = {"meta", "restart", "sample", "begin", "sample", "node", "damage"};
    TEST_ASSERT_EQUAL(7, main_payloads.size());
    for (int i = 0; i < 7; i++) {
        TEST_ASSERT_EQUAL_STRING(want[i], main_payloads[i].c_str());
        TEST_ASSERT_EQUAL_STRING((std::string("resonance/rsn-0123abcd/") + leaf[i]).c_str(), main_topics[i].c_str());
    }
    // retained state = the decision lines, the LAST one is the latest (s2 came after begin)
    std::vector<Pub> st = only(io.pubs, "state");
    TEST_ASSERT_EQUAL(3, st.size());   // s1, begin, s2
    TEST_ASSERT_TRUE(st[0].retain && st[1].retain && st[2].retain);
    TEST_ASSERT_EQUAL_STRING(s2.c_str(), st[2].payload.c_str());
    // meta is retained, the stream lines are not
    TEST_ASSERT_TRUE(only(io.pubs, "meta")[0].retain);
    TEST_ASSERT_FALSE(only(io.pubs, "sample")[0].retain);
    TEST_ASSERT_EQUAL(7, b.stats().published);
}

void test_bridge_status_carries_boot_epoch() {
    Bridge<8> &b = *make_bridge();
    b.set_epoch(2866824256u);
    char pl[64];
    TEST_ASSERT_TRUE(b.status_payload(pl, sizeof pl, true) > 0);
    TEST_ASSERT_EQUAL_STRING("{\"state\":\"online\",\"epoch\":2866824256}", pl);
    TEST_ASSERT_TRUE(b.status_payload(pl, sizeof pl, false) > 0);
    TEST_ASSERT_EQUAL_STRING("{\"state\":\"offline\",\"epoch\":2866824256}", pl);   // the last will text the host registers
    TEST_ASSERT_EQUAL(0, b.status_payload(pl, 10, true));                                  // too small: refused
    FakeIo io;
    TEST_ASSERT_TRUE(b.on_connected(io));
    TEST_ASSERT_EQUAL_STRING("{\"state\":\"online\",\"epoch\":2866824256}", io.pubs[0].payload.c_str());
    TEST_ASSERT_TRUE(b.publish_status(io));   // periodic re-publish
    TEST_ASSERT_EQUAL(2, io.pubs.size());
    TEST_ASSERT_TRUE(io.pubs[1].retain);
    TEST_ASSERT_EQUAL(2, b.stats().status_published);
}

// The REAL lines of the firmware formatter (fusion_json.h, schema 3, trailing newline and all), not hand-written look-alikes.
void test_real_schema3_lines_route_and_damage_state_holds_the_real_line() {
    using namespace fusion;
    DamageEvent d;
    d.t_us = 131529754ull;
    d.sf = 93;
    d.begin = true;
    d.drop = 0.0205f;
    d.f1[0] = 5.25f;
    d.f1[1] = NAN;
    d.f1_0[0] = 5.4f;
    d.f1_0[1] = 6.0f;
    char b[json::JSON_LINE_MAX];
    size_t n = json::damage_line(b, sizeof b, d);
    TEST_ASSERT_TRUE(n > 0 && b[n - 1] == '\n');
    Route r = route_of(b, n - 1);
    TEST_ASSERT_EQUAL(K_DAMAGE, r.kind);
    TEST_ASSERT_TRUE(r.to_damage_state);
    TEST_ASSERT_FALSE(r.to_state);
    TEST_ASSERT_FALSE(r.retain);
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage", topic(std::string(b, n - 1).c_str()).c_str());
    Bridge<8> &br = *make_bridge();
    br.push(b, n);                       // with its newline, as SerialJson::write_line hands it over
    FakeIo io;
    TEST_ASSERT_TRUE(br.pump(io));
    TEST_ASSERT_EQUAL(2, io.pubs.size());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage", io.pubs[0].topic.c_str());
    TEST_ASSERT_FALSE(io.pubs[0].retain);
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage_state", io.pubs[1].topic.c_str());
    TEST_ASSERT_TRUE(io.pubs[1].retain);
    TEST_ASSERT_EQUAL_STRING(std::string(b, n - 1).c_str(), io.pubs[1].payload.c_str());
    TEST_ASSERT_NOT_NULL(strstr(io.pubs[1].payload.c_str(), "\"state\":\"BEGIN\""));
    // the END line replaces the retained BEGIN (same topic, retained)
    d.begin = false;
    d.drop = 0.004f;
    n = json::damage_line(b, sizeof b, d);
    br.push(b, n);
    FakeIo io2;
    TEST_ASSERT_TRUE(br.pump(io2));
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage_state", io2.pubs[1].topic.c_str());
    TEST_ASSERT_NOT_NULL(strstr(io2.pubs[1].payload.c_str(), "\"state\":\"END\""));
    // the other schema-3 lines at their widest all fit the queue's line cap and route as before
    Sample s;
    memset(&s, 0, sizeof s);
    s.t_us = 18446744073709551615ull; s.sf = s.closed = 4294967295u; s.cls = COMMISSIONING; s.slave = 2; s.degraded = 2;
    s.r[0] = s.r[1] = 3.9e18f; s.age_sf[0] = s.age_sf[1] = 2147483647; s.commission_n[0] = s.commission_n[1] = 255;
    s.ae[0] = s.ae[1] = 3.9e18f; s.f1[0] = s.f1[1] = 99.99999f; s.drop[0] = s.drop[1] = -0.9999999f; s.dmg = 1;
    n = json::sample_line(b, sizeof b, s);
    TEST_ASSERT_TRUE(n > 0 && n - 1 <= LINE_CAP);
    TEST_ASSERT_EQUAL(K_SAMPLE, route_of(b, n - 1).kind);
    Event e;
    memset(&e, 0, sizeof e);
    e.kind = EV_BEGIN; e.cls = LOCAL; e.slave = 1; e.ae[0] = e.ae[1] = 3.9e18f; e.f1[0] = e.f1[1] = 99.99999f; e.drop[0] = e.drop[1] = -0.9999999f;
    n = json::event_line(b, sizeof b, e);
    TEST_ASSERT_TRUE(n > 0 && n - 1 <= LINE_CAP);
    TEST_ASSERT_TRUE(route_of(b, n - 1).to_state);   // begin / change are the retained decision
    json::Meta m;
    m.source = "master"; m.cfg = chosen_config(); m.floor1 = 5; m.floor2 = 10; m.epoch = 1; m.samples = true;
    n = json::meta_line(b, sizeof b, m);
    TEST_ASSERT_TRUE(n > 0 && n - 1 <= LINE_CAP);
    TEST_ASSERT_EQUAL(K_META, route_of(b, n - 1).kind);
    static_assert(LINE_CAP == json::JSON_LINE_MAX, "LINE_CAP must equal JSON_LINE_MAX");
}

void test_bridge_damage_state_and_no_duplicate_after_partial_failure() {
    Bridge<8> &b = *make_bridge();
    b.push(LINE(DAMAGE));
    FakeIo io;
    io.fail_after = 1;   // the line itself goes out, its retained damage_state copy fails
    TEST_ASSERT_FALSE(b.pump(io));
    TEST_ASSERT_EQUAL(1, io.pubs.size());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage", io.pubs[0].topic.c_str());
    TEST_ASSERT_EQUAL(1, b.queue().size());
    FakeIo io2;   // reconnected: only the missing copy is sent, the line is NOT published twice
    TEST_ASSERT_TRUE(b.pump(io2));
    TEST_ASSERT_EQUAL(1, io2.pubs.size());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage_state", io2.pubs[0].topic.c_str());
    TEST_ASSERT_TRUE(io2.pubs[0].retain);
    TEST_ASSERT_EQUAL_STRING(DAMAGE, io2.pubs[0].payload.c_str());
    TEST_ASSERT_TRUE(b.queue().empty());
    // same for a decision line whose state copy fails
    std::string s1 = sample(1);
    b.push(s1.c_str(), s1.size());
    FakeIo io3;
    io3.fail_after = 1;
    TEST_ASSERT_FALSE(b.pump(io3));
    FakeIo io4;
    TEST_ASSERT_TRUE(b.pump(io4));
    TEST_ASSERT_EQUAL(1, io4.pubs.size());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/state", io4.pubs[0].topic.c_str());
}

void test_damage_is_not_evicted_before_samples_and_reserved_leaf() {
    LineQueue<4> q;
    q.push(LINE(DAMAGE), K_DAMAGE);
    std::string s1 = sample(1), s2 = sample(2), s3 = sample(3), s4 = sample(4);
    q.push(s1.c_str(), s1.size(), K_SAMPLE);
    q.push(s2.c_str(), s2.size(), K_SAMPLE);
    q.push(s3.c_str(), s3.size(), K_SAMPLE);
    q.push(s4.c_str(), s4.size(), K_SAMPLE);   // evicts the oldest SAMPLE, never the damage line
    std::vector<std::string> v = drain(q);
    TEST_ASSERT_EQUAL_STRING(DAMAGE, v[0].c_str());
    TEST_ASSERT_EQUAL(4, v.size());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/damage_state_line", topic("{\"type\":\"damage_state\"}").c_str());
}

void test_handoff_policy_reserves_slots_for_non_samples() {
    // 12-slot hand-off queue, 4 reserved: samples are refused at 4 or fewer free slots, everything else until 0
    TEST_ASSERT_TRUE(handoff_accepts(K_SAMPLE, 12, 4));
    TEST_ASSERT_TRUE(handoff_accepts(K_SAMPLE, 5, 4));
    TEST_ASSERT_FALSE(handoff_accepts(K_SAMPLE, 4, 4));
    TEST_ASSERT_FALSE(handoff_accepts(K_SAMPLE, 1, 4));
    TEST_ASSERT_TRUE(handoff_accepts(K_EVENT, 4, 4));
    TEST_ASSERT_TRUE(handoff_accepts(K_NODE, 1, 4));
    TEST_ASSERT_TRUE(handoff_accepts(K_META, 1, 4));
    TEST_ASSERT_TRUE(handoff_accepts(K_DAMAGE, 1, 4));
    TEST_ASSERT_FALSE(handoff_accepts(K_EVENT, 0, 4));
    TEST_ASSERT_FALSE(handoff_accepts(K_SAMPLE, 0, 0));
}

void test_head_progress_survives_eviction_of_a_younger_sample() {
    // verifier probe: head = event whose state copy failed; offline the queue fills with samples; further pushes evict the oldest
    // SAMPLE (not the head): after reconnect the event must not be published a second time
    Bridge<4> b;
    b.set_site("rsn-0123abcd");
    b.push(LINE(BEGIN));
    FakeIo io;
    io.fail_after = 1;   // the line goes out, its retained state copy fails
    TEST_ASSERT_FALSE(b.pump(io));
    for (int i = 0; i < 6; i++) { std::string s = sample(10 + i); b.push(s.c_str(), s.size()); }   // fills, then evicts samples only
    FakeIo io2;
    TEST_ASSERT_TRUE(b.on_connected(io2));
    TEST_ASSERT_TRUE(b.pump(io2, 20));
    for (size_t i = 0; i < io2.pubs.size(); i++)
        TEST_ASSERT_FALSE_MESSAGE(io2.pubs[i].topic == "resonance/rsn-0123abcd/begin", "the event was published twice");
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/state", io2.pubs[1].topic.c_str());   // only its missing state copy went out
    // but when the head itself IS evicted (queue of non-samples only), the new head starts from scratch
    Bridge<2> c;
    c.set_site("rsn-0123abcd");
    c.push(LINE(BEGIN));
    c.push(LINE(CHANGE));
    FakeIo io3;
    io3.fail_after = 1;
    TEST_ASSERT_FALSE(c.pump(io3));            // BEGIN's main publish done, state copy failed
    c.push(LINE(NODE));                        // full, no sample: the oldest (BEGIN, the head) is evicted
    FakeIo io4;
    TEST_ASSERT_TRUE(c.pump(io4, 10));
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/change", io4.pubs[0].topic.c_str());   // CHANGE published in full
    TEST_ASSERT_EQUAL_STRING(CHANGE, io4.pubs[0].payload.c_str());
}

void test_link_wifi_backoff_is_configurable_slow() {
    Link l(30000, 300000);
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(0, false, false));
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(20000, false, false));    // join timeout -> failure
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(49999, false, false));    // 30 s back-off, no scan storm
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(50000, false, false));
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(70000, false, false));    // fails again: 60 s
    TEST_ASSERT_EQUAL(ACT_NONE, l.poll(129999, false, false));
    TEST_ASSERT_EQUAL(ACT_WIFI_BEGIN, l.poll(130000, false, false));
}

void test_bridge_meta_republished_after_reconnect() {
    Bridge<8> &b = *make_bridge();
    FakeIo io;
    b.push(LINE(META));
    TEST_ASSERT_TRUE(b.on_connected(io));
    TEST_ASSERT_TRUE(b.pump(io));
    TEST_ASSERT_EQUAL(1, only(io.pubs, "meta").size());
    // connection lost and re-established later: a late subscriber must learn the meta again
    FakeIo io2;
    std::string s = sample(5);
    b.push(s.c_str(), s.size());
    TEST_ASSERT_TRUE(b.on_connected(io2));
    TEST_ASSERT_EQUAL(2, io2.pubs.size());                  // online, meta
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/status", io2.pubs[0].topic.c_str());
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/meta", io2.pubs[1].topic.c_str());
    TEST_ASSERT_EQUAL_STRING(META, io2.pubs[1].payload.c_str());
    TEST_ASSERT_EQUAL(1, b.stats().meta_republished);
    TEST_ASSERT_TRUE(b.pump(io2));
    TEST_ASSERT_EQUAL_STRING(s.c_str(), only(io2.pubs, "sample")[0].payload.c_str());
    // a newer meta (master rebooted) replaces the remembered one
    const char *meta2 = "{\"type\":\"meta\",\"schema\":2,\"source\":\"master\",\"epoch\":7}";
    b.push(LINE(meta2));
    TEST_ASSERT_TRUE(b.pump(io2));
    FakeIo io3;
    TEST_ASSERT_TRUE(b.on_connected(io3));
    TEST_ASSERT_EQUAL_STRING(meta2, only(io3.pubs, "meta")[0].payload.c_str());
}

void test_bridge_strips_line_terminator() {
    Bridge<8> &b = *make_bridge();
    std::string withnl = std::string(NODE) + (char)10, withcrlf = std::string(BEGIN) + (char)13 + (char)10;
    b.push(withnl.c_str(), withnl.size());
    b.push(withcrlf.c_str(), withcrlf.size());
    b.push("\x0a", 1);  // only a terminator: nothing left, refused
    TEST_ASSERT_EQUAL(2, b.queue().size());
    TEST_ASSERT_EQUAL(1, b.queue().stats().too_long);
    FakeIo io;
    TEST_ASSERT_TRUE(b.pump(io));
    TEST_ASSERT_EQUAL_STRING(NODE, io.pubs[0].payload.c_str());
    TEST_ASSERT_EQUAL_STRING(BEGIN, io.pubs[1].payload.c_str());
}

void test_bridge_no_meta_yet_no_republish() {
    Bridge<8> &b = *make_bridge();
    FakeIo io;
    TEST_ASSERT_TRUE(b.on_connected(io));
    TEST_ASSERT_EQUAL(1, io.pubs.size());
    TEST_ASSERT_EQUAL(0, b.stats().meta_republished);
}

void test_bridge_publish_failure_keeps_line_for_retry() {
    Bridge<8> &b = *make_bridge();
    std::string s1 = sample(1), s2 = sample(2);
    b.push(s1.c_str(), s1.size());
    b.push(LINE(NODE));
    b.push(s2.c_str(), s2.size());
    FakeIo io;
    io.fail_after = 2;  // sample s1 + its state copy succeed, then the link breaks
    TEST_ASSERT_FALSE(b.pump(io));
    TEST_ASSERT_EQUAL(2, io.pubs.size());
    TEST_ASSERT_EQUAL(2, b.queue().size());  // s1 left the queue, node and s2 remain, in order
    TEST_ASSERT_EQUAL(1, b.stats().publish_fail);
    FakeIo io2;
    TEST_ASSERT_TRUE(b.pump(io2));
    TEST_ASSERT_EQUAL_STRING(NODE, io2.pubs[0].payload.c_str());
    TEST_ASSERT_EQUAL_STRING(s2.c_str(), io2.pubs[1].payload.c_str());
}

void test_bridge_pump_respects_max_lines() {
    Bridge<8> &b = *make_bridge();
    for (int i = 0; i < 5; i++) b.push(LINE(NODE));
    FakeIo io;
    TEST_ASSERT_TRUE(b.pump(io, 2));
    TEST_ASSERT_EQUAL(2, io.pubs.size());
    TEST_ASSERT_EQUAL(3, b.queue().size());
}

void test_bridge_bad_site_publishes_nothing() {
    Bridge<8> b;  // no site set
    b.push(LINE(NODE));
    FakeIo io;
    TEST_ASSERT_FALSE(b.pump(io));
    TEST_ASSERT_EQUAL(0, io.pubs.size());
    TEST_ASSERT_FALSE(b.set_site("bad/site"));
    TEST_ASSERT_FALSE(b.on_connected(io));
    TEST_ASSERT_TRUE(b.stats().bad_site >= 2);
    TEST_ASSERT_EQUAL(1, b.queue().size());
}

void test_bridge_status_topic_for_lwt() {
    Bridge<8> &b = *make_bridge();
    char t[TOPIC_MAX];
    TEST_ASSERT_TRUE(b.status_topic(t, sizeof t));
    TEST_ASSERT_EQUAL_STRING("resonance/rsn-0123abcd/status", t);
}

void test_bridge_drops_under_backlog_keep_events() {
    // offline for a long time: 200 samples + 3 events + node, depth 8
    Bridge<8> &b = *make_bridge();
    b.push(LINE(META));
    for (int i = 0; i < 100; i++) { std::string s = sample(i); b.push(s.c_str(), s.size()); }
    b.push(LINE(BEGIN));
    for (int i = 100; i < 200; i++) { std::string s = sample(i); b.push(s.c_str(), s.size()); }
    b.push(LINE(NODE));
    b.push(LINE(CHANGE));
    FakeIo io;
    b.on_connected(io);
    b.pump(io, 100);
    TEST_ASSERT_EQUAL(1, only(io.pubs, "begin").size());
    TEST_ASSERT_EQUAL(1, only(io.pubs, "node").size());
    TEST_ASSERT_EQUAL(1, only(io.pubs, "change").size());
    TEST_ASSERT_EQUAL(1, only(io.pubs, "meta").size());
    TEST_ASSERT_EQUAL(200 - 4, b.queue().stats().dropped_samples);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_topic_mapping_known_types);
    RUN_TEST(test_topic_unknown_future_type_routes_by_name);
    RUN_TEST(test_topic_reserved_leaf_names_do_not_collide);
    RUN_TEST(test_topic_flags);
    RUN_TEST(test_topic_unusable_lines_go_to_other);
    RUN_TEST(test_site_validation_and_topic_bounds);
    RUN_TEST(test_queue_fifo_and_counters);
    RUN_TEST(test_queue_drops_oldest_sample_first_keeps_events);
    RUN_TEST(test_queue_all_non_samples_full);
    RUN_TEST(test_queue_sample_flood_never_evicts_events);
    RUN_TEST(test_queue_wraparound_and_limits);
    RUN_TEST(test_backoff_exponential_with_cap);
    RUN_TEST(test_backoff_never_overflows_and_jitter_bounded);
    RUN_TEST(test_backoff_clock_wrap);
    RUN_TEST(test_link_happy_path_and_wifi_retry);
    RUN_TEST(test_link_mqtt_failures_back_off_then_recover);
    RUN_TEST(test_link_drop_after_up_waits_one_base_delay);
    RUN_TEST(test_link_flapping_does_not_reset_backoff_early);
    RUN_TEST(test_bridge_publishes_in_order_with_state_and_online);
    RUN_TEST(test_bridge_meta_republished_after_reconnect);
    RUN_TEST(test_bridge_strips_line_terminator);
    RUN_TEST(test_bridge_no_meta_yet_no_republish);
    RUN_TEST(test_bridge_publish_failure_keeps_line_for_retry);
    RUN_TEST(test_bridge_status_carries_boot_epoch);
    RUN_TEST(test_real_schema3_lines_route_and_damage_state_holds_the_real_line);
    RUN_TEST(test_bridge_damage_state_and_no_duplicate_after_partial_failure);
    RUN_TEST(test_damage_is_not_evicted_before_samples_and_reserved_leaf);
    RUN_TEST(test_handoff_policy_reserves_slots_for_non_samples);
    RUN_TEST(test_link_wifi_backoff_is_configurable_slow);
    RUN_TEST(test_head_progress_survives_eviction_of_a_younger_sample);
    RUN_TEST(test_bridge_pump_respects_max_lines);
    RUN_TEST(test_bridge_bad_site_publishes_nothing);
    RUN_TEST(test_bridge_status_topic_for_lwt);
    RUN_TEST(test_bridge_drops_under_backlog_keep_events);
    return UNITY_END();
}
