"""Writes firmware/esp32/test/data/fusion_vectors.bin: parity vectors for include/fusion.h.

Each sequence is a deterministic stream of master-side operations (report / DEAD-ALIVE status / advance / flush) plus the
decisions, events and statuses that the Python reference (RefFusion in models/training/fusion_eval.py) produces for it. The C++
test replays the operations through Fusion and requires every output to match exactly, floats bit for bit.
Covers all 63 candidate configurations of rules a-d = 21 rule/T/variant x debounce k in {1,2,3} (short sequences, small
commissioning, no AE), four targeted rule-c sequences that sweep r_other through exactly 1.5, rule-f candidates (AE T x T_b x k,
short sequences with AE_READY transitions, restarts, DEAD/ALIVE), targeted rule-f edge sequences (AE ratio one ulp below / exactly /
one ulp above T for either slave; az_std one ulp below / exactly / one ulp above the 92.12 gate; AE_READY off / on; both flags
OR-ed; each flag's own 2-of-3 history), and long sequences with the FINAL selected configuration (rule f, T_AE 6, T_b 3, k 1):
missing reports, DEAD/ALIVE, commissioning with gaps, ratios at exactly T and one ulp either side, AE ready transitions, a master
restart, a 40-superframe gap, rejected (NaN / inf / negative) reports.

The final selected configuration is read from models/selected/validation/fusion_eval_r3_selected.json (rule, T = AE threshold,
T_b, k, gated) - the Phase C1 pre-registered decision - plus the noise gate from fusion_eval_r3_dev.json (setup.noise_gate_counts)
and the commissioning windows from fusion_eval.json (R2b setup), and embedded in the file (version 4), so the C++ test can assert
that the CHOSEN_* constants of include/fusion.h equal it. This generator does NOT read fusion.h: changing a constant in fusion.h
without re-running the selection is caught by the test.

Run:  .venv/Scripts/python.exe firmware/esp32/tools/gen_fusion_vectors.py     (deterministic; in the repo)

File layout (version 4), little endian:
  "FUSV" u32 version=4 u32 n_sequences
  selected configuration (32 B): u8 rule(ascii) u8 use_raw u8 debounce_k u8 comm_n | f32 T | u32 status_every | u32 max_catchup |
                                 f32 local_other_max (rule c) | f32 T_b | f32 gate | u8 gated u8[3] pad -- then the sequences:
  per sequence (32 B): u8 rule(ascii) u8 use_raw u8 comm_n u8 debounce_k | u32 status_every | f32 T | f32 T_b | f32 gate |
                       u32 n_ops u32 n_dec u32 n_rec | u32 pad
    ops    (19 B): u8 type(1 report,2 status,3 advance,4 flush) u8 slave u8 flagbits | u32 sf | f32 f0 | f32 ae | u32 t_ms
             report: flagbits = raw | state<<2 | ae_ready<<4 ; status: flagbits = alive ; advance: slave unused, flagbits =
             polling_active, sf = master sf
    dec    (16 B): u32 sf u8 cls u8 slave u8 degraded u8 pad | f32 r1 f32 r2
    rec    (40 B): u8 kind(1 begin,2 change,3 status) u8 cls u8 slave u8 degraded | u32 sf u32 t_ms | f32 r1 f32 r2 f32 base1 f32 base2 |
             u8 alive_mask u8[3] pad | u32 closed u32 n_events
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "models" / "training"))
sys.path.insert(0, str(ROOT / "data" / "generator"))
sys.dont_write_bytecode = True
import fusion_eval as fe  # noqa: E402

OUT = ROOT / "firmware" / "esp32" / "test" / "data" / "fusion_vectors.bin"
VAL = ROOT / "models" / "selected" / "validation"
SELECTED_JSON = VAL / "fusion_eval_r3_selected.json"     # the Phase C1 decision (rule f)
DEV_JSON = VAL / "fusion_eval_r3_dev.json"               # setup.noise_gate_counts
R2B_JSON = VAL / "fusion_eval.json"                      # commissioning windows
BASES = (np.float32(100.0), np.float32(128.0))
OP_REPORT, OP_STATUS, OP_ADVANCE, OP_FLUSH = 1, 2, 3, 4
F32 = np.float32
GATE = F32(fe.GATE32)
UP = F32(1e9)


def chosen_from_json():
    """The deployed configuration: rule, T (AE threshold for f), T_b, use_raw, k, comm_n, gate."""
    sel = json.loads(SELECTED_JSON.read_text(encoding="utf-8"))["config"]
    rule, T, T_b, k, gated = str(sel["rule"]), float(sel["T"]), float(sel["T_b"] or sel["T"]), int(sel["k"]), bool(sel["gated"])
    comm_n = int(json.loads(R2B_JSON.read_text(encoding="utf-8"))["rounds"]["R2b"]["setup"]["commissioning_windows"])
    gate = float(json.loads(DEV_JSON.read_text(encoding="utf-8"))["setup"]["noise_gate_counts"])
    assert rule == "f" and gated and k in (1, 2, 3), sel
    assert F32(gate) == GATE, (gate, GATE)
    return rule, T, T_b, False, k, comm_n, gate


def f0_menu(rng, T, base):
    b = F32(base)
    edge = F32(T) * b
    opts = [b * F32(0.4), b * F32(0.8), b, b * F32(1.2), edge, np.nextafter(edge, F32(0)), np.nextafter(edge, UP), b * F32(1.5),
            np.nextafter(b * F32(1.5), F32(0)), np.nextafter(b * F32(1.5), UP), b * F32(2.5), b * F32(4), b * F32(10)]
    w = np.array([2, 3, 3, 2, 3, 2, 2, 3, 2, 2, 2, 2, 1], dtype=float)
    return F32(opts[rng.choice(len(opts), p=w / w.sum())])


def f0_menu_f(rng, T_b, base):
    """Rule f: the b menu plus the noise-gate edge (az_std one ulp below / exactly / one ulp above 92.12) and values far below it."""
    if rng.random() < 0.25:
        opts = [GATE, np.nextafter(GATE, F32(0)), np.nextafter(GATE, UP), F32(10.0), F32(50.0), F32(0.0)]
        return F32(opts[rng.integers(len(opts))])
    return f0_menu(rng, T_b, base)


def ae_menu(rng, T):
    t = F32(T)
    opts = [F32(0.0), F32(0.7), F32(1.0), F32(2.0), t * F32(0.5), np.nextafter(t, F32(0)), t, np.nextafter(t, UP), t * F32(1.5), t * F32(5), F32(1000.0)]
    w = np.array([1, 3, 3, 2, 2, 3, 3, 3, 2, 2, 1], dtype=float)
    return F32(opts[rng.choice(len(opts), p=w / w.sum())])


def make_ops(rng, n_comm_sf, n_sf, T, comm_n, restart=True, gap=False, bad=True, fmode=False, T_b=3.0):
    """-> list of ops (type, slave, flagbits, sf, f0, ae, t_ms). fmode: rule-f traffic (AE ratios around T, gate-edge az_std,
    AE_READY dropping out for a few windows as after a slave reboot)."""
    ops = []

    def rep(slave, sf, raw, state, f0, t, ae=0.0, ready=False):
        ops.append((OP_REPORT, slave, raw | (state << 2) | (16 if ready else 0), sf, F32(f0), F32(ae), t))

    sf = 0
    miss = [0, 0]
    outage = [0, 0]
    dead = [False, False]
    t = 100
    total = n_comm_sf + n_sf
    restart_at = n_comm_sf + int(n_sf * 0.55) if restart else -1
    gap_at = n_comm_sf + int(n_sf * 0.25) if gap else -1
    bad_done = False
    prev = [None, None]
    unready_until = [-1, -1]   # AE not ready until this k (a rebooted slave recommissioning its AE)
    for k in range(total):
        if k == restart_at:
            sf = 0  # master restart: numbering restarts
        if k == gap_at:
            sf += 40  # master stalled / long outage: more than MAX_CATCHUP_SF superframes without any report
        t = 100 + k * 1400 + 1320
        poll_adv = rng.random() < 0.7
        if poll_adv:
            ops.append((OP_ADVANCE, 0, 1, sf, F32(0), F32(0), t - 1320))
        got = [False, False]
        for i in (0, 1):
            if outage[i] > 0:
                outage[i] -= 1
                continue
            if k > n_comm_sf and rng.random() < 0.03:
                outage[i] = int(rng.integers(3, 8))
                outage[i] -= 1
                continue
            if rng.random() < 0.07:  # single lost report
                continue
            got[i] = True
        for i in (0, 1):
            if got[i]:
                if dead[i]:
                    ops.append((OP_STATUS, i + 1, 1, 0, F32(0), F32(0), t + i))
                    dead[i] = False
                    if fmode:
                        unready_until[i] = k + int(rng.integers(0, 6))  # a revived slave usually restarted: its AE is not ready for a while
                miss[i] = 0
                raw = state = 0
                ae, ready = F32(0), False
                if k < n_comm_sf:
                    f0 = F32(BASES[i])  # commissioning: exactly the base value (outliers added below)
                    if rng.random() < 0.1:
                        f0 = F32(1.0) if rng.random() < 0.5 else F32(5000.0)  # outlier; median stays at the base
                    if fmode:
                        ae, ready = ae_menu(rng, T), bool(rng.random() < 0.5)   # ignored while commissioning
                else:
                    if fmode and k > n_comm_sf + 3 and unready_until[i] < k and rng.random() < 0.04:
                        unready_until[i] = k + int(rng.integers(1, 8))
                    if prev[i] is not None and rng.random() < 0.55:
                        f0, raw, state, ae, ready = prev[i]
                    else:
                        f0 = f0_menu_f(rng, T_b, BASES[i]) if fmode else f0_menu(rng, T, BASES[i])
                        state = int(rng.choice(3, p=[0.5, 0.15, 0.35]))
                        raw = int(rng.choice(3, p=[0.5, 0.15, 0.35]))
                        if fmode:
                            ae = ae_menu(rng, T)
                    ready = bool(fmode and k > unready_until[i])
                    prev[i] = (f0, raw, state, ae, ready)
                rep(i + 1, sf, raw, state, f0, t + i, ae, ready)
            else:
                miss[i] += 1
        if bad and not bad_done and k == n_comm_sf + 5:
            bad_done = True
            rep(1, sf, 0, 0, F32(np.nan), t)
            rep(2, sf, 0, 0, F32(-5.0), t)
            rep(1, sf, 0, 0, F32(np.inf), t)
        for i in (0, 1):
            if miss[i] >= 3 and not dead[i] and not got[i]:
                ops.append((OP_STATUS, i + 1, 0, 0, F32(0), F32(0), t + 5))
                dead[i] = True
        if rng.random() < 0.6:
            ops.append((OP_ADVANCE, 0, 0, sf, F32(0), F32(0), t + 6))
        sf += 1
    ops.append((OP_FLUSH, 0, 0, 0, F32(0), F32(0), t + 1400))
    return ops


class Stream:
    """Both-slaves-report-every-superframe op builder for the targeted sequences."""

    def __init__(self):
        self.ops, self.sf, self.t = [], 0, 100

    def both(self, f1, f2, ae1=0.0, ae2=0.0, rdy1=False, rdy2=False, st1=0, st2=0):
        self.ops.append((OP_REPORT, 1, (st1 << 2) | (16 if rdy1 else 0), self.sf, F32(f1), F32(ae1), self.t))
        self.ops.append((OP_REPORT, 2, (st2 << 2) | (16 if rdy2 else 0), self.sf, F32(f2), F32(ae2), self.t + 1))
        self.sf += 1
        self.t += 1400

    def one(self, slave, f, ae=0.0, rdy=False):
        """one report then the master finishes polling the superframe (DEGRADED)"""
        self.ops.append((OP_ADVANCE, 0, 1, self.sf, F32(0), F32(0), self.t - 1000))
        self.ops.append((OP_REPORT, slave, 16 if rdy else 0, self.sf, F32(f), F32(ae), self.t))
        self.ops.append((OP_ADVANCE, 0, 0, self.sf, F32(0), F32(0), self.t + 1000))
        self.sf += 1
        self.t += 1400

    def status(self, slave, alive):
        self.ops.append((OP_STATUS, slave, 1 if alive else 0, 0, F32(0), F32(0), self.t))

    def finish(self):
        self.ops.append((OP_FLUSH, 0, 0, 0, F32(0), F32(0), self.t))
        return self.ops


def make_rule_c_edge_ops(comm_n, base=100.0):
    """Rule c: sweep r_other through exactly LOCAL_OTHER_MAX = 1.5 for either slave as the local one (hot slave: f0 = 4 x base,
    tree state faulty). Order of the six cases: other slave at 1 ulp below, exactly, 1 ulp above 1.5 x base; slave 1 hot then slave 2 hot.
    Each case = 3 superframes of the pattern then 3 quiet superframes (clears the 2-of-3 history)."""
    st = Stream()
    b = F32(base)
    edge = F32(1.5) * b
    others = [np.nextafter(edge, F32(0)), edge, np.nextafter(edge, UP)]
    for _ in range(comm_n):
        st.both(b, b)
    for hot in (1, 2):
        for o in others:
            for _ in range(3):
                if hot == 1:
                    st.both(F32(4) * b, o, st1=2)
                else:
                    st.both(o, F32(4) * b, st2=2)
            for _ in range(3):
                st.both(b, b)
    return st.finish()


def make_rule_f_edge_ops(comm_n, T=6.0, base=100.0):
    """Rule f targeted sweeps (b part never fires: f0 stays at the baseline unless a case says otherwise):
    A  AE ratio one ulp below / exactly / one ulp above T, slave 1 then slave 2 (two windows each; the AE flag has its own 2-of-3,
       so LOCAL(i) from the second window only when ratio >= T);            -> 6 cases x (2 + 3 quiet)
    B  noise gate: az_std one ulp below / exactly / one ulp above 92.12 with AE ratio 100 (ready), slave 1 then slave 2: only one ulp
       below the gate silences the AE flag (below = false; exactly the gate counts);   -> 6 cases x (2 + 3 quiet)
    C  AE not ready with ratio 100 (ignored), then ready (AE flag follows, history advanced while not ready);
    D  b OR e: slave 1 b-flag only, slave 2 AE flag only -> BUILDING; the two flags smooth separately (alternating b / AE windows
       on one slave never reach 2 of 3 in either history);
    E  degraded: only slave 2 reports (AE alone, b alone), DEAD / ALIVE clears BOTH histories, master restart clears them.
    All reports of the cases carry the same ops through RefFusion, the expected outputs come from it."""
    st = Stream()
    b = F32(base)
    quiet = lambda: st.both(b, b, 0.5, 0.5, True, True)  # noqa: E731
    for _ in range(comm_n):
        st.both(b, b, 0.0, 0.0, bool(_ % 2), bool(_ % 3))   # AE ignored during commissioning
    quiet(); quiet(); quiet()
    t = F32(T)
    ae_edges = [np.nextafter(t, F32(0)), t, np.nextafter(t, UP)]
    for hot in (1, 2):                     # A
        for a in ae_edges:
            for _ in range(2):
                st.both(b, b, a if hot == 1 else 0.5, a if hot == 2 else 0.5, True, True)
            for _ in range(3):
                quiet()
    gate_edges = [np.nextafter(GATE, F32(0)), GATE, np.nextafter(GATE, UP)]
    for hot in (1, 2):                     # B
        for g in gate_edges:
            for _ in range(2):
                st.both(g if hot == 1 else b, g if hot == 2 else b, 100.0 if hot == 1 else 0.5, 100.0 if hot == 2 else 0.5, True, True)
            for _ in range(3):
                quiet()
    for _ in range(3):                     # C: ready off with a huge ratio, then on
        st.both(b, b, 100.0, 100.0, False, False)
    for _ in range(3):
        st.both(b, b, 100.0, 100.0, True, True)
    for _ in range(4):
        quiet()
    for _ in range(3):                     # D: slave 1 by b alone, slave 2 by the AE alone -> both e -> BUILDING
        st.both(F32(4) * b, b, 0.5, 100.0, True, True)
    for _ in range(4):
        quiet()
    for j in range(8):                     # D2: alternate b-high and AE-high windows on slave 1: neither history reaches 2 of 3
        if j % 2 == 0:
            st.both(F32(4) * b, b, 0.5, 0.5, True, True)
        else:
            st.both(b, b, 100.0, 0.5, True, True)
    for _ in range(4):
        quiet()
    for _ in range(3):                     # E: degraded
        st.one(2, b, 100.0, True)          # AE alone (slave 2 only): LOCAL(2) from the 2nd report
    for _ in range(2):
        st.one(2, F32(4) * b, 0.5, True)   # b alone joins (OR)
    st.one(2, b, 0.5, True)
    st.one(2, b, 0.5, True)
    st.one(2, b, 0.5, True)
    st.status(2, False)                    # DEAD
    st.one(1, b, 100.0, True)              # slave 2 absent: its histories are held
    st.status(2, True)                     # ALIVE after DEAD: both histories cleared
    st.both(b, b, 0.5, 100.0, True, True)  # one AE-high window: not yet (cleared history)
    st.both(b, b, 0.5, 100.0, True, True)  # second: now
    st.sf = 0                              # master restart
    st.both(b, b, 0.5, 100.0, True, True)
    st.both(b, b, 0.5, 100.0, True, True)  # history cleared by the restart: LOCAL again only now
    return st.finish()


def run_ref(rule, T, use_raw, comm_n, status_every, ops, k=1, T_b=3.0, gate=GATE):
    r = fe.RefFusion(rule, T, use_raw, comm_n=comm_n, status_every=status_every, debounce=k, T_b=T_b, gate=gate)
    for typ, slave, fb, sf, f0, ae, t in ops:
        if typ == OP_REPORT:
            r.on_report(slave, sf, fb & 3, (fb >> 2) & 3, f0, t, ae, bool(fb & 16))
        elif typ == OP_STATUS:
            r.on_status(slave, bool(fb), t)
        elif typ == OP_ADVANCE:
            r.advance(sf, bool(fb), t)
        else:
            r.flush(t)
    return r


def pack_seq(rule, T, use_raw, comm_n, status_every, ops, k=1, T_b=3.0, gate=GATE):
    r = run_ref(rule, T, use_raw, comm_n, status_every, ops, k, T_b, gate)
    b = bytearray()
    b += struct.pack("<BBBBIfffIII", ord(rule), int(use_raw), comm_n, k, status_every, float(F32(T)), float(F32(T_b)), float(F32(gate)),
                     len(ops), len(r.decisions), len(r.records))
    for typ, slave, fb, sf, f0, ae, t in ops:
        b += struct.pack("<BBBIffI", typ, slave, fb, sf, float(f0), float(ae), t)
    for sf, cls, slave, deg, r1, r2 in r.decisions:
        b += struct.pack("<IBBBBff", sf, cls, slave, deg, 0, float(r1), float(r2))
    for kind, cls, slave, deg, sf, t, r1, r2, b1, b2, am, closed, nev in r.records:
        b += struct.pack("<BBBBIIffffBBBBII", kind, cls, slave, deg, sf, t, float(r1), float(r2), float(b1), float(b2), am, 0, 0, 0, closed, nev)
    cls_counts = np.bincount([d[1] for d in r.decisions], minlength=5)
    return bytes(b), r, cls_counts


def main():
    seqs = []
    summary = []
    rng = np.random.default_rng(20240611)
    for cfg in fe.configs():                       # rules a-d: unchanged traffic (no AE)
        for k in (1, 2, 3):
            rule, T, ur = cfg["rule"], cfg["T"], cfg["use_raw"]
            comm_n = 4 if len(seqs) % 2 == 0 else 5
            ops = make_ops(rng, comm_n + 1, 20, T, comm_n, restart=len(seqs) % 4 == 0, bad=len(seqs) % 3 == 0)
            seqs.append(pack_seq(rule, T, ur, comm_n, 10, ops, k))
            summary.append((cfg["name"] + f" k={k}", len(ops)))
    for T, ur in ((2.0, False), (3.0, False), (1.5, False), (2.0, True)):
        ops = make_rule_c_edge_ops(4)
        entry = pack_seq("c", T, ur, 4, 10, ops, 1)
        if T == 2.0 and not ur:  # guard: the sweep must actually separate the cases (LOCAL below 1.5, not LOCAL at exactly 1.5)
            cls_by_sf = {d[0]: (d[1], d[2]) for d in entry[1].decisions}
            LOCAL_, AMBIENT_ = 2, 1
            assert cls_by_sf[4 + 2] == (LOCAL_, 1), "slave 1 local with r2 one ulp below 1.5"
            assert cls_by_sf[4 + 6 + 2][0] == AMBIENT_, "r2 exactly 1.5 must not be LOCAL"
            assert cls_by_sf[4 + 12 + 2][0] == AMBIENT_, "r2 one ulp above 1.5 must not be LOCAL"
            assert cls_by_sf[4 + 18 + 2] == (LOCAL_, 2), "slave 2 local with r1 one ulp below 1.5"
            assert cls_by_sf[4 + 24 + 2][0] == AMBIENT_, "r1 exactly 1.5 must not be LOCAL"
        seqs.append(entry)
        summary.append((f"rule c r_other edge T={T} raw={ur}", len(ops)))
    # ---- rule f candidates: AE T x T_b x k, short, with AE_READY transitions
    for T_ae in (3.0, 6.0, 12.0):
        for T_b in (2.0, 3.0):
            for k in (1, 2, 3):
                comm_n = 4 if len(seqs) % 2 == 0 else 5
                ops = make_ops(rng, comm_n + 1, 30, T_ae, comm_n, restart=len(seqs) % 3 == 0, bad=len(seqs) % 2 == 0, fmode=True, T_b=T_b)
                seqs.append(pack_seq("f", T_ae, False, comm_n, 10, ops, k, T_b, GATE))
                summary.append((f"rule f T_ae={T_ae} T_b={T_b} k={k}", len(ops)))
    # ---- rule f targeted edges (guards below make sure the sweep separates the cases)
    ops = make_rule_f_edge_ops(4)
    entry = pack_seq("f", 6.0, False, 4, 10, ops, 1, 3.0, GATE)
    by = {d[0]: (d[1], d[2], d[3]) for d in entry[1].decisions}
    LOCAL_, AMBIENT_, BUILDING_ = 2, 1, 3
    base_sf = 4 + 3     # 4 commissioning + 3 quiet
    # A: case j (0..5): 2 hot windows + 3 quiet; slave 1 (j 0..2: one ulp below, exactly, one ulp above), then slave 2
    for j, want in enumerate([None, 1, 1, None, 2, 2]):
        sf2 = base_sf + 5 * j + 1                 # the second hot window of case j
        got = by[sf2]
        if want is None:
            assert got[0] == AMBIENT_, ("AE one ulp below T must not flag", j, got)
        else:
            assert got[0] == LOCAL_ and got[1] == want, ("AE at / above T must give LOCAL", j, got)
    gate_sf = base_sf + 30
    # B: AE 100 with az_std one ulp below the gate -> flag false; exactly the gate and above -> counts
    for j, want in enumerate([None, 1, 1, None, 2, 2]):
        sf2 = gate_sf + 5 * j + 1
        got = by[sf2]
        if want is None:
            assert got[0] == AMBIENT_, ("az_std below the gate silences the AE flag", j, got)
        else:
            assert got[0] == LOCAL_ and got[1] == want, ("az_std at / above the gate lets the AE flag count", j, got)
    c_sf = gate_sf + 30
    assert by[c_sf + 1][0] == AMBIENT_ and by[c_sf + 2][0] == AMBIENT_, "AE not ready is ignored"
    assert by[c_sf + 3 + 1][0] == BUILDING_, "ready again: both flags (history advanced while not ready was all false)"
    seqs.append(entry)
    summary.append(("rule f AE / gate / ready / OR / degraded edges", len(ops)))
    # ---- long sequences of the FINAL selected configuration
    crule, cT, cT_b, cur, ck, ccomm, cgate = chosen_from_json()
    for j in range(3):
        ops = make_ops(rng, 24, 200 if j == 0 else 140, cT, ccomm, restart=True, gap=True, fmode=True, T_b=cT_b)
        seqs.append(pack_seq(crule, cT, cur, ccomm, 64, ops, ck, cT_b, F32(cgate)))
        summary.append((f"chosen long {j}", len(ops)))
    sel = struct.pack("<BBBBfIIfffBBBB", ord(crule), int(cur), ck, ccomm, float(F32(cT)), 64, 32, float(F32(fe.OTHER_MAX)), float(F32(cT_b)), float(F32(cgate)), 1, 0, 0, 0)
    assert len(sel) == 32
    blob = b"FUSV" + struct.pack("<II", 4, len(seqs)) + sel + b"".join(s[0] for s in seqs)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(blob)
    tot = np.zeros(5, dtype=int)
    nev = 0
    for _, r, cc in seqs:
        tot += cc
        nev += len(r.records)
    print(f"wrote {OUT} ({len(blob)} bytes, {len(seqs)} sequences); decisions by class {dict(zip(fe.CLS_NAMES, tot.tolist()))}; event/status records {nev}")
    print("final selected config (fusion_eval_r3_selected.json):", crule, "T_ae =", cT, "T_b =", cT_b, "gate =", cgate, "k =", ck, "comm_n =", ccomm)


if __name__ == "__main__":
    main()
