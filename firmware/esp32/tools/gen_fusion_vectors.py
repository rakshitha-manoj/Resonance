"""Writes firmware/esp32/test/data/fusion_vectors.bin: parity vectors for include/fusion.h.

Each sequence is a deterministic stream of master-side operations (report / DEAD-ALIVE status / advance / flush) plus the
decisions, events and statuses that the Python reference (RefFusion in models/training/fusion_eval.py) produces for it. The C++
test replays the operations through Fusion and requires every output to match exactly, floats bit for bit.
Covers all 63 candidate configurations = 21 rule/T/variant x debounce k in {1,2,3} (short sequences, small commissioning), four
targeted rule-c sequences that sweep r_other through exactly 1.5 (one ulp below / exactly / one ulp above, for either slave as the
"local" one; T = 1.5, 2, 3 and the raw variant), and two long sequences with the FINAL selected configuration: missing reports,
DEAD/ALIVE, commissioning with gaps, ratios at exactly T and one ulp either side, a master restart, a 40-superframe gap, rejected
(NaN / inf / negative) reports.

The final selected configuration is read from models/selected/validation/fusion_eval.json ("final_selected": rule, T, use_raw, k)
and embedded in the file (version 3), so the C++ test can assert that the CHOSEN_* constants of include/fusion.h equal it. This
generator does NOT read fusion.h: changing a constant in fusion.h without re-running the selection is caught by the test.

Run:  .venv/Scripts/python.exe firmware/esp32/tools/gen_fusion_vectors.py     (deterministic; under 200 KB, in the repo)

File layout, little endian:
  "FUSV" u32 version=3 u32 n_sequences
  selected configuration (20 B): u8 rule(ascii) u8 use_raw u8 debounce_k u8 comm_n | f32 T | u32 status_every | u32 max_catchup |
                                 f32 local_other_max (rule c) -- then the sequences:
  per sequence: u8 rule(ascii) u8 T_index(0:1.5 1:2.0 2:3.0) u8 use_raw u8 comm_n u8 debounce_k u8[3] pad | u32 status_every | u32 n_ops u32 n_dec u32 n_rec
    ops    (15 B): u8 type(1 report,2 status,3 advance,4 flush) u8 slave u8 flagbits | u32 sf | f32 f0 | u32 t_ms
             report: flagbits = raw | state<<2 ; status: flagbits = alive ; advance: slave unused, flagbits = polling_active, sf = master sf
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
SELECTED_JSON = ROOT / "models" / "selected" / "validation" / "fusion_eval.json"
BASES = (np.float32(100.0), np.float32(128.0))
OP_REPORT, OP_STATUS, OP_ADVANCE, OP_FLUSH = 1, 2, 3, 4
F32 = np.float32


def chosen_from_json():
    """The final selected configuration (R2b) from fusion_eval.json -> rule, T, use_raw, k, comm_n."""
    d = json.loads(SELECTED_JSON.read_text(encoding="utf-8"))
    fs = d["final_selected"]
    rule, T, raw, k = str(fs["rule"]), float(fs["T"]), bool(fs["use_raw"]), int(fs["k"])
    comm_n = int(d["rounds"]["R2b"]["setup"]["commissioning_windows"])
    assert rule in "abcd" and T in fe.T_VALUES and k in (1, 2, 3), fs
    return rule, T, raw, k, comm_n


def f0_menu(rng, T, base):
    b = F32(base)
    edge = F32(T) * b
    opts = [b * F32(0.4), b * F32(0.8), b, b * F32(1.2), edge, np.nextafter(edge, F32(0)), np.nextafter(edge, F32(1e9)), b * F32(1.5),
            np.nextafter(b * F32(1.5), F32(0)), np.nextafter(b * F32(1.5), F32(1e9)), b * F32(2.5), b * F32(4), b * F32(10)]
    w = np.array([2, 3, 3, 2, 3, 2, 2, 3, 2, 2, 2, 2, 1], dtype=float)
    return F32(opts[rng.choice(len(opts), p=w / w.sum())])


def make_ops(rng, n_comm_sf, n_sf, T, comm_n, restart=True, gap=False, bad=True):
    """-> list of ops (type, slave, flagbits, sf, f0, t_ms)."""
    ops = []

    def rep(slave, sf, raw, state, f0, t):
        ops.append((OP_REPORT, slave, raw | (state << 2), sf, F32(f0), t))

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
    for k in range(total):
        if k == restart_at:
            sf = 0  # master restart: numbering restarts
        if k == gap_at:
            sf += 40  # master stalled / long outage: more than MAX_CATCHUP_SF superframes without any report
        t = 100 + k * 1400 + 1320
        poll_adv = rng.random() < 0.7
        if poll_adv:
            ops.append((OP_ADVANCE, 0, 1, sf, F32(0), t - 1320))
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
                    ops.append((OP_STATUS, i + 1, 1, 0, F32(0), t + i))
                    dead[i] = False
                miss[i] = 0
                raw = state = 0
                if k < n_comm_sf:
                    f0 = F32(BASES[i])  # commissioning: exactly the base value (outliers added below)
                    if rng.random() < 0.1:
                        f0 = F32(1.0) if rng.random() < 0.5 else F32(5000.0)  # outlier; median stays at the base
                else:
                    if prev[i] is not None and rng.random() < 0.55:
                        f0, raw, state = prev[i]
                    else:
                        f0 = f0_menu(rng, T, BASES[i])
                        state = int(rng.choice(3, p=[0.5, 0.15, 0.35]))
                        raw = int(rng.choice(3, p=[0.5, 0.15, 0.35]))
                    prev[i] = (f0, raw, state)
                rep(i + 1, sf, raw, state, f0, t + i)
            else:
                miss[i] += 1
        if bad and not bad_done and k == n_comm_sf + 5:
            bad_done = True
            rep(1, sf, 0, 0, F32(np.nan), t)
            rep(2, sf, 0, 0, F32(-5.0), t)
            rep(1, sf, 0, 0, F32(np.inf), t)
        for i in (0, 1):
            if miss[i] >= 3 and not dead[i] and not got[i]:
                ops.append((OP_STATUS, i + 1, 0, 0, F32(0), t + 5))
                dead[i] = True
        if rng.random() < 0.6:
            ops.append((OP_ADVANCE, 0, 0, sf, F32(0), t + 6))
        sf += 1
    ops.append((OP_FLUSH, 0, 0, 0, F32(0), t + 1400))
    return ops


def make_rule_c_edge_ops(comm_n, base=100.0):
    """Rule c: sweep r_other through exactly LOCAL_OTHER_MAX = 1.5 for either slave as the local one (hot slave: f0 = 4 x base,
    tree state faulty). Order of the six cases: other slave at 1 ulp below, exactly, 1 ulp above 1.5 x base; slave 1 hot then slave 2 hot.
    Each case = 3 superframes of the pattern then 3 quiet superframes (clears the 2-of-3 history)."""
    ops = []
    b = F32(base)
    edge = F32(1.5) * b
    others = [np.nextafter(edge, F32(0)), edge, np.nextafter(edge, F32(1e9))]
    sf = 0
    t = 100

    def both(f1, f2, st1, st2):
        nonlocal sf, t
        ops.append((OP_REPORT, 1, st1 << 2, sf, F32(f1), t))
        ops.append((OP_REPORT, 2, st2 << 2, sf, F32(f2), t + 1))
        sf += 1
        t += 1400

    for _ in range(comm_n):
        both(b, b, 0, 0)
    for hot in (1, 2):
        for o in others:
            for _ in range(3):
                if hot == 1:
                    both(F32(4) * b, o, 2, 0)
                else:
                    both(o, F32(4) * b, 0, 2)
            for _ in range(3):
                both(b, b, 0, 0)
    ops.append((OP_FLUSH, 0, 0, 0, F32(0), t))
    return ops


def run_ref(rule, T, use_raw, comm_n, status_every, ops, k=1):
    r = fe.RefFusion(rule, T, use_raw, comm_n=comm_n, status_every=status_every, debounce=k)
    for typ, slave, fb, sf, f0, t in ops:
        if typ == OP_REPORT:
            r.on_report(slave, sf, fb & 3, fb >> 2, f0, t)
        elif typ == OP_STATUS:
            r.on_status(slave, bool(fb), t)
        elif typ == OP_ADVANCE:
            r.advance(sf, bool(fb), t)
        else:
            r.flush(t)
    return r


def pack_seq(rule, T, use_raw, comm_n, status_every, ops, k=1):
    r = run_ref(rule, T, use_raw, comm_n, status_every, ops, k)
    b = bytearray()
    b += struct.pack("<BBBBBBBBIIII", ord(rule), fe.T_VALUES.index(T), int(use_raw), comm_n, k, 0, 0, 0, status_every, len(ops), len(r.decisions), len(r.records))
    for typ, slave, fb, sf, f0, t in ops:
        b += struct.pack("<BBBIfI", typ, slave, fb, sf, float(f0), t)
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
    for cfg in fe.configs():
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
            assert cls_by_sf[4 + 12 + 2] [0] == AMBIENT_, "r2 one ulp above 1.5 must not be LOCAL"
            assert cls_by_sf[4 + 18 + 2] == (LOCAL_, 2), "slave 2 local with r1 one ulp below 1.5"
            assert cls_by_sf[4 + 24 + 2][0] == AMBIENT_, "r1 exactly 1.5 must not be LOCAL"
        seqs.append(entry)
        summary.append((f"rule c r_other edge T={T} raw={ur}", len(ops)))
    crule, cT, cur, ck, ccomm = chosen_from_json()
    for j in range(2):
        ops = make_ops(rng, 24, 200 if j == 0 else 140, cT, ccomm, restart=True, gap=True)
        seqs.append(pack_seq(crule, cT, cur, ccomm, 64, ops, ck))
        summary.append((f"chosen long {j}", len(ops)))
    sel = struct.pack("<BBBBfIIf", ord(crule), int(cur), ck, ccomm, float(F32(cT)), 64, 32, float(F32(fe.OTHER_MAX)))
    blob = b"FUSV" + struct.pack("<II", 3, len(seqs)) + sel + b"".join(s[0] for s in seqs)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(blob)
    tot = np.zeros(5, dtype=int)
    nev = 0
    for _, r, cc in seqs:
        tot += cc
        nev += len(r.records)
    print(f"wrote {OUT} ({len(blob)} bytes, {len(seqs)} sequences); decisions by class {dict(zip(fe.CLS_NAMES, tot.tolist()))}; event/status records {nev}")
    print("final selected config (fusion_eval.json):", crule, cT, cur, "k =", ck)


if __name__ == "__main__":
    main()
