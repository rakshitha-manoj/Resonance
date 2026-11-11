"""R3: fair comparison of ONE standalone node against the 3-ESP32 hierarchy (master + 2 slaves).

Data: the R2b TEST block only (240 buildings, seeds 6,200,000+; healthy monitoring + the same event recordings). Scoring only, nothing is
selected here. Primary pair floors 5/10 (also 3/7 for the Python-side tables).

Configurations (pair floors lo/hi = 5/10; for 3/7: lo = 3, hi = 7):
  S1  one node on the hi floor, old tree + noise gate + 2-of-3 smoothing; alarm = smoothed state non-normal (any class)
  S2  one node on the lo floor, same
  S3  one node on the hi floor, ratio rule r = az_std / own commissioning median >= 3.0, 2-of-3 smoothed (k = 1); alarm "event", no type
  H   the hierarchy, final config (rule b, T = 3.0, k = 1): both slaves elevated = BUILDING, one = LOCAL(slave)
Metrics: false onsets per hour (healthy + noise-only monitoring), event detection by ANY alarm (per kind and level), correct event type
(H only; single nodes score 0 by construction), behaviour with a dead node, data sent, effect of i.i.d. packet loss on H (needs the
netsim executable: raw windows of a subset of the test buildings are regenerated with the same seeds, written as netsim trace files,
and the C++ simulator + fusion.h run on them). Bootstrap 95% intervals by building (2000 resamples, seed 42).

Usage:  python models/training/compare_hierarchy.py [--small] [--netsim PATH] [--jobs N] [--no-net] [--cache DIR]
  --small  12 buildings (R2b small test block, seeds 6,950,000+), 200 resamples, output to .cache/compare_hierarchy/small_out (determinism demo).
  netsim:  default firmware/esp32/.pio/build/netsim/program.exe (build it first: cd firmware/esp32 && pio run -e netsim; `pio test -e netsim`
           overwrites that file with the test runner, so build again after testing). Missing => a clear error before any work is done;
           --no-net skips the packet-loss/netsim section.
  --cache DIR  the R2b simulation cache that is reused (default: that of fusion_eval_r2b.py, .cache/fusion_eval_r2b/cache or
           $RESONANCE_CACHE_DIR/fusion_eval_r2b/cache); a missing chunk is simulated (about 25 s per 20 buildings).
The script's own files (raw windows, traces, JSONL, --small output) go under .cache/compare_hierarchy/ ($RESONANCE_CACHE_DIR/compare_hierarchy/).
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import fusion_eval as fe  # noqa: E402
import fusion_eval_r2b as r2b  # noqa: E402
from fusion_eval import bs, lg, N, PAIRS, SF_SECONDS  # noqa: E402

ROOT = fe.ROOT
OUT_DIR = fe.OUT_DIR
import cachedir  # noqa: E402

R3 = cachedir.script_dir("compare_hierarchy")   # regenerated raw windows, traces, JSONL, --small output (cachedir.py)
DEFAULT_EXE = ROOT / "firmware" / "esp32" / ".pio" / "build" / "netsim" / "program.exe"
N_BOOT = 2000
COMM = 20
T_RATIO = 3.0
LOSSES = (0.0, 0.05, 0.10, 0.20)
LOSS_SEEDS = {0.0: 1, 0.05: 3, 0.10: 3, 0.20: 3}
N_NET_BUILDINGS = 240
DEAD_RUN_SF = 20000
DEAD_RUN_SEEDS = 10
FLOORS_NET = (5, 10)
RAW_BYTES_PER_S_6AX = 6 * 2 * 100
RAW_BYTES_PER_S_2AX = 2 * 2 * 100
SYS_NAMES = {"S1": "S1 single node, hi floor, old tree + gate + smoothing", "S2": "S2 single node, lo floor, old tree + gate + smoothing",
             "S3": "S3 single node, hi floor, ratio rule r>=3.0, k=1", "H": "H hierarchy (rule b, T=3.0, k=1)",
             "S3lo": "S3lo single node, lo floor, ratio rule r>=3.0, k=1 (extra, for the dead-slave comparison)",
             "Hc_posthoc": "Hc POST-HOC context only: hierarchy rule c, T=3.0, k=1 (R2b runner-up; NOT a selection)"}
KINDS = r2b.KINDS
LOCAL_KINDS, BUILD_KINDS = r2b.LOCAL_KINDS, r2b.BUILD_KINDS
ETYPES = r2b.ETYPES


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ====================================================================================================================
# Python side: alarm / code arrays per system, per-building statistics
# ====================================================================================================================
def rule_b():
    return next(c for c in fe.configs() if c["rule"] == "b" and c["T"] == T_RATIO)


class Sub:
    """First n buildings of a PairView (masks and names only)."""

    def __init__(self, v, n):
        self.v, self.n = v, n

    def event_rec(self, et):
        nm, tru, mask = self.v.event_rec(et)
        return nm, tru, None if mask is None else mask[:self.n]


def stats_core(alarm_of, code_of, v):
    """alarm_of(nm) -> bool (nb, nw); code_of(nm) -> int codes (0 none, 1/2 LOCAL(slave), 3 BUILDING) or None. Per-building arrays."""
    st = {}
    for key in ("hl", "nz"):
        a = alarm_of(key)
        prev = np.concatenate([np.zeros((a.shape[0], 1), dtype=bool), a[:, :-1]], axis=1)
        st[key + "_begins"] = (a & ~prev).sum(1).astype(float)
        st[key + "_n"] = np.full(a.shape[0], a.shape[1], dtype=float)
    for et in ETYPES:
        nm, tru, mask = v.event_rec(et)
        a = alarm_of(nm)
        nb, nw = a.shape
        w = np.arange(nw)[None, :]
        m = np.ones((nb, nw), dtype=bool) if mask is None else mask
        valid = m.any(1)
        s0 = np.argmax(m, axis=1)
        last = nw - 1 - np.argmax(m[:, ::-1], axis=1)
        span = (w >= s0[:, None]) & (w <= last[:, None] + 3)
        st[et + "_valid"] = valid.astype(float)
        st[et + "_any"] = (a.any(1) & valid).astype(float)
        st[et + "_any_span"] = ((a & span).any(1) & valid).astype(float)
        cd = code_of(nm)
        if cd is None:
            st[et + "_typed"] = np.zeros(nb)
            st[et + "_typed_span"] = np.zeros(nb)
            st[et + "_wrong"] = np.zeros(nb)
        else:
            ok = cd == tru
            st[et + "_typed"] = (ok.any(1) & valid).astype(float)
            st[et + "_typed_span"] = ((ok & span).any(1) & valid).astype(float)
            st[et + "_wrong"] = (((cd > 0) & (cd != tru)).any(1) & valid).astype(float)
    return st


def sum_stats(sts):
    return {k: sum(s[k] for s in sts) for k in sts[0]}


def _ratio(a, b):
    return np.divide(a, b, out=np.full(np.shape(a), np.nan), where=np.asarray(b) > 0)


def summarize(st, W=None):
    A = (lambda k: st[k].sum(0)) if W is None else (lambda k: np.tensordot(W, st[k], axes=(1, 0)))
    m = {}
    n = A("hl_n") + A("nz_n")
    m["false_onsets_per_hour"] = (A("hl_begins") + A("nz_begins")) / (n * SF_SECONDS / 3600.0)
    m["false_onsets_per_hour_healthy"] = A("hl_begins") / (A("hl_n") * SF_SECONDS / 3600.0)
    m["false_onsets_per_hour_noise"] = A("nz_begins") / (A("nz_n") * SF_SECONDS / 3600.0)
    for which in ("any", "typed", "any_span", "typed_span"):
        for kind, ets in KINDS.items():
            m[f"{which}_{kind}"] = _ratio(sum(A(e + "_" + which) for e in ets), sum(A(e + "_valid") for e in ets))
        m[f"{which}_local"] = sum(m[f"{which}_{k}"] for k in LOCAL_KINDS) / 3
        m[f"{which}_building"] = sum(m[f"{which}_{k}"] for k in BUILD_KINDS) / 3
        m[f"{which}_mean"] = (m[f"{which}_local"] + m[f"{which}_building"]) / 2
        # local events split by the floor they happen on: s1 = lo floor, s2 = hi floor
        for sl, lab in (("s1", "lofloor"), ("s2", "hifloor")):
            ets = [e for e in ETYPES[:6] if e.endswith(sl)]
            m[f"{which}_local_{lab}"] = _ratio(sum(A(e + "_" + which) for e in ets), sum(A(e + "_valid") for e in ets))
            for kind in LOCAL_KINDS:
                e = f"{kind}_{sl}"
                m[f"{which}_{kind}_{lab}"] = _ratio(A(e + "_" + which), A(e + "_valid"))
    for kind, ets in KINDS.items():
        m[f"wrong_{kind}"] = _ratio(sum(A(e + "_wrong") for e in ets), sum(A(e + "_valid") for e in ets))
    m["wrong_local"] = sum(m[f"wrong_{k}"] for k in LOCAL_KINDS) / 3
    m["wrong_building"] = sum(m[f"wrong_{k}"] for k in BUILD_KINDS) / 3
    m["hours"] = n * SF_SECONDS / 3600.0 if W is None else None
    return m


def with_ci(st, W):
    pt, bt = summarize(st), summarize(st, W)
    out = {}
    for k in pt:
        if k == "hours":
            out[k] = float(pt[k])
        else:
            out[k] = {"value": float(pt[k]), "ci95": fe.ci(bt[k])}
    return out


def system_stats(v):
    """Per-building statistics of S1, S2, S3, H and the dead-slave H variants on a PairView."""
    rc = rule_b()
    res = {}
    cache = {}

    def tree(nm, ch):
        return v.rec[nm]["state"][:, ch] != 0

    def ratio(nm, ch):
        return v.e(nm, T_RATIO)[:, ch]

    def hcode(nm):
        if nm not in cache:
            cache[nm] = fe.decide_vec(rc, v, nm)
        return cache[nm]

    res["S1"] = stats_core(lambda nm: tree(nm, 1), lambda nm: None, v)
    res["S2"] = stats_core(lambda nm: tree(nm, 0), lambda nm: None, v)
    res["S3"] = stats_core(lambda nm: ratio(nm, 1), lambda nm: None, v)
    res["H"] = stats_core(lambda nm: hcode(nm) > 0, hcode, v)
    res["S3lo"] = stats_core(lambda nm: ratio(nm, 0), lambda nm: None, v)
    rcc = next(c for c in fe.configs() if c["rule"] == "c" and c["T"] == T_RATIO and not c["use_raw"])
    ccache = {}

    def ccode(nm):
        if nm not in ccache:
            ccache[nm] = fe.decide_vec(rcc, v, nm)
        return ccache[nm]
    res["Hc_posthoc"] = stats_core(lambda nm: ccode(nm) > 0, ccode, v)
    for dead, alive in ((1, 1), (2, 0)):  # slave `dead` is dead; the alive channel index is `alive`
        dc = {}

        def code(nm, alive=alive, dc=dc):
            if nm not in dc:
                dc[nm] = fe.decide_degraded_vec(rc, v, nm, alive)
            return dc[nm]
        res[f"H_slave{dead}_dead"] = stats_core(lambda nm, code=code: code(nm) > 0, code, v)
    return res


# ====================================================================================================================
# raw windows of the first buildings (same code path and seeds as r2b.simulate_chunk) and netsim trace files
# ====================================================================================================================
def simulate_raw(seeds, floors=FLOORS_NET):
    """-> {rec name: (az, gx)} each int16 (nb, 2, nwin, 128) for the given floors. Mirrors r2b.simulate_chunk.record exactly."""
    ps = [bs.building_params(s) for s in seeds]
    nb = len(ps)
    sel = [f - 1 for f in floors]
    out = {}

    def record(name, exc, nwin, amp=1.0, zero=False, mult=None, kind=None, fl=None, batch=20):
        sig = np.zeros((nb, bs.NFLOOR, nwin * N)) if zero else bs.simulate_many(ps, exc, nwin, None, "default", amp, batch=batch)
        if mult is not None:
            sig = sig * mult[:, None, None]
        az_o = np.empty((nb, 2, nwin, N), dtype=np.int16)
        gx_o = np.empty((nb, 2, nwin, N), dtype=np.int16)
        for i, p in enumerate(ps):
            kw = {}
            s_i = sig[i]
            if kind == "imp":
                kw["impact_floor"] = int(fl)
            elif kind == "shift":
                kw["shift_floor"] = int(fl)
            elif kind == "sp":
                sp, _ = r2b.sparse_spike(p["seed"], exc, int(fl), nwin * N)
                s_i = s_i.copy()
                s_i[int(fl) - 1] += sp
            az, gx, _ = bs.sensor_output(p, s_i, exc, return_truth=True, **kw)
            az_o[i] = bs.to_windows(az[sel])
            gx_o[i] = bs.to_windows(gx[sel])
        out[name] = (az_o, gx_o)

    record("comm", r2b.EXC["comm"], r2b.COMM_NW)
    mh = np.array([fe.level_mult(p["seed"], r2b.EXC["hl"]) for p in ps])
    record("hl", r2b.EXC["hl"], r2b.HL_NW, mult=mh, batch=10)
    record("nz", r2b.EXC["nz"], r2b.NZ_NW, zero=True)
    for nm, a in r2b.STRONG.items():
        record(nm, r2b.EXC[nm], r2b.NW, amp=a)
    for kind, base_exc in (("imp", 100), ("sp", 400), ("shift", 200)):
        for f in floors:
            exc = base_exc + f
            m = np.array([fe.level_mult(p["seed"], exc) for p in ps])
            record(f"{kind}{f}", exc, r2b.NW, mult=m, kind=kind, fl=f)
    return out


def load_raw(seeds, cache_dir):
    """Chunks of 20 seeds like the R2b cache (identical batching), cached as npz."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i in range(0, len(seeds), r2b.CHUNK):
        sd = seeds[i:i + r2b.CHUNK]
        key = hashlib.sha256((r2b.chunk_key(sd) + "|raw_v1").encode()).hexdigest()[:16]
        f = cache_dir / f"raw_{sd[0]}_{len(sd)}_{key}.npz"
        if f.exists():
            try:
                z = np.load(f)
                parts.append({k: (z[k + "__az"], z[k + "__gx"]) for k in {n.split("__")[0] for n in z.files}})
                log(f"  raw chunk {sd[0]}+{len(sd)} from cache")
                continue
            except Exception:  # noqa: BLE001
                pass
        t = time.time()
        d = simulate_raw(sd)
        flat = {}
        for k, (a, g) in d.items():
            flat[k + "__az"], flat[k + "__gx"] = a, g
        tmp = f.with_suffix(".tmp.npz")
        np.savez(tmp, **flat)
        tmp.replace(f)
        log(f"  raw chunk {sd[0]}+{len(sd)} simulated in {time.time() - t:.0f}s")
        parts.append(d)
    return {k: (np.concatenate([p[k][0] for p in parts]), np.concatenate([p[k][1] for p in parts])) for k in parts[0]}


def check_raw_against_cache(raw, D, floors=FLOORS_NET):
    """The regenerated raw windows must give exactly the az_std stored in the R2b cache."""
    sel = [f - 1 for f in floors]
    n = next(iter(raw.values()))[0].shape[0]
    bad = 0
    tot = 0
    for k, (az, _) in raw.items():
        std = lg.window_features(az)[0].astype(np.float32)
        ref = D[k + "_std"][:n][:, sel]
        tot += 1
        bad += int(not np.array_equal(std, ref))
    return {"recordings_checked": tot, "recordings_not_identical": bad}


def write_trace(path, comm, rec):
    """comm/rec: (az, gx) each (2, nw, 128) int16 for ONE building -> RTRC file (commissioning windows first)."""
    az = np.concatenate([comm[0], rec[0]], axis=1)
    gx = np.concatenate([comm[1], rec[1]], axis=1)
    n = az.shape[1]
    with open(path, "wb") as f:
        f.write(b"RTRC" + np.array([1, 2, n], dtype="<u4").tobytes())
        for i in range(2):
            f.write(np.concatenate([az[i], gx[i]], axis=1).astype("<i2").tobytes())
    return n


# ====================================================================================================================
# netsim runs
# ====================================================================================================================
RE_BYTES = re.compile(r"^(master|slave1|slave2): bytes_tx=(\d+) pkts beacon=(\d+) poll=(\d+) report=(\d+)", re.M)
RE_DEATH = re.compile(r"^death: slave(\d) killed t=(\d+) (?:detected t=(\d+) latency_us=(\d+)|NOT detected)", re.M)
RE_DEADEV = re.compile(r"status slave=(\d) DEAD")


def run_net(exe, trace, loss, seed, jl_path, extra=(), want_log=False):
    cmd = [str(exe), "--traces", str(trace), "--events-jsonl", str(jl_path), "--loss", repr(float(loss)), "--seed", str(int(seed)),
           "--no-samples"] + list(extra)   # events-only log: the data-volume and visibility sections describe the begin/change/status lines (current netsim also writes per-superframe sample lines by default)
    if not want_log:
        cmd.append("--quiet")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 2):
        raise RuntimeError(f"netsim failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[:500]}")
    lines, node_lines = [], []
    try:
        for ln in Path(jl_path).read_text(encoding="utf-8").splitlines():
            o = json.loads(ln)
            # schema-2 `node` lines (immediate slave DEAD/ALIVE, added after this analysis was written) have no class / alive fields;
            # they are kept apart so the event-line statistics below (and the published numbers) are computed on the same lines as before
            (node_lines if o.get("type") == "node" else lines).append(o)
    finally:
        try:
            Path(jl_path).unlink()
        except OSError:
            pass
    by = {m.group(1): (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))) for m in RE_BYTES.finditer(p.stdout)}
    deaths = [(int(m.group(1)), int(m.group(2)), None if m.group(3) is None else int(m.group(3))) for m in RE_DEATH.finditer(p.stdout)]
    nsf = int(re.search(r"^summary: superframes=(\d+)", p.stdout, re.M).group(1))
    return {"events": lines, "node_lines": node_lines, "bytes": by, "deaths": deaths, "nsf": nsf, "false_dead": [int(x) for x in RE_DEADEV.findall(p.stdout)], "rc": p.returncode}


def codes_from_events(events, nsf):
    """Forward-fill the per-superframe output code (0 none/ambient/commissioning/no_data, 1/2 LOCAL(slave), 3 BUILDING)."""
    ev = [(e["sf"], 3 if e["class"] == "BUILDING" else (e["slave"] if e["class"] == "LOCAL" else 0)) for e in events if e["type"] in ("begin", "change")]
    arr = np.zeros(nsf, dtype=np.int8)
    cur, j = 0, 0
    for sf in range(nsf):
        while j < len(ev) and ev[j][0] <= sf:
            cur = ev[j][1]
            j += 1
        arr[sf] = cur
    return arr


NET_SCEN = ["hl", "nz", "s2", "s3", "s5", "imp5", "imp10", "sp5", "sp10", "shift5", "shift10"]
NW_OF = {"hl": r2b.HL_NW, "nz": r2b.NZ_NW}


def build_traces(raw, nb, tdir):
    tdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for b in range(nb):
        comm = (raw["comm"][0][b], raw["comm"][1][b])
        for nm in NET_SCEN:
            p = tdir / f"b{b:03d}_{nm}.bin"
            write_trace(p, comm, (raw[nm][0][b], raw[nm][1][b]))
            paths[(b, nm)] = p
    return paths


def net_jobs(exe, paths, jobs, loss, seed_of, tag, extra=(), want_log=False, jl_dir=None, names=None):
    """Run the (building, scenario) traces in parallel -> {(b, nm): result}. Deterministic: seed derived from (b, scenario index, seed idx)."""
    keys = [k for k in sorted(paths) if names is None or k[1] in names]

    def one(k):
        b, nm = k
        return k, run_net(exe, paths[k], loss, seed_of(b, NET_SCEN.index(nm)), jl_dir / f"{tag}_{b}_{nm}.jsonl", extra, want_log)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        return dict(ex.map(one, keys))


def outs_from_runs(res, nb):
    """-> {rec name: int code array (nb, nw)} from net_jobs results (windows after the commissioning stretch)."""
    outs = {}
    for nm in NET_SCEN:
        nw = NW_OF.get(nm, r2b.NW)
        a = np.zeros((nb, nw), dtype=np.int8)
        for b in range(nb):
            r = res[(b, nm)]
            cd = codes_from_events(r["events"], r["nsf"])
            a[b] = cd[COMM:COMM + nw]
        outs[nm] = a
    return outs


def stats_from_outs(outs, sub):
    return stats_core(lambda nm: outs[nm] > 0, lambda nm: outs[nm], sub)


def poisson_ci(k, alpha=0.05):
    from scipy.stats import chi2
    lo = 0.0 if k == 0 else chi2.ppf(alpha / 2, 2 * k) / 2
    hi = chi2.ppf(1 - alpha / 2, 2 * (k + 1)) / 2
    return float(lo), float(hi)


def net_section(v_primary, D, seeds_all, nb, jobs, exe, raw_dir, W_net, small):
    sub = Sub(v_primary, nb)
    seeds = seeds_all[:nb]
    raw = load_raw(seeds, raw_dir)
    chk = check_raw_against_cache(raw, D)
    log(f"  raw regeneration vs R2b cache: {chk}")
    assert chk["recordings_not_identical"] == 0, "regenerated windows differ from the R2b cache"
    tdir = R3 / ("traces_small" if small else "traces")
    paths = build_traces(raw, nb, tdir)
    jl_dir = R3 / ("jl_small" if small else "jl")
    jl_dir.mkdir(parents=True, exist_ok=True)
    out = {"n_buildings": nb, "seeds": [seeds[0], seeds[-1]], "raw_check": chk, "loss_levels": {}}

    # ---- packet loss sweep
    py_codes = {nm: fe.decide_vec(rule_b(), v_primary, nm)[:nb] for nm in NET_SCEN}
    for loss in LOSSES:
        t = time.time()
        sts, byte_tot, sf_tot = [], np.zeros(3), 0
        ls = {"lines": 0, "status": 0, "begin_change": 0, "bytes": 0, "hours": 0.0}
        false_dead = 0
        mismatch = None
        for si in range(LOSS_SEEDS[loss]):
            res = net_jobs(exe, paths, jobs, loss, lambda b, s, si=si: 1 + 1000 * si + 17 * b + s, f"L{loss}_{si}", jl_dir=jl_dir, want_log=True)
            outs = outs_from_runs(res, nb)
            sts.append(stats_from_outs(outs, sub))
            if loss == 0.0:
                mismatch = {nm: int((outs[nm] != py_codes[nm]).sum()) for nm in NET_SCEN}
                mismatch["total_superframes"] = int(sum(py_codes[nm].size for nm in NET_SCEN))
            for (b, nm), r in sorted(res.items()):
                false_dead += len(r["false_dead"])
                byte_tot += np.array([r["bytes"][k][0] for k in ("master", "slave1", "slave2")], dtype=float)
                sf_tot += r["nsf"]
                if nm in ("hl", "nz"):
                    evs = [e for e in r["events"] if e["type"] != "meta" and e["sf"] >= COMM + 2]
                    ls["lines"] += len(evs)
                    ls["status"] += sum(e["type"] == "status" for e in evs)
                    ls["begin_change"] += sum(e["type"] != "status" for e in evs)
                    ls["bytes"] += sum(len(json.dumps(e, separators=(",", ":"))) + 1 for e in evs)
                    ls["hours"] += (r["nsf"] - COMM - 2) * SF_SECONDS / 3600.0
        st = sum_stats(sts)
        hours_all = sf_tot * SF_SECONDS / 3600.0
        entry = {"seeds": LOSS_SEEDS[loss], "metrics": with_ci(st, W_net),
                 "bytes_per_hour": {"master": float(byte_tot[0] / hours_all), "slave1": float(byte_tot[1] / hours_all), "slave2": float(byte_tot[2] / hours_all)},
                 "monitoring_hours_simulated": float(hours_all),
                 "event_lines_per_hour_healthy": {"total": ls["lines"] / ls["hours"], "status": ls["status"] / ls["hours"], "begin_change": ls["begin_change"] / ls["hours"],
                                                  "bytes_per_hour": ls["bytes"] / ls["hours"], "hours": ls["hours"]},
                 "dead_events_in_sweep_runs": false_dead}
        if mismatch is not None:
            entry["parity_vs_python_reference"] = mismatch
        out["loss_levels"][str(loss)] = entry
        log(f"  loss {loss}: {time.time() - t:.0f}s, false onsets/h {entry['metrics']['false_onsets_per_hour']['value']:.2f}")

    # ---- dead slave runs (loss 0): kill at the first event window
    out["dead_slave_netsim"] = {}
    for dead in (1, 2):
        res = net_jobs(exe, paths, jobs, 0.0, lambda b, s: 7 + 17 * b + s, f"dead{dead}", extra=("--kill", f"{dead}:{COMM}"), jl_dir=jl_dir)
        outs = outs_from_runs(res, nb)
        out["dead_slave_netsim"][f"slave{dead}_dead"] = with_ci(stats_from_outs(outs, sub), W_net)
    # ---- fault visibility: kill in a long healthy trace
    vis = {}
    kill_sf = 40
    for dead in (1, 2):
        res = net_jobs(exe, paths, jobs, 0.0, lambda b, s: 31 + 17 * b, f"vis{dead}", extra=("--kill", f"{dead}:{kill_sf}"), jl_dir=jl_dir, names=("hl",))
        lat_dead, first_deg, first_alive = [], [], []
        other = 3 - dead
        for (b, nm), r in sorted(res.items()):
            d = [x for x in r["deaths"] if x[0] == dead]
            if d and d[0][2] is not None:
                lat_dead.append((d[0][2] - d[0][1]) / 1e6)
            evs = [e for e in r["events"] if e["type"] != "meta" and e["sf"] >= kill_sf]
            fd = [e["sf"] - kill_sf for e in evs if e["degraded_alive"] == other]
            fa = [e["sf"] - kill_sf for e in evs if not e["alive"][dead - 1]]
            first_deg.append(fd[0] if fd else None)
            first_alive.append(fa[0] if fa else None)

        def agg(xs):
            xs = [x for x in xs if x is not None]
            return {"seen": len(xs), "mean": float(np.mean(xs)) if xs else None, "max": max(xs) if xs else None}
        vis[f"slave{dead}_killed"] = {"runs": len(res), "death_detected": len(lat_dead),
                                      "death_latency_s_mean": float(np.mean(lat_dead)) if lat_dead else None,
                                      "death_latency_s_max": float(np.max(lat_dead)) if lat_dead else None,
                                      "first_event_line_with_degraded_alive_set_sf_after_kill": agg(first_deg),
                                      "first_line_with_alive_flag_false_sf_after_kill": agg(first_alive)}
    out["fault_visibility"] = vis

    # ---- false DEAD rate: long healthy runs
    long_trace = paths[(0, "hl")]
    fdr = {}
    for loss in LOSSES:
        n_runs = 1 if loss == 0 else DEAD_RUN_SEEDS

        def one(si, loss=loss):
            r = run_net(exe, long_trace, loss, 5000 + si, jl_dir / f"dead_{loss}_{si}.jsonl", extra=("--superframes", str(DEAD_RUN_SF)), want_log=True)
            return len(r["false_dead"])
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            ev = list(ex.map(one, range(n_runs)))
        slave_hours = n_runs * DEAD_RUN_SF * SF_SECONDS / 3600.0 * 2
        k = int(sum(ev))
        lo, hi = poisson_ci(k)
        fdr[str(loss)] = {"false_dead_events": k, "slave_hours": slave_hours, "per_slave_hour": k / slave_hours, "ci95": [lo / slave_hours, hi / slave_hours], "runs": n_runs}
    out["false_dead_rate"] = fdr
    return out


# ====================================================================================================================
def run(small=False, exe=None, jobs=4, no_net=False, out_dir=None, cache_dir=None, write=True):
    t0 = time.time()
    cache_dir = cache_dir or r2b.SCRATCH / "cache"
    exe = Path(exe or DEFAULT_EXE)
    if not no_net and not exe.exists():   # fail before the long part, with the fix
        raise SystemExit(f"netsim executable not found: {exe}\nBuild it first: cd firmware/esp32 && pio run -e netsim   (or pass --netsim PATH, or --no-net to skip the packet-loss section).")
    r2b.assert_seed_blocks_disjoint()
    tn = "small_test" if small else "test"
    B = 200 if small else N_BOOT
    seeds = r2b.seeds_of(tn)
    nb_net = min(12 if small else N_NET_BUILDINGS, len(seeds))
    log(f"== R3 compare_hierarchy small={small}: test block {r2b.BLOCKS2[tn]}, netsim buildings {nb_net}, B={B}")
    D = r2b.load_block(seeds, cache_dir, tn)
    R, base = r2b.derive(D)
    res = {"task": "R3 single node vs 3-node hierarchy", "small_mode": small, "test_block": {"start": r2b.BLOCKS2[tn][0], "n": r2b.BLOCKS2[tn][1]},
           "bootstrap": {"resamples": B, "unit": "building", "seed": fe.SEED}, "configs": SYS_NAMES, "pairs": {}}
    vp = None
    for pn, (fa, fb) in PAIRS.items():
        v = r2b.PV(R, base, fa, fb)
        nb = v.base.shape[0]
        W = fe.boot_weights(nb, B)
        sts = system_stats(v)
        entry = {"floors": [fa, fb], "n_buildings": nb, "single_node_floors": {"S1": fb, "S2": fa, "S3": fb},
                 "systems": {k: with_ci(s, W) for k, s in sts.items()}}
        res["pairs"][pn] = entry
        log(f"  {pn}: " + ", ".join(f"{k} fo/h {entry['systems'][k]['false_onsets_per_hour']['value']:.2f}" for k in ("S1", "S2", "S3", "H")))
        if pn == "primary":
            vp = v
    res["data_sent"] = {"raw_streaming_bytes_per_s_per_node": {"6_axes": RAW_BYTES_PER_S_6AX, "2_axes_used_by_model": RAW_BYTES_PER_S_2AX},
                        "raw_streaming_bytes_per_hour_per_node": {"6_axes": RAW_BYTES_PER_S_6AX * 3600, "2_axes_used_by_model": RAW_BYTES_PER_S_2AX * 3600},
                        "note": "netsim bytes are application payload (beacon 17 B, poll 18 B, report 44 B incl. CRC), not 802.11/ESP-NOW framing."}
    if not no_net:
        W_net = fe.boot_weights(nb_net, B)
        res["netsim"] = net_section(vp, D, seeds, nb_net, jobs, exe, R3 / ("raw_small" if small else "raw"), W_net, small)
        res["netsim"]["executable"] = exe.name
        res["netsim"]["executable_sha256_16"] = hashlib.sha256(exe.read_bytes()).hexdigest()[:16]
    if write:
        od = out_dir or OUT_DIR
        od.mkdir(parents=True, exist_ok=True)
        fe.jdump(res, od / "compare_hierarchy.json")
        (od / "compare_hierarchy.md").write_text(render_md(res), encoding="utf-8")
    log(f"== done in {time.time() - t0:.0f}s")
    return res


# ====================================================================================================================
# write-up
# ====================================================================================================================
def c(d, nd=3):
    if d is None:
        return "n/a"
    v, ci = d["value"], d["ci95"]
    if v != v:
        return "n/a"
    return f"{v:.{nd}f} [{ci[0]:.{nd}f}, {ci[1]:.{nd}f}]" if ci[0] is not None else f"{v:.{nd}f}"


def v_(d):
    return d["value"]


SYS_ORDER = ["S1", "S2", "S3", "H"]
SYS_SHORT = {"S1": "S1 (tree, hi floor)", "S2": "S2 (tree, lo floor)", "S3": "S3 (ratio, hi floor)", "H": "H (hierarchy)"}


def _overlap(a, b):
    return not (a["ci95"][1] < b["ci95"][0] or b["ci95"][1] < a["ci95"][0])


def summary_text(R):
    P = R["pairs"]["primary"]
    lo, hi = P["floors"]
    S1, S2, S3, H = (P["systems"][k] for k in SYS_ORDER)
    fo = {k: v_(P["systems"][k]["false_onsets_per_hour_healthy"]) for k in SYS_ORDER}
    D1, D2 = P["systems"]["H_slave1_dead"], P["systems"]["H_slave2_dead"]
    L = []
    L.append(f"**What was compared.** The same R2b test recordings (simulated buildings, {H['hours']:.0f} h of healthy monitoring plus nine event kinds) scored for: one node at floor {hi} (S1 old tree, S3 ratio rule), "
             f"one node at floor {lo} (S2 old tree), and the 3-ESP32 hierarchy H (slaves on floors {lo} and {hi}). Simulation only; every system sees the same sensor signals. Intervals are in the tables below.")
    L.append("")
    same = _overlap(S3["false_onsets_per_hour"], H["false_onsets_per_hour"])
    L.append(f"**False alarms (onsets per hour of healthy ambient monitoring; the combined rate with noise-only hours is in the tables).** S1 {fo['S1']:.1f}, S2 {fo['S2']:.1f}, S3 {fo['S3']:.2f}, H {fo['H']:.2f}. The old tree used alone is far noisier than the ratio rule. "
             + (f"S3 and H are statistically indistinguishable ({fo['S3']:.2f} vs {fo['H']:.2f}, intervals overlap): the hierarchy does NOT reduce false alarms relative to one ratio node, and adds none measurable."
                if same else f"S3 and H differ ({fo['S3']:.2f} vs {fo['H']:.2f})."))
    L.append("")
    L.append(f"**Detecting that something happened (any alarm counts).** Mean of LOCAL and BUILDING event detection: S1 {v_(S1['any_mean']):.3f}, S2 {v_(S2['any_mean']):.3f}, S3 {v_(S3['any_mean']):.3f}, H {v_(H['any_mean']):.3f}. "
             f"S1 detects the most events of every kind, but at {fo['S1'] / max(fo['H'], 1e-9):.0f} times H's false-alarm rate, so the two are not comparable at one operating point. "
             f"Against S3, which has the same false-alarm rate as H: building-wide events S3 {v_(S3['any_building']):.3f} vs H {v_(H['any_building']):.3f}; events on S3's own floor ({hi}) S3 {v_(S3['any_local_hifloor']):.3f} vs H {v_(H['any_local_hifloor']):.3f}; "
             f"events on the other floor ({lo}) S3 {v_(S3['any_local_lofloor']):.3f} vs H {v_(H['any_local_lofloor']):.3f}. "
             "So for building-wide events and for events on its own floor, one ratio node is just as good as the hierarchy; the hierarchy's extra detection is the second floor.")
    L.append("")
    typed = f"LOCAL {v_(H['typed_local']):.3f}, BUILDING {v_(H['typed_building']):.3f}"
    fv = (R.get("netsim") or {}).get("fault_visibility", {}).get("slave1_killed", {}).get("death_latency_s_mean")
    fvs = f"the master declares it DEAD after about {fv:.1f} s in the simulator" if fv else "the master declares it DEAD"
    L.append(f"**What the hierarchy adds.** (1) Event type and floor: H says LOCAL(slave) or BUILDING (correct-type recall {typed}); a single node cannot, so single nodes score 0 on correct type BY CONSTRUCTION. "
             f"(2) Two-floor coverage (previous paragraph). (3) Dead-node handling: when a slave dies {fvs}, H keeps working in DEGRADED mode on the other slave "
             f"(any-alarm detection of LOCAL events {v_(D1['any_local']):.3f} / {v_(D2['any_local']):.3f} with slave 1 / slave 2 dead, building-wide events still alarm as LOCAL(alive) {v_(D1['any_building']):.3f} / {v_(D2['any_building']):.3f}, but never typed BUILDING); "
             "a single node that dies is blind and silent unless an external watchdog exists. The master writes a dedicated `node` DEAD/ALIVE line to the JSONL log when it declares a slave dead or alive again (see the visibility table).")
    L.append("")
    nl = (R.get("netsim") or {}).get("loss_levels")
    if nl:
        a, b20 = nl["0.0"]["metrics"], nl["0.2"]["metrics"]
        fd = R["netsim"]["false_dead_rate"]
        net = (f" Packet loss: with 20% i.i.d. loss BUILDING correct-type recall falls {v_(a['typed_building']):.3f} to {v_(b20['typed_building']):.3f} and LOCAL {v_(a['typed_local']):.3f} to {v_(b20['typed_local']):.3f}; "
               f"false DEAD declarations of healthy slaves are {fd['0.1']['per_slave_hour']:.2f} per slave-hour at 10% and {fd['0.2']['per_slave_hour']:.1f} at 20%.")
    else:
        net = ""
    L.append("**What it does not add.** Sensitivity to weak or rare events: sparse single impacts and 2x building-wide events are missed by the ratio rule whether it runs in one node or in H, and S3 and H have the same false-alarm rate. "
             "H needs three boards, a working radio link and about 0.36 MB/h of radio traffic; one node needs none of that." + net
             + " Where only 'did something happen on my floor or building-wide' matters, a single ratio node (S3) is as good and simpler.")
    return "\n".join(L)


def render_md(R):
    md = ["# R3: one standalone node vs the 3-ESP32 hierarchy", ""]
    md += ["## Plain-language summary", "", summary_text(R), ""]
    netline = (f"- The packet-loss, data-volume and fault-visibility sections use the first {R['netsim']['n_buildings']} of the test buildings (seeds {R['netsim']['seeds'][0]}-{R['netsim']['seeds'][1]}; raw windows regenerated and verified identical to the R2b cache) "
               "because netsim needs raw windows; all Python-side tables use every building of the test block." if "netsim" in R else "- netsim section not run.")
    md += ["## Limits (read before using any number)", "",
           "- Simulation only (`building_sim`): uniform shear building, two instrumented floors, stationary band-limited excitation, i.i.d. packet loss (real loss is bursty), no real hardware, no ESP-NOW airtime or collisions.",
           "- R2b TEST block only (seeds 6,200,000+), scoring only; nothing was selected on it. H (rule b, T=3.0, k=1) was fixed in R2b on the development block. The single-node configurations are the project's existing tree/gate/smoothing and the same ratio threshold 3.0, not tuned here.",
           "- Baselines are medians of 20 quiet commissioning windows; real drift in ambient level moves every ratio rule (S3 and H alike).",
           "- 'Detected' = at least one alarm of the stated kind anywhere in the event recording (30 windows), as in R2b. In-span variants (event windows + 3 superframes) are in the JSON.",
           f"- Bootstrap 95% intervals resample buildings ({R['bootstrap']['resamples']} resamples).",
           netline, ""]
    for pn in ("primary", "secondary"):
        P = R["pairs"][pn]
        S = P["systems"]
        fa, fb = P["floors"]
        md += [f"## Main comparison, {pn} pair (floors {fa}/{fb}), {P['n_buildings']} test buildings, {S['H']['hours']:.1f} h of healthy + noise-only monitoring", "",
               f"Single nodes: S1 and S3 on floor {fb}, S2 on floor {fa}. Hierarchy H: slaves on {fa} and {fb}.", "",
               "### False onsets per hour", "",
               "Main figure = healthy ambient only (noise-only recordings cannot produce onsets, so the combined rate is lower by the hour ratio 240/300).", "",
               "| config | healthy ambient (main) | all monitoring (healthy + noise-only) | noise-only |", "|---|---|---|---|"]
        for k in SYS_ORDER:
            md.append(f"| {SYS_SHORT[k]} | {c(S[k]['false_onsets_per_hour_healthy'], 2)} | {c(S[k]['false_onsets_per_hour'], 2)} | {c(S[k]['false_onsets_per_hour_noise'], 2)} |")
        md += ["", "### Event detection, any alarm counts", "",
               "| config | LOCAL events (mean) | dense impacts | sparse impact | frequency shift | BUILDING events (mean) | 2x | 3x | 5x |", "|---|---|---|---|---|---|---|---|---|"]
        for k in SYS_ORDER:
            m = S[k]
            md.append(f"| {SYS_SHORT[k]} | {c(m['any_local'])} | {c(m['any_dense'])} | {c(m['any_sparse'])} | {c(m['any_shift'])} | {c(m['any_building'])} | {c(m['any_x2'])} | {c(m['any_x3'])} | {c(m['any_x5'])} |")
        md += ["", "### LOCAL events split by the floor the event is on (any alarm)", "",
               f"A single node sees its own floor directly and the other floor only through the building response. Lo floor = {fa}, hi floor = {fb}.", "",
               "| config | events on lo floor | dense | sparse | shift | events on hi floor | dense | sparse | shift |", "|---|---|---|---|---|---|---|---|---|"]
        for k in SYS_ORDER:
            m = S[k]
            md.append(f"| {SYS_SHORT[k]} | {c(m['any_local_lofloor'])} | {c(m['any_dense_lofloor'])} | {c(m['any_sparse_lofloor'])} | {c(m['any_shift_lofloor'])} | "
                      f"{c(m['any_local_hifloor'])} | {c(m['any_dense_hifloor'])} | {c(m['any_sparse_hifloor'])} | {c(m['any_shift_hifloor'])} |")
        md += ["", "### Correct event type (LOCAL with the right slave, or BUILDING, output for at least one superframe)", "",
               "Single nodes cannot tell local from building-wide or name a floor: their 0 is BY CONSTRUCTION, not a measurement.", "",
               "| config | LOCAL (right slave) | dense | sparse | shift | BUILDING | 2x | 3x | 5x |", "|---|---|---|---|---|---|---|---|---|"]
        for k in SYS_ORDER:
            m = S[k]
            if k == "H":
                md.append(f"| {SYS_SHORT[k]} | {c(m['typed_local'])} | {c(m['typed_dense'])} | {c(m['typed_sparse'])} | {c(m['typed_shift'])} | {c(m['typed_building'])} | {c(m['typed_x2'])} | {c(m['typed_x3'])} | {c(m['typed_x5'])} |")
            else:
                md.append(f"| {SYS_SHORT[k]} | 0 (by construction) | 0 | 0 | 0 | 0 (by construction) | 0 | 0 | 0 |")
        md += ["", "### Behaviour with a dead node (lossless network, Python reference)", "",
               "A dead single node is blind: it detects nothing, raises no alarm and, without an external watchdog, nothing tells the user it is dead. "
               "H with one dead slave reports DEGRADED and continues with the other slave (LOCAL(alive) or AMBIENT only; BUILDING cannot be output). "
               "Events on the dead slave's floor reach the alive slave only through the building response.", "",
               "| case | false onsets/h | any alarm, LOCAL events | LOCAL on alive slave's floor | LOCAL on dead slave's floor | any alarm, BUILDING events | correct type: LOCAL on alive floor | correct type: BUILDING |", "|---|---|---|---|---|---|---|---|",
               "| single node dead (S1, S2, S3) | 0 (no data) | 0 | 0 | 0 | 0 | 0 | 0 |"]
        for dead in (1, 2):
            m = S[f"H_slave{dead}_dead"]
            alive_lab, dead_lab = ("hifloor", "lofloor") if dead == 1 else ("lofloor", "hifloor")
            md.append(f"| H, slave {dead} dead (DEGRADED) | {c(m['false_onsets_per_hour'], 2)} | {c(m['any_local'])} | {c(m['any_local_' + alive_lab])} | {c(m['any_local_' + dead_lab])} | "
                      f"{c(m['any_building'])} | {c(m['typed_local_' + alive_lab])} | {c(m['typed_building'])} |")
        md += ["", "### Degraded mode in detail (rule b, T=3, k=1: LOCAL only on the alive slave), compared with a single node on the alive floor", "",
               "False onsets are per hour of healthy ambient monitoring (combined rate in the table above). With one slave BUILDING can never be output, so a building-wide event appears as LOCAL(alive): the last three columns are the share of building-wide events that raise that (wrong-type) LOCAL alarm.", "",
               "| case | false onsets/h | LOCAL recall on alive floor | dense | sparse | shift | building-wide shows as LOCAL(alive): 2x | 3x | 5x |", "|---|---|---|---|---|---|---|---|---|"]
        for dead in (1, 2):
            alive_lab = "hifloor" if dead == 1 else "lofloor"
            m = S[f"H_slave{dead}_dead"]
            md.append(f"| H, slave {dead} dead, alive floor {fb if dead == 1 else fa} | {c(m['false_onsets_per_hour_healthy'], 2)} | {c(m['typed_local_' + alive_lab])} | {c(m['typed_dense_' + alive_lab])} | {c(m['typed_sparse_' + alive_lab])} | {c(m['typed_shift_' + alive_lab])} | {c(m['any_x2'])} | {c(m['any_x3'])} | {c(m['any_x5'])} |")
            singles = ("S1", "S3") if dead == 1 else ("S2", "S3lo")
            for sk in singles:
                m = S[sk]
                md.append(f"| single node {sk} on floor {fb if dead == 1 else fa} (any alarm) | {c(m['false_onsets_per_hour_healthy'], 2)} | {c(m['any_local_' + alive_lab])} | {c(m['any_dense_' + alive_lab])} | {c(m['any_sparse_' + alive_lab])} | {c(m['any_shift_' + alive_lab])} | {c(m['any_x2'])} | {c(m['any_x3'])} | {c(m['any_x5'])} |")
        md += ["", "Reading this: degraded H with rule b is the same detector as the single ratio node on the alive floor (so S3 / S3lo match it), minus the type information that needs two slaves. "
               "What it adds over a lone node is that it was commissioned in a pair and the master KNOWS the other slave is gone.", ""]
        md += ["### POST-HOC context: rule c, T=3, k=1 (the R2b runner-up under the fixed criterion; NOT a selection, the final configuration stays rule b)", "",
               "Rule c: both e = BUILDING; F (tree faulty) and e on one slave with the other's ratio below 1.5 = LOCAL(that slave). Wrong-class = share of events of that kind during which some event class other than the true one was output (BUILDING called LOCAL, LOCAL called BUILDING, or wrong slave).", "",
               "| config | false onsets/h healthy (main) | combined | LOCAL correct-type | BUILDING correct-type | 2x | 3x | 5x | wrong-class 2x | wrong-class 3x | wrong-class 5x | wrong-class LOCAL events |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, lab in (("H", "b (final)"), ("Hc_posthoc", "c (post-hoc context)")):
            m = S[k]
            md.append(f"| rule {lab} | {c(m['false_onsets_per_hour_healthy'], 2)} | {c(m['false_onsets_per_hour'], 2)} | {c(m['typed_local'])} | {c(m['typed_building'])} | {c(m['typed_x2'])} | {c(m['typed_x3'])} | {c(m['typed_x5'])} | {c(m['wrong_x2'])} | {c(m['wrong_x3'])} | {c(m['wrong_x5'])} | {c(m['wrong_local'])} |")
        md.append("")
    ds = R["data_sent"]
    md += ["## Data sent", "", "| channel | per second | per hour |", "|---|---|---|",
           f"| raw streaming, 6 axes x 2 B x 100 Hz, per node | {ds['raw_streaming_bytes_per_s_per_node']['6_axes']} B | {ds['raw_streaming_bytes_per_hour_per_node']['6_axes'] / 1e6:.2f} MB |",
           f"| raw streaming, the 2 axes the model uses, per node | {ds['raw_streaming_bytes_per_s_per_node']['2_axes_used_by_model']} B | {ds['raw_streaming_bytes_per_hour_per_node']['2_axes_used_by_model'] / 1e6:.2f} MB |"]
    if "netsim" in R:
        n = R["netsim"]["loss_levels"]
        l0 = n["0.0"]
        b = l0["bytes_per_hour"]
        e = l0["event_lines_per_hour_healthy"]
        tot = b["master"] + b["slave1"] + b["slave2"]
        md += [f"| hierarchy radio, master (beacons + polls), lossless | {b['master'] / 3600:.1f} B | {b['master'] / 1e3:.0f} kB |",
               f"| hierarchy radio, slave 1 (reports) | {b['slave1'] / 3600:.1f} B | {b['slave1'] / 1e3:.0f} kB |",
               f"| hierarchy radio, slave 2 (reports) | {b['slave2'] / 3600:.1f} B | {b['slave2'] / 1e3:.0f} kB |",
               f"| hierarchy radio, all three nodes | {tot / 3600:.1f} B | {tot / 1e3:.0f} kB |",
               f"| events-only uplink to the cloud (JSONL, healthy monitoring, lossless) | {e['bytes_per_hour'] / 3600:.2f} B | {e['bytes_per_hour'] / 1e3:.1f} kB ({e['total']:.1f} lines/h: {e['status']:.1f} periodic status + {e['begin_change']:.1f} begin/change) |", "",
               f"Hierarchy bytes are measured by netsim on the test traces (application payload only; ESP-NOW/802.11 framing and acknowledgements excluded). Raw streaming of the two slave nodes would be {2 * ds['raw_streaming_bytes_per_hour_per_node']['6_axes'] / 1e6:.2f} MB/h "
               f"(6 axes) or {2 * ds['raw_streaming_bytes_per_hour_per_node']['2_axes_used_by_model'] / 1e6:.2f} MB/h (2 axes), about {2 * ds['raw_streaming_bytes_per_hour_per_node']['2_axes_used_by_model'] / tot:.0f}x the whole hierarchy radio. "
               "A single node that classifies on the device and uplinks only its alarms would send even less than the hierarchy; the hierarchy's radio traffic is the price of the local/building-wide decision.", ""]
        N_ = R["netsim"]
        md += [f"## Network loss effect on H ({N_['n_buildings']} test buildings, primary pair, netsim with fusion.h on traces built from the R2b test recordings)", "",
               "i.i.d. loss per link on every packet (beacon, poll, report). Seeds per level: " + ", ".join(f"{float(k) * 100:.0f}%: {v['seeds']}" for k, v in N_["loss_levels"].items()) + ". Intervals resample buildings. "
               "'Correct type' columns are the fusion event recall of R2b; the last column is any-alarm detection.", "",
               "| loss | false onsets/h (healthy, main / combined) | LOCAL correct-type | dense | sparse | shift | BUILDING correct-type | 2x | 3x | 5x | any alarm (mean of LOCAL, BUILDING) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, v in N_["loss_levels"].items():
            m = v["metrics"]
            md.append(f"| {float(k) * 100:.0f}% | {c(m['false_onsets_per_hour_healthy'], 2)} / {c(m['false_onsets_per_hour'], 2)} | {c(m['typed_local'])} | {c(m['typed_dense'])} | {c(m['typed_sparse'])} | {c(m['typed_shift'])} | "
                      f"{c(m['typed_building'])} | {c(m['typed_x2'])} | {c(m['typed_x3'])} | {c(m['typed_x5'])} | {c(m['any_mean'])} |")
        md += ["", "Hierarchy radio bytes per hour by loss level (retries add polls):", "", "| loss | master | slave 1 | slave 2 |", "|---|---|---|---|"]
        for k, v in N_["loss_levels"].items():
            b = v["bytes_per_hour"]
            md.append(f"| {float(k) * 100:.0f}% | {b['master'] / 1e3:.0f} kB | {b['slave1'] / 1e3:.0f} kB | {b['slave2'] / 1e3:.0f} kB |")
        pr = N_["loss_levels"]["0.0"].get("parity_vs_python_reference")
        if pr:
            md += ["", "Parity at 0% loss, C++ netsim + fusion.h vs the Python reference on the same buildings, differing superframes: " + ", ".join(f"{k} {v}" for k, v in pr.items() if k != "total_superframes")
                   + f" (of {pr['total_superframes']}). The C++ fusion keeps its smoothing history from the commissioning windows, the Python recording starts fresh."]
        md += ["", "Events-only uplink per hour of healthy monitoring, by loss level:", "", "| loss | JSONL lines/h | status | begin/change | bytes/h |", "|---|---|---|---|---|"]
        for k, v in N_["loss_levels"].items():
            e = v["event_lines_per_hour_healthy"]
            md.append(f"| {float(k) * 100:.0f}% | {e['total']:.1f} | {e['status']:.1f} | {e['begin_change']:.1f} | {e['bytes_per_hour']:.0f} |")
        md += ["", "### False DEAD events (healthy slaves declared DEAD)", "",
               f"One healthy trace repeated to {DEAD_RUN_SF} superframes, {DEAD_RUN_SEEDS} seeds per level (1 at 0%); exact Poisson 95% interval. `STRUCTURE.md` states about 0.1 per slave-hour at 10% and about 5 at 20%.", "",
               "| loss | DEAD events | slave-hours | per slave-hour | 95% interval |", "|---|---|---|---|---|"]
        for k, v in N_["false_dead_rate"].items():
            md.append(f"| {float(k) * 100:.0f}% | {v['false_dead_events']} | {v['slave_hours']:.0f} | {v['per_slave_hour']:.3f} | [{v['ci95'][0]:.3f}, {v['ci95'][1]:.3f}] |")
        md += ["", "### Dead slave in the network simulator (slave killed at the first event window, lossless)", "",
               "| case | any alarm, LOCAL events | any alarm, BUILDING events | correct type, LOCAL | correct type, BUILDING |", "|---|---|---|---|---|"]
        for k, m in N_["dead_slave_netsim"].items():
            md.append(f"| {k.replace('_', ' ')} | {c(m['any_local'])} | {c(m['any_building'])} | {c(m['typed_local'])} | {c(m['typed_building'])} |")
        md += ["", "### How a dead slave becomes visible (slave killed at superframe 40 of a healthy trace, lossless)", "",
               "| killed | runs | DEAD declared, s after kill (mean / max) | first event line with `degraded_alive` = other slave, sf after kill (mean / max) | first JSONL line with `alive` false for it, sf after kill (mean / max) |", "|---|---|---|---|---|"]
        for k, v in N_["fault_visibility"].items():
            d1, d2 = v["first_event_line_with_degraded_alive_set_sf_after_kill"], v["first_line_with_alive_flag_false_sf_after_kill"]

            def fm(d, v=v):
                return f"{d['mean']:.1f} / {d['max']} ({d['seen']} of {v['runs']} runs)" if d["mean"] is not None else f"never ({d['seen']} of {v['runs']})"
            dl = f"{v['death_latency_s_mean']:.1f} / {v['death_latency_s_max']:.1f} ({v['death_detected']} of {v['runs']})" if v["death_latency_s_mean"] is not None else f"never ({v['death_detected']} of {v['runs']})"
            md.append(f"| {k} | {v['runs']} | {dl} | {fm(d1)} | {fm(d2)} |")
        md += ["", "The master declares DEAD within a few seconds (latency column) and writes a dedicated `{\"type\":\"node\",...,\"state\":\"DEAD\"}` line to the JSONL log at that moment, and an `ALIVE` line on recovery. "
               "The columns above measure the older indirect signals, which remain: `degraded_alive` in a change event (which also fires for one merely lost report) and `alive: false` on the next event or periodic status line (at most 64 superframes, about 90 s). "
               "A dashboard should use the `node` line for dead-node alerts.", ""]
    md += ["## Assumptions not fixed by the brief", "",
           "- Primary pair: S1 and S3 on the higher floor (10), S2 on the lower (5); secondary 3/7 analogously (7 / 3). A single node runs on the recording of its floor in the same building; events on the other floor reach it through the building response.",
           "- S1/S2 'alarm' = smoothed state non-normal (abnormal or faulty), starting each recording from normal. S3 uses the same 2-of-3 smoothing of (r >= 3.0) as H (k = 1).",
           "- A false onset is a no-alarm to alarm transition in a healthy or noise-only recording (as in R2b). H alarms on a LOCAL or BUILDING output.",
           "- Correct-type detection requires the right class (and slave for LOCAL) for at least one superframe of the recording; any-alarm detection requires any alarm in the recording.",
           "- Netsim traces: 20 commissioning windows (the R2b commissioning recording) followed by one R2b recording per run, fresh fusion state per run. The per-superframe output is forward-filled from the JSONL begin/change events; COMMISSIONING and NO_DATA count as no alarm.",
           "- Loss seeds: 1 at 0% (deterministic), 3 at 5/10/20%; per-building counts pool the seeds. The dead-slave runs kill the node at the first event window, after commissioning.",
           ""]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true")
    ap.add_argument("--netsim", type=Path, default=None)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-net", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=None, help="R2b simulation cache directory to reuse (default: that of fusion_eval_r2b.py)")
    a = ap.parse_args()
    out = a.out or (R3 / "small_out" if a.small else None)
    run(small=a.small, exe=a.netsim, jobs=a.jobs, no_net=a.no_net, out_dir=out, cache_dir=a.cache)


if __name__ == "__main__":
    main()
