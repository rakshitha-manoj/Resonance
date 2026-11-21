// CLI for the 3-node network simulator. See include/netsim.h.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include "netsim.h"
#include "fusion.h"
#include "fusion_json.h"

static void usage() {
    printf("netsim [--seed N] [--superframes N] [--loss P] [--corrupt P] [--delay US] [--jitter US]\n"
           "       [--compute US] [--kill NODE:SF]... [--revive NODE:SF]... [--kill-us NODE:US]... [--revive-us NODE:US]...\n"
           "       [--tx-fail P] [--fail-send NODE:FROM_US:UNTIL_US]... [--master-late US] [--quiet]\n"
           "       [--traces FILE] [--fusion] [--events-jsonl FILE] [--no-samples] [--floors A,B] [--no-analytics]\n"
           "  loss/corrupt/tx-fail are probabilities 0..1 (per receiver link / per delivered copy / per send).\n"
           "  NODE is 0 (master), 1 or 2. Killing the master and reviving it = master restart with a fresh epoch\n"
           "  and superframe numbering from 0. --kill/--revive take effect at the beacon time of superframe SF;\n"
           "  the -us forms at an arbitrary microsecond. --master-late delays every master wakeup by up to US.\n"
           "  --events-jsonl FILE writes the schema-3 event log (implies --fusion): meta, begin/change/status events, one\n"
           "  'sample' line per superframe (drop them with --no-samples), a 'node' line per slave DEAD/ALIVE change, a\n"
           "  'damage' line per DAMAGE begin/end and a 'restart' line per master restart.\n"
           "  --no-analytics: slaves report the tree features only (no AE, no f1 tracker; wire v3 fields unavailable).\n"
           "  --floors A,B = physical floors of slave 1 and 2 (default: FILE.floors next to --traces, else 1,2).\n"
           "  Limits: i.i.d. loss, no collisions or airtime modelling.\n");
}

static bool parse_action(const char *s, netsim::Action *a) {
    int slave;
    unsigned sf;
    if (sscanf(s, "%d:%u", &slave, &sf) != 2 || slave < 0 || slave > net::NUM_SLAVES) return false;
    a->slave = slave;
    a->sf = sf;
    return true;
}

static bool parse_timed(const char *s, netsim::TimedAction *a) {
    int node;
    unsigned long long t;
    if (sscanf(s, "%d:%llu", &node, &t) != 2 || node < 0 || node > net::NUM_SLAVES) return false;
    a->node = node;
    a->t_us = t;
    return true;
}

static bool parse_fail(const char *s, netsim::FailWindow *w) {
    int node;
    unsigned long long a, b;
    if (sscanf(s, "%d:%llu:%llu", &node, &a, &b) != 3 || node < 0 || node > net::NUM_SLAVES) return false;
    w->node = node;
    w->from_us = a;
    w->until_us = b;
    return true;
}

// Prints fusion events and optionally writes them as JSON lines (shared formatter: include/fusion_json.h).
struct FusionOut : fusion::json::JsonLineSink {
    FILE *jl = nullptr;
    std::vector<std::string> lines_txt;
    static void num(char *b, size_t n, float v) {
        if (v < 0.0f) snprintf(b, n, "null");
        else snprintf(b, n, "%.9g", (double)v);
    }
    void on_event(const fusion::Event &e) override {
        const char *type = e.kind == fusion::EV_BEGIN ? "begin" : e.kind == fusion::EV_CHANGE ? "change" : "status";
        char r1[32], r2[32];
        num(r1, sizeof r1, e.r[0]);
        num(r2, sizeof r2, e.r[1]);
        char b[512];
        snprintf(b, sizeof b,
                 "fusion t=%llu sf=%u %-6s %s slave=%u degraded=%u r1=%s r2=%s alive=%u%u",
                 (unsigned long long)e.t_us, e.sf, type, fusion::cls_name(e.cls), e.slave, e.degraded, r1, r2, e.alive_mask & 1, (e.alive_mask >> 1) & 1);
        lines_txt.push_back(b);
        if (jl) JsonLineSink::on_event(e);
    }
    void on_sample(const fusion::Sample &s) override {
        if (jl) JsonLineSink::on_sample(s);
    }
    void on_damage(const fusion::DamageEvent &d) override {
        char b[256];
        snprintf(b, sizeof b, "fusion t=%llu sf=%u damage %s pooled_drop=%.5g", (unsigned long long)d.t_us, d.sf, d.begin ? "BEGIN" : "END", (double)d.drop);
        lines_txt.push_back(b);
        if (jl) JsonLineSink::on_damage(d);
    }
    bool write_line(const char *line, size_t len, bool) override { return fwrite(line, 1, len, jl) == len; }
};

// MasterListener between the simulator and fusion: writes the meta line before the first callback (the master epoch is only
// known once Sim::run() has started) and a restart line whenever the master's epoch changes (kill + revive of node 0).
struct Tap : net::MasterListener {
    netsim::Sim *sim = nullptr;
    fusion::Fusion *fus = nullptr;
    FusionOut *out = nullptr;
    fusion::json::Meta meta;
    uint32_t last_epoch = 0;
    bool meta_done = false;
    char buf[fusion::json::JSON_LINE_MAX];

    void begin(uint64_t now) {
        uint32_t e = sim->master_epoch();
        if (!meta_done) {
            meta_done = true;
            meta.epoch = e;
            last_epoch = e;
            if (out->jl) out->emit(buf, fusion::json::meta_line(buf, sizeof buf, meta), false);
        } else if (e != last_epoch) {
            if (out->jl) out->emit(buf, fusion::json::restart_line(buf, sizeof buf, now, e, last_epoch), false);
            last_epoch = e;
        }
    }
    void on_report(uint8_t slave, const net::ReportPkt &r, uint64_t now) override {
        begin(now);
        fus->on_report(slave, r, now);
        sim->on_report(slave, r, now);
    }
    void on_status(uint8_t slave, bool alive, uint64_t now) override {
        begin(now);
        fus->on_status(slave, alive, now);
        if (out->jl) out->node(now, sim->master().current_sf(), slave, alive);
        sim->on_status(slave, alive, now);
    }
    void service(const net::Master &m, uint64_t now) {
        begin(now);
        fus->advance(m.current_sf(), m.polling_active(), now);
    }
};

static bool parse_floors(const char *s, int *f1, int *f2) {
    int a, b;
    char tail = 0;
    int n = sscanf(s, "%d,%d%c", &a, &b, &tail);
    if (!(n == 2 || (n == 3 && isspace((unsigned char)tail)))) return false;
    *f1 = a;
    *f2 = b;
    return true;
}

// "<traces>.floors" next to the trace file: one line "5,10".
static bool read_floors_sidecar(const char *traces, int *f1, int *f2) {
    std::string p = std::string(traces) + ".floors";
    FILE *fp = fopen(p.c_str(), "rb");
    if (!fp) return false;
    char line[64] = {0};
    bool ok = fgets(line, sizeof line, fp) != nullptr && parse_floors(line, f1, f2);
    fclose(fp);
    return ok;
}

int main(int argc, char **argv) {
    netsim::SimConfig c;
    const char *traces = nullptr, *jsonl = nullptr;
    bool use_fusion = false, superframes_set = false, samples = true, floors_set = false;
    int floor1 = 1, floor2 = 2;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : nullptr;
        netsim::Action act;
        netsim::TimedAction ta;
        netsim::FailWindow fw;
        if (!strcmp(a, "--quiet")) c.quiet = true;
        else if (!strcmp(a, "--fusion")) use_fusion = true;
        else if (!strcmp(a, "--no-samples")) samples = false;
        else if (!strcmp(a, "--no-analytics")) c.analytics = false;
        else if (!strcmp(a, "--help") || !strcmp(a, "-h")) { usage(); return 0; }
        else if (!v) { usage(); return 1; }
        else if (!strcmp(a, "--seed")) c.seed = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--superframes")) c.superframes = (uint32_t)strtoul(v, 0, 10), superframes_set = true, i++;
        else if (!strcmp(a, "--traces")) traces = v, i++;
        else if (!strcmp(a, "--floors")) {
            if (!parse_floors(v, &floor1, &floor2)) { fprintf(stderr, "netsim: --floors wants A,B (two integers)\n"); return 1; }
            floors_set = true;
            i++;
        }
        else if (!strcmp(a, "--events-jsonl")) jsonl = v, use_fusion = true, i++;
        else if (!strcmp(a, "--loss")) c.loss = atof(v), i++;
        else if (!strcmp(a, "--corrupt")) c.corrupt = atof(v), i++;
        else if (!strcmp(a, "--delay")) c.delay_us = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--jitter")) c.jitter_us = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--compute")) c.compute_us = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--tx-fail")) c.tx_fail = atof(v), i++;
        else if (!strcmp(a, "--master-late")) c.master_late_max_us = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--kill") && parse_action(v, &act)) c.kills.push_back(act), i++;
        else if (!strcmp(a, "--revive") && parse_action(v, &act)) c.revives.push_back(act), i++;
        else if (!strcmp(a, "--kill-us") && parse_timed(v, &ta)) c.kills_us.push_back(ta), i++;
        else if (!strcmp(a, "--revive-us") && parse_timed(v, &ta)) c.revives_us.push_back(ta), i++;
        else if (!strcmp(a, "--fail-send") && parse_fail(v, &fw)) c.fail_windows.push_back(fw), i++;
        else { usage(); return 1; }
    }
    netsim::TraceSource trace;
    if (traces) {
        std::string err;
        if (!trace.load(traces, &err)) { fprintf(stderr, "netsim: %s\n", err.c_str()); return 1; }
        if (!superframes_set) c.superframes = trace.windows();
    }
    netsim::Sim sim(c, traces ? &trace : nullptr);
    if (!floors_set && traces) read_floors_sidecar(traces, &floor1, &floor2);
    FusionOut fout;
    fout.samples = samples;
    fusion::Fusion fus(&fout);
    Tap tap;
    tap.sim = &sim;
    tap.fus = &fus;
    tap.out = &fout;
    tap.meta.source = "netsim";
    tap.meta.cfg = fus.config();
    tap.meta.floor1 = floor1;
    tap.meta.floor2 = floor2;
    tap.meta.epoch = 0;
    tap.meta.samples = samples;
    if (jsonl) {
        fout.jl = fopen(jsonl, "wb");
        if (!fout.jl) { fprintf(stderr, "netsim: cannot write %s\n", jsonl); return 1; }
    }
    if (use_fusion) {
        sim.master().set_listener(&tap);
        sim.master_hook = [&](const net::Master &m, uint64_t now) { tap.service(m, now); };
    }
    sim.run();
    if (use_fusion) {
        tap.begin(c.superframes * (uint64_t)net::SUPERFRAME_US);  // meta even if the run produced no callback
        fus.flush(c.superframes * (uint64_t)net::SUPERFRAME_US);
    }
    if (!c.quiet)
        for (size_t i = 0; i < sim.res.log.size(); i++) printf("%s\n", sim.res.log[i].c_str());
    printf("%s", sim.res.summary().c_str());
    printf("%s", sim.res.host_summary().c_str());
    if (use_fusion) {
        for (size_t i = 0; i < fout.lines_txt.size(); i++) printf("%s\n", fout.lines_txt[i].c_str());
        const fusion::Stats &st = fus.stats();
        printf("fusion summary: rule=%c T=%g T_b=%g gate=%g use_raw=%d superframes_closed=%u commissioned=%d base1=%.2f base2=%.2f\n", fus.config().rule, (double)fus.config().T,
               (double)fus.config().T_b, (double)fus.config().gate, fus.config().use_raw ? 1 : 0, st.closed, fus.commissioned() ? 1 : 0, (double)fus.baseline(1), (double)fus.baseline(2));
        printf("fusion summary: damage active=%d begins=%u ends=%u f1_0=%.3f,%.3f ae_bad=%u f1_bad=%u\n", fus.damage_active() ? 1 : 0, st.dmg_begins, st.dmg_ends,
               (double)fus.slave_f1_0(1), (double)fus.slave_f1_0(2), st.ae_bad, st.f1_bad);
        printf("fusion summary: COMMISSIONING=%u AMBIENT=%u LOCAL=%u (slave1=%u slave2=%u) BUILDING=%u NO_DATA=%u degraded=%u events=%u rejected_reports=%u restarts=%u\n",
               st.by_cls[fusion::COMMISSIONING], st.by_cls[fusion::AMBIENT], st.by_cls[fusion::LOCAL], st.local_by_slave[0], st.local_by_slave[1],
               st.by_cls[fusion::BUILDING], st.by_cls[fusion::NO_DATA], st.degraded_sf, fus.events_emitted(), st.rejected_reports, st.restarts);
    }
    if (fout.jl) {
        printf("events-jsonl: %s schema=%d lines=%u (samples=%u) dropped=%u floors=%d,%d\n", jsonl, fusion::json::SCHEMA, fout.lines,
               fout.sample_lines, fout.dropped, floor1, floor2);
        fclose(fout.jl);
    }
    return (sim.res.viol_tx_in_sampling || sim.res.viol_poll_overrun) ? 2 : 0;
}
