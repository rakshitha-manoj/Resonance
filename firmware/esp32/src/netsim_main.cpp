// CLI for the 3-node network simulator. See include/netsim.h.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "netsim.h"

static void usage() {
    printf("netsim [--seed N] [--superframes N] [--loss P] [--corrupt P] [--delay US] [--jitter US]\n"
           "       [--compute US] [--kill NODE:SF]... [--revive NODE:SF]... [--kill-us NODE:US]... [--revive-us NODE:US]...\n"
           "       [--tx-fail P] [--fail-send NODE:FROM_US:UNTIL_US]... [--master-late US] [--quiet]\n"
           "  loss/corrupt/tx-fail are probabilities 0..1 (per receiver link / per delivered copy / per send).\n"
           "  NODE is 0 (master), 1 or 2. Killing the master and reviving it = master restart with a fresh epoch\n"
           "  and superframe numbering from 0. --kill/--revive take effect at the beacon time of superframe SF;\n"
           "  the -us forms at an arbitrary microsecond. --master-late delays every master wakeup by up to US.\n"
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

int main(int argc, char **argv) {
    netsim::SimConfig c;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : nullptr;
        netsim::Action act;
        netsim::TimedAction ta;
        netsim::FailWindow fw;
        if (!strcmp(a, "--quiet")) c.quiet = true;
        else if (!strcmp(a, "--help") || !strcmp(a, "-h")) { usage(); return 0; }
        else if (!v) { usage(); return 1; }
        else if (!strcmp(a, "--seed")) c.seed = (uint32_t)strtoul(v, 0, 10), i++;
        else if (!strcmp(a, "--superframes")) c.superframes = (uint32_t)strtoul(v, 0, 10), i++;
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
    netsim::Sim sim(c);
    sim.run();
    if (!c.quiet)
        for (size_t i = 0; i < sim.res.log.size(); i++) printf("%s\n", sim.res.log[i].c_str());
    printf("%s", sim.res.summary().c_str());
    return (sim.res.viol_tx_in_sampling || sim.res.viol_poll_overrun) ? 2 : 0;
}
