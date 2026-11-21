// Host tests of the Wokwi cloud demo's in-process network (include/virtual_net.h): two virtual tiers.h slaves behind a
// loopback Transport, fed from the demo trace header (include/demo_traces.h, generated from test/data/demo_traces.bin).
// The real Master + Fusion + JSON formatter run on top, so the output must be the netsim output of the same traces.
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <string>
#include <vector>
#include <unity.h>
#include "demo_traces.h"
#include "virtual_net.h"
#include "fusion.h"
#include "fusion_json.h"
#include "netsim.h"

using namespace net;

void setUp(void) {}
void tearDown(void) {}

namespace {
class HeaderSource : public VWindowSource {
public:
    uint64_t per_get_us = 0;   // artificial host compute time of ONE window (the host is blocked that long in get(), see Rig::run_until)
    uint64_t stall_us = 0;
    uint32_t gets = 0;
    void get(uint8_t slave, uint32_t sf, int16_t *az, int16_t *gx) override {
        stall_us += per_get_us;
        gets++;
        const int16_t *p = DEMO_TRACE[slave - 1][sf % DEMO_N_WINDOWS];  // the demo loops
        memcpy(az, p, 128 * sizeof(int16_t));
        memcpy(gx, p + 128, 128 * sizeof(int16_t));
    }
};

struct Collect : fusion::json::JsonLineSink {
    std::vector<std::string> lines, samples, events, nodes;
    bool write_line(const char *l, size_t n, bool is_sample) override {
        lines.push_back(std::string(l, n));
        (is_sample ? samples : events).push_back(std::string(l, n));
        return true;
    }
};

// Same wiring as the master in src/net_main.cpp (listener -> fusion, fusion_service after every tick / frame).
struct Rig {
    HeaderSource src;
    SlaveAnalytics pipes[NUM_SLAVES];   // the AE + f1 tracker of the two virtual slaves (~25 KB each; static on the ESP32)
    VirtualNet vnet;
    Collect sink;
    fusion::Fusion fus;
    fusion::FusionListener lis;
    Master master;
    uint64_t fus_last = 0;
    uint32_t frames = 0;
    Rig() : vnet(&src, pipes), fus(&sink), lis(&fus), master(&vnet, &lis) { master.reset(1000000, 0xC0FFEEu); }
    uint64_t clock(uint64_t t) {
        if (t < fus_last) t = fus_last;
        return fus_last = t;
    }
    void service() { fus.advance(master.current_sf(), master.polling_active(), clock(fus_last)); }
    void run_until(uint64_t t_end_us, size_t stop_after_samples = 0) {
        for (uint64_t t = 0; t < t_end_us; t += 1000) {
            t += src.stall_us;   // a blocking window computation on the host lets simulated time pass
            src.stall_us = 0;
            vnet.service(t, [&](const uint8_t *b, size_t n, uint64_t ft) {
                master.on_packet(b, n, ft);
                frames++;
                fus.advance(master.current_sf(), master.polling_active(), clock(ft));
            });
            if (t >= master.next_wakeup()) {
                master.tick(t);
                fus.advance(master.current_sf(), master.polling_active(), clock(t));
            }
            if (stop_after_samples && sink.samples.size() >= stop_after_samples) break;
        }
        t_last_stall_check = src.stall_us;
    }
    uint64_t t_last_stall_check = 0;
};

// Removes "t_us":<digits>, from a JSON line (timing differs between the netsim run and this one).
std::string strip_t(std::string s) {
    while (!s.empty() && (s.back() == 10 || s.back() == 13)) s.pop_back();
    size_t a = s.find("\"t_us\":");
    if (a == std::string::npos) return s;
    size_t b = s.find(',', a);
    return s.substr(0, a) + s.substr(b + 1);
}
}  // namespace

void test_header_matches_trace_file() {
    netsim::TraceSource ts;
    std::string err;
    TEST_ASSERT_TRUE_MESSAGE(ts.load("test/data/demo_traces.bin", &err), err.c_str());
    TEST_ASSERT_EQUAL_UINT32(ts.windows(), DEMO_N_WINDOWS);  // nothing decimated or shortened
    int16_t az[128], gx[128];
    for (uint8_t s = 1; s <= 2; s++) {
        for (uint32_t w = 0; w < ts.windows(); w++) {
            ts.get(s, w, az, gx);
            TEST_ASSERT_EQUAL_INT16_ARRAY(az, DEMO_TRACE[s - 1][w], 128);
            TEST_ASSERT_EQUAL_INT16_ARRAY(gx, DEMO_TRACE[s - 1][w] + 128, 128);
        }
    }
    TEST_ASSERT_EQUAL(5, DEMO_FLOOR_1);
    TEST_ASSERT_EQUAL(10, DEMO_FLOOR_2);
    // flash size of the table
    TEST_ASSERT_EQUAL(327680, (int)sizeof(DEMO_TRACE));   // 2 x 320 x 256 x 2 B (the 116-window legacy trace was 118,784 B)
}

void test_virtual_slaves_every_report_reaches_master_in_time() {
    Rig r;
    r.run_until(1000000ull + 40ull * SUPERFRAME_US);
    const MasterStats &s = r.master.stats();
    TEST_ASSERT_TRUE(s.beacons >= 40);
    // lossless: every superframe that finished polling got both reports, no retries, no missed, nobody dead
    uint32_t done = s.beacons - 1;
    TEST_ASSERT_TRUE(s.reports[0] >= done && s.reports[1] >= done);
    TEST_ASSERT_EQUAL(0, s.missed_total[0]);
    TEST_ASSERT_EQUAL(0, s.missed_total[1]);
    TEST_ASSERT_EQUAL(0, s.retries[0] + s.retries[1]);
    TEST_ASSERT_EQUAL(0, s.rx_invalid + s.rx_stale + s.rx_epoch + s.rx_unpolled + s.rx_duplicate + s.rx_bad_field);
    TEST_ASSERT_TRUE(r.master.alive(1) && r.master.alive(2));
    TEST_ASSERT_EQUAL(0, r.vnet.dropped());
    TEST_ASSERT_EQUAL(0, r.vnet.slave(1).stats().free_runs + r.vnet.slave(2).stats().free_runs);
    TEST_ASSERT_EQUAL(0, r.vnet.slave(1).stats().windows_rejected + r.vnet.slave(2).stats().windows_rejected);
}

void test_cloud_demo_lines_equal_netsim_output_of_the_same_traces() {
    // dashboard/demo_events_sim.jsonl is REAL netsim output of demo_traces.bin (floors 5,10), schema 3: meta, 320 samples, 3 begin + 5 change + 5 status + 1 damage BEGIN (Phase C2b scenario)
    // (with the AE ratio, f1 and drop of the slaves' full pipeline in every sample; the virtual slaves run the same pipeline)
    FILE *fp = fopen("../../dashboard/demo_events_sim.jsonl", "rb");
    TEST_ASSERT_NOT_NULL(fp);
    std::vector<std::string> gold;
    char buf[2048];
    while (fgets(buf, sizeof buf, fp)) {
        std::string l(buf);
        while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
        gold.push_back(l);
    }
    fclose(fp);
    TEST_ASSERT_EQUAL(DEMO_N_WINDOWS + 15, gold.size());   // meta + samples + 14 event/status/damage lines

    Rig r;
    r.run_until(1000000ull + (DEMO_N_WINDOWS + 14ull) * SUPERFRAME_US, DEMO_N_WINDOWS);
    TEST_ASSERT_EQUAL(DEMO_N_WINDOWS, r.sink.samples.size());
    // my lines in emission order = gold lines without the meta line (the master firmware writes its own meta)
    std::vector<std::string> mine = r.sink.lines;
    TEST_ASSERT_TRUE(mine.size() >= gold.size() - 1);
    size_t gi = 1, mi = 0, compared = 0;
    while (gi < gold.size() && mi < mine.size()) {
        TEST_ASSERT_EQUAL_STRING_MESSAGE(strip_t(gold[gi]).c_str(), strip_t(mine[mi]).c_str(), ("line " + std::to_string(gi)).c_str());
        gi++; mi++; compared++;
    }
    TEST_ASSERT_TRUE(compared >= gold.size() - 1);
    bool gold_has_damage = false;
    for (size_t i = 0; i < gold.size(); i++)
        if (gold[i].find("\"type\":\"damage\"") != std::string::npos) gold_has_damage = true;
    TEST_ASSERT_TRUE_MESSAGE(gold_has_damage, "the golden demo log must contain the DAMAGE BEGIN");
    TEST_ASSERT_EQUAL_UINT32(0, r.sink.overflow);
    // the comparison is not vacuous: the golden log carries real AE ratios and f1 estimates from the slaves' pipeline
    int with_ae = 0, with_f1 = 0;
    for (size_t i = 0; i < gold.size(); i++) {
        if (gold[i].find("\"type\":\"sample\"") == std::string::npos) continue;
        if (gold[i].find("\"ae\":[null,null]") == std::string::npos) with_ae++;
        if (gold[i].find("\"f1\":[null,null]") == std::string::npos) with_f1++;
    }
    TEST_ASSERT_TRUE(with_ae >= 290);   // AE ready from the 21st window of 320
    TEST_ASSERT_TRUE(with_f1 >= 250);   // f1 valid from the 32nd window (at least one slave)
}

// The full slave pipeline is slow on the Wokwi ESP32 (tens of ms per window, two slaves in one process). The windows are computed
// right after the beacon, so even 60 ms (or 150 ms) of blocked host time per slave never makes the master's polls late.
void test_slow_window_computation_does_not_delay_polls() {
    const uint64_t per[3] = {0, 60000, 150000};
    uint32_t base_reports = 0;
    for (int k = 0; k < 3; k++) {
        Rig r;
        r.src.per_get_us = per[k];
        r.run_until(1000000ull + 40ull * SUPERFRAME_US);
        const MasterStats &s = r.master.stats();
        char msg[64];
        snprintf(msg, sizeof msg, "compute %u us per window", (unsigned)per[k]);
        TEST_ASSERT_TRUE_MESSAGE(r.src.gets >= 70, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0, s.missed_total[0] + s.missed_total[1], msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0, s.retries[0] + s.retries[1], msg);
        TEST_ASSERT_TRUE_MESSAGE(r.master.alive(1) && r.master.alive(2), msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0, s.beacons_skipped, msg);
        TEST_ASSERT_TRUE_MESSAGE(s.reports[0] >= s.beacons - 1 && s.reports[1] >= s.beacons - 1, msg);
        if (k == 0) base_reports = s.reports[0];
        else TEST_ASSERT_TRUE_MESSAGE(s.reports[0] + 1 >= base_reports, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0, r.vnet.dropped(), msg);
    }
}

void test_demo_trace_loops_and_keeps_producing() {
    // three laps: the events repeat (the baselines survive), no node ever dies, no stall
    Rig r;
    r.run_until(1000000ull + 3ull * DEMO_N_WINDOWS * SUPERFRAME_US + 5000000ull);
    TEST_ASSERT_TRUE(r.sink.samples.size() >= 3 * DEMO_N_WINDOWS);
    int begins = 0;
    for (size_t i = 0; i < r.sink.events.size(); i++)
        if (r.sink.events[i].find("\"type\":\"begin\"") != std::string::npos) begins++;
    TEST_ASSERT_TRUE_MESSAGE(begins >= 9, "expected 3 begin events per lap (impact, building, shift)");
    TEST_ASSERT_TRUE(r.master.alive(1) && r.master.alive(2));
}

// With CLOUD_DEMO_OUT=<file> the log of one lap is written exactly as the master firmware would print it (meta with source
// "wokwi_demo", restart, then every sample / event line) so dashboard/tools/validate_logs.py can check it.
void test_cloud_demo_log_for_validator() {
    const char *path = getenv("CLOUD_DEMO_OUT");
    if (!path) return;
    Rig r;
    r.run_until(1000000ull + (DEMO_N_WINDOWS + 14ull) * SUPERFRAME_US, DEMO_N_WINDOWS);
    FILE *fp = fopen(path, "wb");
    TEST_ASSERT_NOT_NULL(fp);
    char b[fusion::json::JSON_LINE_MAX];
    fusion::json::Meta m;
    m.source = "wokwi_demo";
    m.cfg = r.fus.config();
    m.floor1 = DEMO_FLOOR_1;
    m.floor2 = DEMO_FLOOR_2;
    m.epoch = 0xC0FFEEu;
    m.samples = true;
    size_t n = fusion::json::meta_line(b, sizeof b, m);
    fwrite(b, 1, n, fp);
    n = fusion::json::restart_line(b, sizeof b, 0, 0xC0FFEEu, 0);
    fwrite(b, 1, n, fp);
    for (size_t i = 0; i < r.sink.lines.size(); i++) fwrite(r.sink.lines[i].data(), 1, r.sink.lines[i].size(), fp);
    fclose(fp);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_header_matches_trace_file);
    RUN_TEST(test_virtual_slaves_every_report_reaches_master_in_time);
    RUN_TEST(test_cloud_demo_lines_equal_netsim_output_of_the_same_traces);
    RUN_TEST(test_slow_window_computation_does_not_delay_polls);
    RUN_TEST(test_demo_trace_loops_and_keeps_producing);
    RUN_TEST(test_cloud_demo_log_for_validator);
    return UNITY_END();
}
