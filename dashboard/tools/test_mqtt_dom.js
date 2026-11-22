// Headless test of the dashboard's "MQTT live" source (index.html + vendor/mqtt.min.js) in jsdom, against a real broker.
//   1. loads the page, connects it to the broker (MQTT over WebSocket) for a site id,
//   2. runs tools/sim_publisher.py (a separate process) publishing a bundled JSONL log,
//   3. waits for the master's retained "offline" status, then compares the page's parsed state (events, samples, node lines,
//      restarts, timeline segments) with the state of a second page that REPLAYED the same JSONL file through loadText(),
//      and checks the browser notifications that fired.
// Phase C2b: an OFFLINE section runs first (no network): schema-3 parsing (ae, f1, drop, dmg, damage lines), the DAMAGE banner / system line /
// detector tags / history rendered into the DOM, the embedded copy of demo_events_sim.jsonl, the ?replay= link, schema 1 and 2 logs.
// `--offline` stops after it; `--twin [--twin-speed N]` runs the digital-twin test (tools/twin_server.py, real broker) instead of the live comparison. Usage (jsdom is not a project dependency: install it anywhere, e.g. `npm i jsdom` in a scratch folder):
//   NODE_PATH=<folder>/node_modules node dashboard/tools/test_mqtt_dom.js [--url ws://broker.hivemq.com:8000/mqtt]
//        [--site rsn-822298ab] [--log dashboard/demo_events_faults_sim.jsonl] [--python <python>] [--speed 0]
// Reboot case: --log dashboard/tools/short_reboot.jsonl --speed 2 (a burst can reorder a reboot's restart line across topics, see dashboard/README.md).
// Defaults that work on this laptop: --url ws://broker.hivemq.com:8000/mqtt (the plain WebSocket port; wss :8884 and 1883 are blocked on some networks), --python = the repo's .venv python when it exists (else `python`). The twin run needs netsim built (`pio run -e netsim` in firmware/esp32) and strips TELEGRAM_* from the server's environment.
"use strict";
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const jsdom = require("jsdom");
const { JSDOM, VirtualConsole } = jsdom;

const arg = (n, d) => { const i = process.argv.indexOf("--" + n); return i > 0 ? process.argv[i + 1] : d; };
const ROOT = path.resolve(__dirname, "..", "..");
const URL_ = arg("url", "ws://broker.hivemq.com:8000/mqtt");   // plain WebSocket port: 8884 (wss) is blocked on some networks, this one works here
const SITE = arg("site", "rsn-822298ab");
const LOG = path.resolve(arg("log", path.join(ROOT, "dashboard", "demo_events_sim.jsonl")));
const VENV_PY = [path.join(ROOT, ".venv", "Scripts", "python.exe"), path.join(ROOT, ".venv", "bin", "python")].find((p) => fs.existsSync(p));
const PY = arg("python", VENV_PY || "python");   // the repo's .venv python when present
const SPEED = arg("speed", "0");
const PAGE = path.join(ROOT, "dashboard", "index.html");

function canvasStub(window, reduced) {
  const ctx = new Proxy({}, { get: (t, k) => (k === "measureText" ? () => ({ width: 10 }) : k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } });
  window.HTMLCanvasElement.prototype.getContext = () => ctx;
  window.matchMedia = (q) => ({ matches: !!reduced && /prefers-reduced-motion/.test(q), addEventListener() {}, addListener() {} });
}

// An http:// page (the dashboard as served by tools/serve_dashboard.py) whose files come from dashboard/ (no network needed).
const HTTP_ORIGIN = "http://localhost:8765/";
const MIME = { ".js": "application/javascript", ".html": "text/html", ".json": "application/json", ".jsonl": "text/plain" };
function dashFile(url) {
  if (!url.startsWith(HTTP_ORIGIN)) return null;
  const f = path.join(ROOT, "dashboard", url.slice(HTTP_ORIGIN.length).split("?")[0] || "index.html");
  return fs.existsSync(f) ? f : undefined;
}
function httpResources() {   // jsdom 29+: requestInterceptor; older jsdom: a ResourceLoader subclass
  if (jsdom.requestInterceptor) {
    return { interceptors: [jsdom.requestInterceptor((request) => {
      const f = dashFile(request.url); if (f === null) return undefined;
      return f ? new Response(fs.readFileSync(f), { headers: { "Content-Type": MIME[path.extname(f)] || "text/plain" } }) : new Response("not found", { status: 404 });
    })] };
  }
  class DashLoader extends jsdom.ResourceLoader {
    fetch(url, o) { const f = dashFile(url); return f === null ? super.fetch(url, o) : f ? Promise.resolve(fs.readFileSync(f)) : Promise.reject(new Error("404 " + url)); }
  }
  return new DashLoader();
}

function openPage(opts) {
  const vc = new VirtualConsole();
  const errors = [];
  vc.on("jsdomError", (e) => errors.push(String(e && e.message || e)));
  vc.on("error", (e) => errors.push(String(e)));
  const notes = [];
  return new Promise((resolve, reject) => {
    const http = !!(opts && opts.http);
    const dom = new JSDOM(fs.readFileSync(PAGE, "utf8"), {
      url: (http ? HTTP_ORIGIN : "file:///" + PAGE.replace(/\\/g, "/")) + ((opts && opts.search) || ""), runScripts: "dangerously", resources: http ? httpResources() : "usable", pretendToBeVisual: true, virtualConsole: vc,
      beforeParse(window) {
        canvasStub(window, opts && opts.reduced);
        if (opts && "lan" in opts) {   // stub of GET /lan.json: an object, or null for 404; any other URL fails like a dead network
          window.fetch = async (u) => {
            if (/lan\.json/.test(u)) return opts.lan === null ? { ok: false, status: 404, json: async () => ({}) } : { ok: true, status: 200, json: async () => opts.lan };
            throw new Error("no network in the test");
          };
        }
        window.__MQTT_TIMER = "native";   // jsdom has no Web Workers / blob URLs (see the timerVariant note in index.html)
        window.Notification = function (title, o) { notes.push({ title, body: o && o.body }); };
        window.Notification.permission = "granted";
        window.Notification.requestPermission = () => Promise.resolve("granted");
        if (opts && opts.noMqttLib) window.__noMqtt = true;
      },
    });
    dom.window.addEventListener("load", () => resolve({ dom, window: dom.window, errors, notes }));
    setTimeout(() => reject(new Error("page load timeout")), 20000);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (fn()) return; await sleep(200); }
  throw new Error("timeout waiting for " + what);
}
const snap = (S) => JSON.stringify({ ev: S.ev, sm: S.sm, nl: S.nl, dl: S.dl, meta: S.meta, rs: S.rs.map((r) => ({ t: r.t, epoch: r.epoch, prev: r.prev })), segs: S.segs, floors: S.meta && S.meta.floors, schema: S.meta && S.meta.schema });

const readLog = (n) => fs.readFileSync(path.join(ROOT, "dashboard", n), "utf8");
const qs = (w, id) => w.document.getElementById(id);

async function offlineChecks(check) {
  console.log("--- offline checks (no network) ---");
  const text = readLog("demo_events_sim.jsonl");
  const pgA = await openPage();
  const A = pgA.window.__resonance, w = pgA.window;
  await A.loadText(text, "damage demo", false);
  const S = A.state();
  const beg = S.dl.filter((d) => d.state === "BEGIN");
  check(S.meta.schema === 3 && S.meta.T === 3 && S.meta.T_ae === 6 && S.meta.dmg_delta === 0.02 && S.meta.dmg_p === 32 && S.meta.gate === 92.12 && S.meta.rule === "f", "meta: schema 3, rule f, T 3, T_ae 6, gate 92.12, dmg_delta 0.02, dmg_p 32 parsed");
  check(S.sm.length === 320 && S.dl.length === 1 && beg.length === 1 && S.stats.bad === 0, `demo log: 320 samples, one damage BEGIN line, no malformed lines (damage lines ${S.dl.length}, bad ${S.stats.bad})`);
  check(beg[0].sf > 192 && beg[0].sf < 192 + 80, "DAMAGE BEGIN at sf " + beg[0].sf + " is after the damage onset (sf 192) and within 80 superframes of it");
  check(S.sm.every((m) => (m.sf < beg[0].sf) === (m.dmg === 0)), "sample dmg flag is 0 exactly before the BEGIN superframe and 1 from it on");
  const lastS = S.sm[S.sm.length - 1];
  check(lastS.pd > 0.03 && lastS.pd < 0.05 && lastS.f1a < lastS.f0k1 * 0.97, `end state: pooled drop ${(lastS.pd * 100).toFixed(2)} % (storey-1 loss of 30 % predicts 3.9 %), f1 ${lastS.f1a.toFixed(3)} vs f1_0 ${lastS.f0k1.toFixed(3)} Hz`);
  check(Math.abs(beg[0].f0[0] - lastS.f0k1) < 0.1 && beg[0].f1[0] < beg[0].f0[0], "damage line carries f1 / f1_0 per sensor");
  check(S.sm.slice(0, 190).every((m) => m.dmg === 0) && S.sm.slice(0, 190).every((m) => m.pd === null || m.pd < 0.03), "no damage indication before the onset (max pooled drop before sf 190: " + (Math.max(...S.sm.slice(0, 190).map((m) => m.pd === null ? -1 : m.pd)) * 100).toFixed(2) + " %)");
  // detector tags
  const evs = S.ev.filter((e) => e.type === "begin");
  const dets = evs.map((e) => ({ sf: e.sf, cls: e.cls, slave: e.slave, d: A.detectorOf(e) }));
  const loc2 = dets.find((x) => x.cls === 2 && x.slave === 2), loc1 = dets.find((x) => x.cls === 2 && x.slave === 1);
  check(loc2 && loc2.d.ae && !loc2.d.std, "frequency shift on sensor 2: detector tag AE only (sf " + (loc2 && loc2.sf) + ")");
  check(loc1 && loc1.d.ae, "impact on sensor 1: the AE fires first at its begin (sf " + (loc1 && loc1.sf) + ", std " + (loc1 && loc1.d.std) + ", ae " + (loc1 && loc1.d.ae) + "); the std ratio joins as the burst builds");
  // rendering at the end and before the damage
  A.seek(S.tEnd); A.render();
  const banner = qs(w, "dmgBanner");
  check(!banner.hidden && /d-alarm/.test(banner.className) && /DAMAGE INDICATED/.test(qs(w, "dText").textContent), "end state: DAMAGE banner is red: '" + qs(w, "dText").textContent + "'");
  const sys = qs(w, "sysLine").textContent;
  check(/Rule f/.test(sys) && /T_ae=6/.test(sys) && /92\.12/.test(sys) && /M=32/.test(sys) && /P=32/.test(sys), "system line: " + sys.slice(0, 170));
  check(/DAMAGE begins/.test(qs(w, "dmgHist").textContent), "DAMAGE history lists the BEGIN");
  const hist = qs(w, "hist").innerHTML;
  check(/class="tag ae"/.test(hist) && /class="tag std"/.test(hist), "event history rows show detector tags std and AE");
  check(!qs(w, "aeCard").hidden && !qs(w, "f1Card").hidden && !qs(w, "dropCard").hidden, "AE, f1 and drop panels visible for a schema 3 log");
  A.seek(S.sm[150].t); A.render();
  check(/d-ok/.test(banner.className) && /No damage/.test(qs(w, "dText").textContent), "before the onset (sf 150): banner green: '" + qs(w, "dText").textContent + "'");
  A.seek(S.sm[40].t); A.render();
  check(/Waiting|Commissioning/.test(qs(w, "dText").textContent) && /d-na/.test(banner.className), "at sf 40: f1 not commissioned yet -> grey: '" + qs(w, "dText").textContent + "'");
  const warnAt = S.sm.find((m) => m.run >= 5 && m.dmg === 0);
  if (warnAt) { A.seek(warnAt.t); A.render(); check(/d-warn/.test(banner.className), "approaching the alarm (run " + warnAt.run + " of 32): amber"); }
  // B1: DAMAGE shading / red bar never show the future of the replay
  const upTo = (sf) => { A.seek(S.sm.find((m) => m.sf >= sf).t); return A.dmgIntervals(S.tEnd); };
  check(upTo(150).length === 0 && upTo(234).length === 0, "replay at sf 150 / 234: no DAMAGE interval is drawn yet (charts and timeline use the playhead)");
  const iv240 = upTo(240);
  check(iv240.length === 1 && iv240[0][1] === S.playhead && iv240[0][0] === beg[0].t, "replay at sf 240: the interval runs from the BEGIN to the playhead only");
  A.seek(S.tEnd); check(A.dmgIntervals(S.tEnd).length === 1 && A.dmgIntervals(S.tEnd)[0][1] === S.tEnd, "replay at the end: one interval to the end");
  // wording / labels
  A.seek(S.sm[140].t); A.render();
  check(/Vibration events: only ordinary background vibration \(shaking only; structural damage is judged separately/.test(qs(w, "bSub").textContent), "state banner says it is about shaking only: '" + qs(w, "bSub").textContent.slice(0, 90) + "'");
  check(/Sensor 1<\/b> \(floor 5\)/.test(qs(w, "nodes").innerHTML) && !/floor 5 \(sensor 1\)\)/.test(qs(w, "nodes").innerHTML), "sensor health label is 'Sensor 1 (floor 5)'");
  check(/scripted shaking, so this is not a false-alarm rate/.test(qs(w, "counters").textContent), "events-per-hour label says it is not a false-alarm rate for a simulated log");
  check(/STRUCTURE: DAMAGE INDICATED/.test((A.seek(S.tEnd), A.render(), qs(w, "dText").textContent)), "end banner: 'STRUCTURE: DAMAGE INDICATED'");
  // B5: notifications for damage
  const n0 = pgA.notes.length;
  A.notifyLine(JSON.stringify({ type: "damage", sf: 235, state: "BEGIN", drop: 0.0426, f1: [6.7, 6.7], f1_0: [7, 7] }));
  A.notifyLine(JSON.stringify({ type: "damage", sf: 300, state: "END", drop: 0.004, f1: [7, 7], f1_0: [7, 7] }));
  A.notifyLine(JSON.stringify({ type: "damage", sf: 310, state: "BASELINE", drop: null, f1: [7, null], f1_0: [7, null], slave: 1 }));
  const nn = pgA.notes.slice(n0);
  check(nn.length === 2 && nn[0].title === "DAMAGE ALARM" && nn[0].body === "Natural frequency down 4.3 % (superframe 235)" && nn[1].title === "DAMAGE cleared", "notifications: DAMAGE ALARM / DAMAGE cleared, none for BASELINE: " + JSON.stringify(nn));
  // BASELINE note
  const base = JSON.stringify({ type: "damage", t_us: lastS.t + 1000, sf: lastS.sf + 1, state: "BASELINE", drop: null, f1: [7.0, null], f1_0: [7.0, null], slave: 1 });
  const pgB = await openPage();
  await pgB.window.__resonance.loadText(text.trimEnd() + "\n" + base + "\n", "with baseline", false);
  pgB.window.__resonance.seek(pgB.window.__resonance.state().tEnd); pgB.window.__resonance.render();
  const sb = pgB.window.__resonance.state();
  check(sb.dl.length === 2 && sb.dl[1].state === "BASELINE" && /baseline note: sensor 1/.test(qs(pgB.window, "dmgHist").textContent) && sb.stats.bad === 0, "a BASELINE damage line is parsed and listed as a note");
  // the embedded copy and the Damage-demo button without fetch (jsdom has none, like file:// in Chrome)
  const emb = qs(w, "sim-data").textContent.replace(/^\n/, "");
  check(emb === text, "embedded sim-data equals dashboard/demo_events_sim.jsonl (" + emb.length + " bytes)");
  const pgC = await openPage();
  await pgC.window.__resonance.loadSim({ play: false });
  check(snap(pgC.window.__resonance.state()) === snap(A.state()) && /embedded/.test(pgC.window.__resonance.state().source), "loadSim() without fetch uses the embedded copy: state identical to the replay of the file");
  // ?replay= link
  const pgD = await openPage({ search: "?replay=demo_events_sim.jsonl&speed=20&at=end" });
  await until(() => pgD.window.__resonance.state().sm.length === 320, 10000, "?replay= load");
  await sleep(300);
  const sd = pgD.window.__resonance.state();
  check(sd.mode === "replay" && sd.speed === 20 && sd.playhead === sd.tEnd && /d-alarm/.test(qs(pgD.window, "dmgBanner").className), "?replay=...&speed=20&at=end: loaded, speed 20, paused at the end, banner red");
  const pgE = await openPage({ search: "?replay=demo_events_sim.jsonl&speed=50" });
  await until(() => pgE.window.__resonance.state().playing === true, 10000, "?replay= autoplay");
  check(pgE.window.__resonance.state().speed === 50, "?replay=...&speed=50 autoplays at 50x");
  // B3: only relative .jsonl file names are accepted by ?replay= (no data: URLs, other origins, paths)
  const bad = ["data:text/plain,%7B%22type%22:%22meta%22%7D", "https://example.com/x.jsonl", "../x.jsonl", "//evil.example/x.jsonl", "a/b.jsonl", "x.txt"];
  const badPages = [];
  for (const u of bad) {
    const pg = await openPage({ search: "?replay=" + encodeURIComponent(u) });
    badPages.push(pg);
  }
  await sleep(600);
  check(badPages.every((pg) => pg.window.__resonance.state().sm.length === 0 && /must name a \.jsonl file/.test(qs(pg.window, "msgs").textContent)), "?replay= refuses data:, cross-origin, parent / sub-folder and non-.jsonl names (" + bad.length + " cases)");
  // B4: ?at=abc must not give a NaN playhead
  const pgH = await openPage({ search: "?replay=demo_events_sim.jsonl&at=abc&play=0" });
  await until(() => pgH.window.__resonance.state().sm.length === 320, 10000, "?at=abc load");
  await sleep(300);
  const sh = pgH.window.__resonance.state();
  check(Number.isFinite(sh.playhead) && sh.playhead === sh.tEnd, "?at=abc falls back to the end of the log (playhead " + sh.playhead + ")");
  // schema 2 and schema 1 logs: no damage panels, no errors
  const pgF = await openPage();
  await pgF.window.__resonance.loadText(readLog("sample_events.jsonl"), "schema 2", false);
  pgF.window.__resonance.seek(pgF.window.__resonance.state().tEnd); pgF.window.__resonance.render();
  check(qs(pgF.window, "dmgBanner").hidden && qs(pgF.window, "aeCard").hidden && qs(pgF.window, "f1Card").hidden && pgF.window.__resonance.state().stats.bad === 0, "schema 2 log: damage banner and AE/f1 panels hidden, nothing malformed");
  const s1 = '{"type":"meta","schema":1,"rule":"b","T":3,"use_raw":false,"commission_windows":20,"status_every_sf":64,"superframe_s":1.4}\n' +
    '{"type":"change","t_us":1000000,"sf":0,"class":"COMMISSIONING","slave":0,"degraded_alive":0,"r1":null,"r2":null,"base1":0,"base2":0,"alive":[true,true],"closed":1,"events":1}\n' +
    '{"type":"change","t_us":29000000,"sf":20,"class":"AMBIENT","slave":0,"degraded_alive":0,"r1":1.0,"r2":0.9,"base1":75.2,"base2":102.6,"alive":[true,true],"closed":21,"events":2}\n' +
    '{"type":"begin","t_us":57000000,"sf":40,"class":"LOCAL","slave":1,"degraded_alive":0,"r1":7.4,"r2":1.4,"base1":75.2,"base2":102.6,"alive":[true,true],"closed":41,"events":3}\n';
  const pgG = await openPage();
  await pgG.window.__resonance.loadText(s1, "schema 1", false);
  pgG.window.__resonance.seek(pgG.window.__resonance.state().tEnd); pgG.window.__resonance.render();
  check(qs(pgG.window, "dmgBanner").hidden && pgG.window.__resonance.state().ev.length === 3 && /std/.test(qs(pgG.window, "hist").innerHTML), "schema 1 log still replays (3 events, std detector tag, no damage panels)");
  // all pages: no script errors
  await viewChecks(check, text);
  await twinViewChecks(check, text);
  const errs = [pgA, pgB, pgC, pgD, pgE, pgF, pgG, pgH, ...badPages, ...EXTRA_PAGES].flatMap((p) => p.errors).filter((e) => !/Not implemented/.test(e));
  check(errs.length === 0, "no script errors in any offline page" + (errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""));
  console.log("--- end offline checks ---");
}

// Animated building view, parameter validation, auto-connect link, phone view (QR code).
const EXTRA_PAGES = [];
async function viewChecks(check, text) {
  console.log("--- building view / launch parameters / phone view ---");
  const mk = async (opts) => { const p = await openPage(opts); EXTRA_PAGES.push(p); return p; };
  const doc = (p) => p.window.document;
  const storeys = (p) => Array.from(doc(p).querySelectorAll(".bv-st")).map((e) => e.getAttribute("data-s"));
  const amp = (p) => +doc(p).getElementById("bsvg").getAttribute("data-amp");
  const outDmg = (p) => doc(p).getElementById("bvOut").getAttribute("data-dmg") === "1";
  const shown = (p, id) => !doc(p).getElementById(id).hasAttribute("hidden");   // attribute, not .hidden: SVG elements have no such property
  const pg = await mk();
  const A = pg.window.__resonance;
  await A.loadText(text, "damage demo", false);
  const S = A.state();
  const at = (sf) => { A.seek(S.sm.find((m) => m.sf >= sf).t); A.render(); };
  const atT = (t) => { A.seek(t); A.render(); };

  // layout and labels
  at(150);
  check(storeys(pg).length === 10 && doc(pg).querySelectorAll(".bv-ic").length === 2, "building view: 10 storeys drawn and 2 sensor icons");
  const labs = Array.from(doc(pg).querySelectorAll(".bv-lab .l1")).map((e) => e.textContent);
  check(labs.join("|") === "sensor 1, floor 5|sensor 2, floor 10", "sensor labels from meta.floors: " + labs.join(" | "));
  check(/10-storey/.test(doc(pg).getElementById("bvCard").textContent) && /exaggerated and slowed down, not to scale/.test(doc(pg).getElementById("bvCap").textContent) && /not to scale/.test(doc(pg).getElementById("bsvgT").textContent), "storey count labelled (10) and the 'exaggerated, slowed, not to scale' caption / tooltip are present");
  // geometry: first-mode shape, zero at the ground, proportional to sin(pi z / 2N), sensor icons on storeys 5 and 10
  const pts = (id) => doc(pg).getElementById(id).getAttribute("points").split(" ").map((x) => x.split(",").map(Number));
  const s1 = pts("bvS1"), s10 = pts("bvS10"), ghost = pts("bvGhost");
  const dx = (p, i, gi) => p[i][0] - ghost[gi][0];
  check(Math.abs(dx(s1, 0, 0)) < 1e-6 && Math.abs(dx(s1, 1, 1)) < 1e-6, "ground corners never move (sway 0 at z = 0)");
  const roof = +doc(pg).getElementById("bsvg").getAttribute("data-roof-dx"), top = dx(s10, 2, 1), s5top = pts("bvS5")[2][0] - ghost[1][0];
  check(Math.abs(top - roof) < 0.06 && Math.abs(s5top - roof * Math.sin(Math.PI / 2 * 5 / 10)) < 0.06, `displacement ~ sin(pi z / 2N): roof ${top.toFixed(2)} px, storey 5 top ${s5top.toFixed(2)} px (expected ${(roof * Math.sin(Math.PI / 4)).toFixed(2)})`);
  const icy = (n) => +/translate\([\d.-]+,([\d.-]+)\)/.exec(doc(pg).getElementById("bvIc" + n).getAttribute("transform"))[1];
  check(Math.abs(icy(1) - (340 - 4.5 * 30)) < 0.1 && Math.abs(icy(2) - (340 - 9.5 * 30)) < 0.1, "sensor icons sit in the middle of storeys 5 and 10");

  // amplitude follows the vibration level
  const lvl = (m) => [m.a1 && m.r1 !== null ? m.r1 : null, m.a2 && m.r2 !== null ? m.r2 : null].filter((x) => x !== null);
  const mean = (m) => { const l = lvl(m); return l.reduce((a, b) => a + b, 0) / l.length; };
  const amb = S.sm.filter((m) => m.cls === 1 && m.sf > 140 && m.sf < 160)[0], bld = S.sm.filter((m) => m.cls === 3).sort((a, b) => mean(b) - mean(a))[0];
  atT(amb.t); const aAmb = amp(pg); atT(bld.t); const aBld = amp(pg);
  check(aAmb > 0 && aAmb < 4 && aBld > aAmb * 3 && aBld <= 17.001, `sway amplitude follows r: ambient r ${mean(amb).toFixed(2)} -> ${aAmb} px, building event r ${mean(bld).toFixed(2)} -> ${aBld} px (clamped to 17)`);
  at(5); check(amp(pg) < 1.5, "calibrating: almost no sway (" + amp(pg) + " px)");
  // paused replay is static: no animation frame changes the picture
  const dx0 = doc(pg).getElementById("bsvg").getAttribute("data-roof-dx"); await sleep(250);
  check(doc(pg).getElementById("bsvg").getAttribute("data-roof-dx") === dx0 && A.state().playing === false, "paused replay: building static");

  // states
  const loc1 = S.sm.find((m) => m.cls === 2 && m.slave === 1 && m.a1), loc2 = S.sm.find((m) => m.cls === 2 && m.slave === 2 && m.a2);
  atT(loc1.t); let st = storeys(pg);
  check(st[4] === "local" && st.filter((x) => x === "local").length === 1 && st.filter((x) => x === "ambient").length === 9 && /pulse/.test(doc(pg).getElementById("bvS5").getAttribute("class")) && doc(pg).getElementById("bvIc1").getAttribute("data-s") === "local", "LOCAL on sensor 1: only storey 5 highlighted (pulsing yellow), the rest normal");
  atT(loc2.t); st = storeys(pg);
  check(st[9] === "local" && st.filter((x) => x === "local").length === 1 && doc(pg).getElementById("bvIc2").getAttribute("data-s") === "local", "LOCAL on sensor 2: only storey 10 highlighted");
  atT(bld.t); st = storeys(pg);
  check(st.every((x) => x === "building") && doc(pg).getElementById("bvIc1").getAttribute("data-s") === "building", "BUILDING event: the whole building is amber");
  at(5); st = storeys(pg);
  check(st.every((x) => x === "comm") && shown(pg, "bvCal") && /calibrating/.test(doc(pg).getElementById("bvCal").textContent), "COMMISSIONING: whole building grey with the 'calibrating' label");
  atT(amb.t);   // a quiet moment before the alarm (sf 150 itself is inside a building-wide shake)
  check(!outDmg(pg) && !shown(pg, "bvFlag") && !shown(pg, "bvBand") && storeys(pg).every((x) => x === "ambient"), "before the damage alarm: normal outline, no damage label, no storey mark (" + storeys(pg).join(",") + ", dmg " + outDmg(pg) + ", flag " + shown(pg, "bvFlag") + ")");
  A.seek(S.tEnd); A.render();
  const flag = doc(pg).getElementById("bvFlag").textContent;
  check(outDmg(pg) && /bv-out dmg/.test(doc(pg).getElementById("bvOut").getAttribute("class")) && shown(pg, "bvFlag") && flag === "DAMAGE indicated — f1 tracking cannot locate the storey", "DAMAGE active: red outline + label '" + flag + "'");
  check(!shown(pg, "bvBand") && !shown(pg, "bvTruthG") && !shown(pg, "bvTruthNote") && doc(pg).getElementById("bvBand").getAttribute("data-truth") === "" && !storeys(pg).includes("damage") && storeys(pg).every((x) => x === "ambient"), "outside twin mode NO storey is marked as damaged (no band, no truth label, storey fills unchanged)");
  // twin truth (synthetic twin/state; the page only trusts it on an MQTT site)
  const tw = (d) => ({ running: true, commissioned: true, speed: 2, sf: 300, damage: d, building: { f1_hz: 7.03 }, f1_true_hz: 6.8, drop_true: 0.039, killed: [], truth: [[0, 0, 7.03]], log: [] });
  const setTw = (d) => A.twinFeed(JSON.stringify(Object.assign(tw(d), { updated_t: Date.now() / 1000, period_s: 5 })), false);
  S.kind = "mqtt"; S.mq = { end() {} }; setTw({ storey: 3, loss: 0.3 }); A.render();
  const lbl = doc(pg).getElementById("bvTruthT").textContent;
  check(shown(pg, "bvBand") && doc(pg).getElementById("bvBand").getAttribute("data-truth") === "3" && lbl === "simulator truth: storey 3, −30 % stiffness" && shown(pg, "bvTruthG"), "twin mode: red hatched band on storey 3 labelled '" + lbl + "'");
  const bandPts = pts("bvBand"), s3 = pts("bvS3");
  check(JSON.stringify(bandPts) === JSON.stringify(s3) && !!doc(pg).getElementById("bvHatch") && /cannot locate/.test(doc(pg).getElementById("bvTruthNote").textContent) && /cannot locate/.test(doc(pg).getElementById("bvFlag").textContent), "twin mode: the band covers exactly storey 3 and the 'cannot locate the storey' note is kept for what the sensors know");
  setTw(null); A.render(); check(!shown(pg, "bvBand"), "twin without damage: no band");
  for (const bad of [{ storey: 11, loss: 0.3 }, { storey: 0, loss: 0.3 }, { storey: 2.5, loss: 0.3 }, { storey: 3, loss: 1.5 }, { storey: "3", loss: 0.3 }, { storey: 3, loss: -0.1 }]) { setTw(bad); A.render(); if (shown(pg, "bvBand")) check(false, "twin truth with invalid values " + JSON.stringify(bad) + " must not be drawn"); }
  check(!shown(pg, "bvBand"), "twin truth with invalid values (storey 0 / 2.5 / 11 / '3', loss <= 0 or >= 1) is not drawn");
  setTw({ storey: 3, loss: 0.3 }); S.kind = ""; A.render(); check(!shown(pg, "bvBand"), "a twin/state outside an MQTT source is ignored (no storey mark)");
  S.twin = null; S.kind = ""; S.mq = null;

  // dead sensor (faults log has node lines)
  const pgF = await mk(); const FA = pgF.window.__resonance;
  await FA.loadText(readLog("demo_events_faults_sim.jsonl"), "faults", false);
  const FS_ = FA.state(), dead = FS_.nl.find((n) => !n.alive);
  FA.seek(dead.t + 1); FA.render();
  const ic = doc(pgF).getElementById("bvIc" + dead.slave);
  check(ic.getAttribute("data-s") === "dead" && /offline/.test(doc(pgF).querySelector("#bvLab" + dead.slave + " .l2").textContent) && doc(pgF).getElementById("bvIc" + (3 - dead.slave)).getAttribute("data-s") !== "dead", "DEAD sensor " + dead.slave + ": grey icon with a cross (data-s=dead), label 'offline', the other sensor unaffected");
  check(!!ic.querySelector(".cross"), "dead icon carries the cross element");

  // schema 2 (no damage state) and schema 1 (no floors) logs
  const pg2 = await mk(); await pg2.window.__resonance.loadText(readLog("sample_events.jsonl"), "schema 2", false);
  pg2.window.__resonance.seek(pg2.window.__resonance.state().tEnd); pg2.window.__resonance.render();
  check(!outDmg(pg2) && !shown(pg2, "bvFlag") && storeys(pg2).length === 10 && !shown(pg2, "bvBand") && amp(pg2) >= 0, "schema 2 log: building shows events and sway, never a damage mark");
  const sch1 = '{"type":"meta","schema":1,"rule":"b","T":3,"use_raw":false,"commission_windows":20,"status_every_sf":64,"superframe_s":1.4}\n' +
    '{"type":"begin","t_us":57000000,"sf":40,"class":"LOCAL","slave":1,"degraded_alive":0,"r1":7.4,"r2":1.4,"base1":75.2,"base2":102.6,"alive":[true,true],"closed":41,"events":3}\n';
  const pg1 = await mk(); await pg1.window.__resonance.loadText(sch1, "schema 1", false);
  pg1.window.__resonance.seek(pg1.window.__resonance.state().tEnd); pg1.window.__resonance.render();
  check(storeys(pg1).filter((x) => x === "local").length === 1 && /floor assumed/.test(doc(pg1).querySelector("#bvLab1 .l3").textContent) && !outDmg(pg1) && amp(pg1) > 5, "schema 1 log (no floors): sensors drawn at assumed floors 5 / 10 and labelled so, local event shown, strong sway for r 7.4");

  // animation and reduced motion
  const pgM = await mk({ search: "?replay=demo_events_sim.jsonl&speed=30&at=sf118" });   // starts paused at sf 118 (before the sensor-1 impact)
  await until(() => pgM.window.__resonance.state().sm.length === 320, 10000, "?replay= load (motion)");
  const MA = pgM.window.__resonance; await sleep(200);
  MA.play(false); MA.state().speed = 1; MA.seek(MA.state().sm.find((m) => m.sf >= 120).t);   // any stretch will do: the amplitude only has to be non-zero
  MA.play(true); const xs = [];
  for (let i = 0; i < 12; i++) { await sleep(150); xs.push(+doc(pgM).getElementById("bsvg").getAttribute("data-roof-dx")); }
  MA.play(false); await sleep(250);
  check(Math.max(...xs) - Math.min(...xs) > 0.5, "playing replay: the building sways (roof dx ranged " + Math.min(...xs).toFixed(2) + " .. " + Math.max(...xs).toFixed(2) + " px)");
  const pgR = await mk({ reduced: true, search: "?replay=demo_events_sim.jsonl&speed=1&at=sf236&play=0" });
  await until(() => pgR.window.__resonance.state().sm.length === 320, 10000, "?replay= load (reduced)");
  const RA = pgR.window.__resonance; await sleep(200);
  RA.play(true); const ys = [];
  for (let i = 0; i < 8; i++) { await sleep(150); const sv = doc(pgR).getElementById("bsvg"); ys.push([+sv.getAttribute("data-roof-dx"), +sv.getAttribute("data-amp")]); }
  check(RA.bvState().reduced && RA.state().playing && ys.every((y) => y[0] === y[1] && y[1] > 0), "prefers-reduced-motion: no animation even while playing, the building stays at its static deflection (roof dx = amplitude: " + JSON.stringify(ys.slice(0, 3)) + " ...)");
  RA.play(false);

  // launch parameter validation (pure) and page behaviour
  const PL = pg.window.__resonance.parseLaunch;
  const goodB = ["wss://broker.hivemq.com:8884/mqtt", "ws://broker.hivemq.com:8000/mqtt", "ws://192.168.1.5:9001", "wss://x.s1.eu.hivemq.cloud:8884/mqtt"];
  const okAll = goodB.every((b) => { const o = PL("?mqtt=1&site=rsn-822298ab&broker=" + encodeURIComponent(b)); return o.mqtt && o.broker === b && o.site === "rsn-822298ab" && o.msgs.length === 0; });
  check(okAll && PL("?mqtt=1&site=Twin_AB-12").site === "Twin_AB-12" && PL("?mqtt=1&site=" + "a".repeat(40)).mqtt, "launch parameters: valid site (letters, digits, _ -, up to 40) and ws(s) broker accepted, mqtt=1 requests the connection");
  const badS = ["a b", "a".repeat(41), "../x", "a/b", "<script>", "x;y", "éa", "a.b", ""];
  const badB = ["javascript:alert(1)", "http://broker.example/mqtt", "wss://", "wss://evil.example/p?x=1", "wss://a b/", "wss://u:p@host/mqtt", "wss://host:123456/mqtt", "ws:/host", "wss://host/%00", "wss://host/a b", "wss://" + "a".repeat(190) + ".com/mqtt"];
  const rejS = badS.filter((x) => x !== "").every((x) => { const o = PL("?mqtt=1&site=" + encodeURIComponent(x)); return o.site === null && !o.mqtt && o.msgs.length === 2; }) && PL("?mqtt=1&site=").mqtt === false;
  const rejB = badB.every((x) => { const o = PL("?mqtt=1&site=ok-1&broker=" + encodeURIComponent(x)); return o.broker === null && !o.mqtt && o.msgs.length === 2; });
  check(rejS && rejB, `launch parameters: ${badS.length} bad site ids and ${badB.length} bad broker URLs are rejected with a message and MQTT is not started`);
  check(!PL("?site=ok-1").mqtt && !PL("").mqtt && PL("?autoconnect=1&site=ok-1").mqtt, "no connection without mqtt=1 (the older autoconnect=1 still works)");
  const pgBad = await mk({ search: "?mqtt=1&site=bad%20site&broker=javascript:alert(1)" }); await sleep(400);
  check(pgBad.window.__resonance.state().mode === "none" && /ignored/.test(doc(pgBad).getElementById("msgs").textContent) && doc(pgBad).getElementById("mqSite").value !== "bad site" && !/javascript/.test(doc(pgBad).getElementById("mqUrl").value), "page with ?mqtt=1 and invalid site / broker: nothing connects, values not put in the fields, message shown");
  const pgOk = await mk({ search: "?mqtt=1&site=rsn-link-test&broker=ws://127.0.0.1:9/mqtt" }); await sleep(500);
  const so = pgOk.window.__resonance.state();
  check(so.mode === "live" && so.kind === "mqtt" && so.mqSite === "rsn-link-test" && doc(pgOk).getElementById("mqUrl").value === "ws://127.0.0.1:9/mqtt" && /MQTT rsn-link-test/.test(doc(pgOk).getElementById("srcBadge").textContent), "?mqtt=1&site=...&broker=...: MQTT live starts on load with that site and broker (site " + so.mqSite + ")");
  doc(pgOk).getElementById("mqBtn").click();

  // phone view
  const pgFile = await mk();   // file:// page, no lan.json
  doc(pgFile).getElementById("phoneBtn").click(); await sleep(100);
  const ft = doc(pgFile).getElementById("phoneBody").textContent;
  check(shown(pgFile, "phoneModal") && ft.includes(".venv\\Scripts\\python.exe tools\\serve_dashboard.py") && /open the printed URL, then use Phone view/.test(ft) && !doc(pgFile).getElementById("qrBox"), "Phone view from file://: modal explains how to start tools\\serve_dashboard.py, no QR");
  pgFile.window.document.dispatchEvent(new pgFile.window.KeyboardEvent("keydown", { key: "Escape" }));
  check(!shown(pgFile, "phoneModal"), "Escape closes the modal");
  const LAN = { port: 8080, urls: ["http://192.168.1.20:8080/", "http://10.0.0.5:8080/"] };
  const qrlib = require(path.join(ROOT, "dashboard", "vendor", "qrcode.js"));
  const darkCount = (u) => { const q = qrlib(0, "M"); q.addData(u); q.make(); let n = 0; for (let r = 0; r < q.getModuleCount(); r++) for (let c = 0; c < q.getModuleCount(); c++) if (q.isDark(r, c)) n++; return { n, m: q.getModuleCount() }; };
  const pgH = await mk({ http: true, lan: LAN }); await sleep(300);
  const HA = pgH.window.__resonance;
  await HA.loadSim({ speed: 20, play: false }); HA.state().playing = false;
  doc(pgH).getElementById("phoneBtn").click(); await until(() => !!doc(pgH).getElementById("phoneUrl"), 3000, "phone modal content");
  let u = doc(pgH).getElementById("phoneUrl").textContent;
  check(u === "http://192.168.1.20:8080/?replay=demo_events_sim.jsonl&speed=20", "Phone view in replay mode: URL from /lan.json (first address) + replay link: " + u);
  let svg = doc(pgH).querySelector("#qrBox svg"), want = darkCount(u), got = (svg.querySelector("path").getAttribute("d").match(/M/g) || []).length;
  check(svg && +svg.getAttribute("data-modules") === want.m && got === want.n, `QR code encodes exactly that URL (${want.m}x${want.m} modules, ${got} dark = ${want.n} from the reference encoder)`);
  const alt = doc(pgH).querySelectorAll(".phone-alt button");
  check(alt.length === 2, "two LAN addresses: both offered as alternatives");
  alt[1].click(); u = doc(pgH).getElementById("phoneUrl").textContent;
  check(u === "http://10.0.0.5:8080/?replay=demo_events_sim.jsonl&speed=20" && +doc(pgH).querySelector("#qrBox svg").getAttribute("data-modules") === darkCount(u).m, "choosing the second address rebuilds URL and QR: " + u);
  doc(pgH).getElementById("phoneClose").click();
  check(!shown(pgH, "phoneModal"), "Close button closes the modal");
  // MQTT view link
  doc(pgH).getElementById("mqSite").value = "twin-ab12cd34"; doc(pgH).getElementById("mqUrl").value = "ws://broker.hivemq.com:8000/mqtt";
  HA.state().mode = "none"; HA.state().replayName = "";
  doc(pgH).getElementById("phoneBtn").click(); await until(() => !!doc(pgH).getElementById("phoneUrl"), 3000, "phone modal content 2");
  u = doc(pgH).getElementById("phoneUrl").textContent;
  check(u === "http://192.168.1.20:8080/?mqtt=1&site=twin-ab12cd34&broker=ws://broker.hivemq.com:8000/mqtt", "Phone view for MQTT live: " + u);
  const back = PL(new URL(u).search);
  check(back.mqtt && back.site === "twin-ab12cd34" && back.broker === "ws://broker.hivemq.com:8000/mqtt" && back.msgs.length === 0, "the generated link round-trips through the page's own launch-parameter parser");
  doc(pgH).getElementById("phoneClose").click();
  doc(pgH).getElementById("mqSite").value = "bad site!"; doc(pgH).getElementById("phoneBtn").click();
  await until(() => !!doc(pgH).getElementById("phoneUrl"), 3000, "phone modal content 3");
  check(doc(pgH).getElementById("phoneUrl").textContent === "http://192.168.1.20:8080/" && /just opens the dashboard/.test(doc(pgH).getElementById("phoneBody").textContent), "an invalid site id in the field is never put into the link");
  const pgN = await mk({ http: true, lan: null }); await sleep(200);
  doc(pgN).getElementById("phoneBtn").click(); await sleep(300);
  check(/serve_dashboard\.py/.test(doc(pgN).getElementById("phoneBody").textContent) && !doc(pgN).getElementById("qrBox"), "http page without /lan.json (404): same explanation, no QR");
  const pgE = await mk({ http: true, lan: { port: 1, urls: ["javascript:alert(1)", "http://evil.example/", "https://192.168.1.2:8080/", "http://192.168.1.2/"] } }); await sleep(200);
  doc(pgE).getElementById("phoneBtn").click(); await sleep(300);
  check(!doc(pgE).getElementById("qrBox") && /serve_dashboard\.py/.test(doc(pgE).getElementById("phoneBody").textContent), "hostile /lan.json entries (javascript:, host names, https, no port) are ignored");
  console.log("--- end building / phone checks ---");
}

// Hardening of the twin panel / truth band against stale, stopped, malformed and hostile twin/state messages, and the other review fixes.
async function twinViewChecks(check, text) {
  console.log("--- twin panel hardening / building view fixes ---");
  const mk = async (opts) => { const p = await openPage(opts); EXTRA_PAGES.push(p); return p; };
  const doc = (p) => p.window.document;
  const shown = (p, id) => !doc(p).getElementById(id).hasAttribute("hidden");
  const btnsOff = (p) => Array.from(doc(p).querySelectorAll("button[data-tw]")).every((b) => b.disabled);
  const btnsOn = (p) => Array.from(doc(p).querySelectorAll("button[data-tw]")).every((b) => !b.disabled);
  const good = (extra) => Object.assign({ running: true, speed: 2, seed: 1, sf: 300, commissioned: true, commission_sf: 100, building: { f1_hz: 7.09, floors: [5, 10] }, damage: { storey: 7, loss: 0.3 },
    f1_true_hz: 6.9, drop_true: 0.027, killed: [], truth: [[0, 0, 7.09], [250, 100000000, 6.9]], last_cmd: {}, log: ["a"], guard_sf: 4, updated_t: Date.now() / 1000, period_s: 5 }, extra || {});
  // a page on an MQTT source that never reaches a broker (port 9): S.mq exists, messages are injected through the page's own handler
  const join = async (site) => { const p = await mk({ search: "?mqtt=1&site=" + site + "&broker=ws://127.0.0.1:9/mqtt" }); await sleep(400); return p; };
  const pg = await join("rsn-twin-a"), A = pg.window.__resonance, S = A.state();
  A.feedMq("twin/state", JSON.stringify(good()), true); A.render();
  check(!doc(pg).getElementById("twinCard").hidden && btnsOn(pg) && /running/.test(doc(pg).getElementById("twState").textContent), "fresh twin/state (retained, heartbeat now): panel shown, buttons enabled");
  check(shown(pg, "bvBand") && doc(pg).getElementById("bvBand").getAttribute("data-truth") === "7", "alive twin with damage: truth band on storey 7");
  // C: stopped
  A.feedMq("twin/state", JSON.stringify(good({ running: false })), false); A.render();
  check(!doc(pg).getElementById("twinCard").hidden && btnsOff(pg) && /twin stopped/.test(doc(pg).getElementById("twState").textContent) && !shown(pg, "bvBand"), "running:false (Ctrl+C): 'twin stopped', buttons disabled, truth band gone");
  // C: stale retained heartbeat (a hard kill leaves it behind)
  A.feedMq("twin/state", JSON.stringify(good({ updated_t: Date.now() / 1000 - 100 })), true); A.render();
  check(btnsOff(pg) && /not responding/.test(doc(pg).getElementById("twState").textContent) && !shown(pg, "bvBand"), "retained state older than 3 x period_s: 'twin not responding', buttons disabled, no truth band");
  A.feedMq("twin/state", JSON.stringify(good()), false); A.render();
  check(btnsOn(pg) && shown(pg, "bvBand"), "a live state after a stale one: alive again");
  const noHb = good(); delete noHb.updated_t; delete noHb.period_s;
  A.feedMq("twin/state", JSON.stringify(noHb), true); A.render();
  check(btnsOn(pg) && shown(pg, "bvBand"), "state without updated_t: the running flag alone decides (alive)");
  // a state stream that goes silent while connected is noticed (local clock)
  A.feedMq("twin/state", JSON.stringify(good()), false);
  S.twinFreshAt = pg.window.performance.now() - 20000; A.render();
  check(btnsOff(pg) && /not responding/.test(doc(pg).getElementById("twState").textContent) && !shown(pg, "bvBand"), "no state for more than 3 periods while connected: 'not responding', buttons disabled, no band");
  // C: disconnect and file load hide the panel
  A.feedMq("twin/state", JSON.stringify(good()), false); A.render();
  check(!doc(pg).getElementById("twinCard").hidden, "(panel visible again after a live state)");
  doc(pg).getElementById("mqBtn").click(); await sleep(100); A.render();
  check(doc(pg).getElementById("twinCard").hidden && !shown(pg, "bvBand"), "after 'MQTT live: disconnect' the twin panel and the band are hidden");
  const pg2 = await join("rsn-twin-b"), A2 = pg2.window.__resonance;
  A2.feedMq("twin/state", JSON.stringify(good()), false); A2.render();
  await A2.loadText(text, "file", false); A2.render();
  check(doc(pg2).getElementById("twinCard").hidden && !shown(pg2, "bvBand") && A2.state().twin === null, "loading a file replaces the MQTT source: twin panel hidden");

  // E: malformed / hostile twin states never break the live feed
  const evil = {
    "no building": { running: true }, "killed string": { running: true, commissioned: true, building: { f1_hz: 7 }, killed: "12", truth: [] }, "building null": { running: false, building: null },
    "truth object": { running: true, building: { f1_hz: 7 }, truth: { length: 5 } },
    "numbers as html": { running: true, speed: "<img src=x onerror=window.__pwned=1>", sf: "<b>", building: { f1_hz: "<i>" }, damage: { storey: "<i>", loss: "x" }, killed: ["<b>", 3, 1, 1], truth: [[0, "<b>", "<i>"]], log: ["<img src=x onerror=window.__pwned=2>", 5, null], drop_true: "x", f1_true_hz: { a: 1 } },
    "out of range": { running: true, speed: 1e309, sf: -1, building: { f1_hz: -5 }, damage: { storey: 99, loss: 7 }, updated_t: "now", period_s: 0 },
    "array": [1, 2], "string": "x", "null": null,
    "huge truth": { running: true, commissioned: true, sf: 1, building: { f1_hz: 7 }, truth: Array.from({ length: 20000 }, (_, i) => [i, i * 1400000, 7 + (i % 7) * 0.01]) },
  };
  const sample = (sf, t) => JSON.stringify({ type: "sample", t_us: t, sf, class: "AMBIENT", slave: 0, degraded_alive: 0, r1: 1.0, r2: 1.1, alive: [true, true], age: [0, 0], commission_n: [20, 20], closed: sf });
  const meta = JSON.stringify({ type: "meta", schema: 2, source: "netsim", rule: "b", T: 3, debounce: 1, commission_windows: 20, status_every_sf: 64, superframe_s: 1.4, floors: [5, 10], epoch: 1, samples: true });
  const pgE = await join("rsn-twin-evil"), EA = pgE.window.__resonance, ES = EA.state();
  EA.feedMq("meta", meta, false);
  let n = 0, allOk = true; const details = [];
  for (const [name, st] of Object.entries(evil)) {
    pgE.window.__pwned = 0;
    EA.feedMq("twin/state", typeof st === "string" ? st : JSON.stringify(st), true);
    try { EA.render(); } catch (e) { allOk = false; details.push(name + ": render threw " + e.message); }
    EA.feedMq("twin/log", "<img src=x onerror=window.__pwned=3>" + "y".repeat(500), false);
    const before = ES.mqRecv, smBefore = ES.sm.length + ES.mqBuf.length;
    n++; EA.feedMq("sample", sample(n, 1000000 + n * 1400000), false);
    if (ES.mqRecv !== before + 1 || ES.sm.length + ES.mqBuf.length !== smBefore + 1) { allOk = false; details.push(name + ": feed stalled (recv " + before + " -> " + ES.mqRecv + ")"); }
    if (pgE.window.__pwned || doc(pgE).querySelector("#twinCard img, #twinCard script, #twinCard b, #twinCard i")) { allOk = false; details.push(name + ": markup injected"); }
    if (ES.twinLog.some((l) => l.length > 300)) { allOk = false; details.push(name + ": long log line kept"); }
  }
  check(allOk, "malformed / hostile twin/state payloads (" + Object.keys(evil).length + " kinds) never throw, never stop the live feed (every following sample was received) and inject nothing" + (details.length ? ": " + details.join("; ") : ""));
  await sleep(3200);   // reorder hold 2.5 s: the samples fed between the bad states are released and parsed
  check(ES.sm.length === n && ES.stats.bad === 0, "all " + n + " samples interleaved with the bad twin states were parsed (" + ES.sm.length + ", malformed " + ES.stats.bad + ")");
  EA.feedMq("twin/state", JSON.stringify({ running: true, commissioned: true, speed: "<x>", building: { f1_hz: "7" }, killed: [1], damage: { storey: 2, loss: 0.3 }, updated_t: Date.now() / 1000, period_s: 5 }), false); EA.render();
  const tx = doc(pgE).getElementById("twState").textContent;
  check(!/<|NaN|undefined|null/.test(tx) && /dead sensor\(s\): 1/.test(tx) && /damage: storey 2 by 30 %/.test(tx), "twin state line shows only validated values: '" + tx + "'");
  doc(pgE).getElementById("mqBtn").click();

  // D: loss labels and accepted range
  const pgD = await join("rsn-twin-d"), DA = pgD.window.__resonance;
  const lbl = () => doc(pgD).getElementById("bvTruthT").textContent;
  const withLoss = (l) => { DA.feedMq("twin/state", JSON.stringify(good({ damage: { storey: 4, loss: l } })), false); DA.render(); };
  const rejected = [0.999, 0.004, 0.049, 0.51].map((l) => { withLoss(l); return shown(pgD, "bvBand"); });
  check(rejected.every((x) => !x), "loss outside the twin's 5-50 % range (0.999, 0.004, 0.049, 0.51): no band, no '-100 %' / '-0 %' label");
  const labels = [0.05, 0.5, 0.125, 0.3].map((l) => { withLoss(l); return lbl().replace("simulator truth: storey 4, ", ""); });
  check(labels.join("|") === "−5 % stiffness|−50 % stiffness|−12.5 % stiffness|−30 % stiffness", "loss labels: 5 %, 50 %, one decimal only where needed (12.5 %), 30 %: " + labels.join(" | "));
  doc(pgD).getElementById("mqBtn").click();

  // F: floors outside the drawing, always 10 storeys
  for (const fl of [[5, 50], [12, 3], [0, 10]]) {
    const pf = await mk(); const FA = pf.window.__resonance;
    await FA.loadText(text.replace('"floors":[5,10]', '"floors":' + JSON.stringify(fl)), "floors " + fl, false);
    FA.seek(FA.state().tEnd); FA.render();
    const outs = [0, 1].filter((k) => fl[k] < 1 || fl[k] > 10), note = doc(pf).getElementById("bNote").textContent;
    const ok = doc(pf).querySelectorAll(".bv-st").length === 10 && !/count (40|12)/.test(note) &&
      outs.every((k) => /outside the drawing/.test(doc(pf).querySelector("#bvLab" + (k + 1) + " .l3").textContent) && doc(pf).getElementById("bvIc" + (k + 1)).hasAttribute("hidden")) &&
      [0, 1].filter((k) => !outs.includes(k)).every((k) => !doc(pf).getElementById("bvIc" + (k + 1)).hasAttribute("hidden")) && /outside the drawn 10 storeys/.test(note) && /always shows the simulator's 10 storeys/.test(note);
    check(ok, "meta.floors " + JSON.stringify(fl) + ": always 10 storeys, a sensor outside 1..10 is labelled 'outside the drawing' (icon not drawn); caption: " + note.slice(0, 110));
  }
  // H: every building label fits the room right of the building
  const pgH = await mk(); const HA = pgH.window.__resonance;
  await HA.loadText(text.replace('"floors":[5,10]', '"floors":[5,50]'), "h", false); HA.seek(HA.state().tEnd); HA.render();
  const long = Array.from(doc(pgH).querySelectorAll(".bv-lab text")).map((e) => e.textContent).filter((t) => t.length > 27);
  const vb = doc(pgH).getElementById("bsvg").getAttribute("viewBox").split(" ").map(Number);
  check(long.length === 0 && vb[0] + vb[2] >= 555, "building labels are at most 27 characters and the viewBox reaches x = " + (vb[0] + vb[2]));
  // G / I / J: wording and palette
  const src = fs.readFileSync(PAGE, "utf8");
  await HA.loadText(text, "g", false); HA.seek(HA.state().tEnd); HA.render();
  const dsub = doc(pgH).getElementById("dSub").textContent;
  check(/cannot tell which storey/.test(dsub) && !/looks like this/.test(src), "DAMAGE banner: 'Natural-frequency tracking detects a stiffness loss but cannot tell which storey' (no storey hint)");
  check(/class="legend-note"/.test(src) && !/R = master restart",4,H-1/.test(src) && /red bar = DAMAGE alarm/.test(doc(pgH).getElementById("legend").textContent), "timeline note is HTML (wraps on a phone), not a fixed canvas string");
  check(/\.bv-st\[data-s="local"\]\{fill:var\(--local\)/.test(src) && /\.bv-st\[data-s="building"\]\{fill:var\(--building\)/.test(src), "building colours use the timeline palette (LOCAL = --local, BUILDING = --building)");
}

// B2: a retained damage_state left over from an EARLIER run must not be listed by a page that joins late in the next run. The retained topics are
// written by a small paho script (status online + meta + state + damage_state), then a fresh page joins and is inspected.
const PUB_RETAINED = `
import sys, json, paho.mqtt.client as mqtt
site, host, port, msgs = sys.argv[1], sys.argv[2], int(sys.argv[3]), json.loads(sys.argv[4])
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
c.connect(host, port, 30); c.loop_start()
for leaf, payload in msgs:
    c.publish("resonance/%s/%s" % (site, leaf), payload.encode(), qos=1, retain=True).wait_for_publish()
c.loop_stop(); c.disconnect()
`;
function setRetained(site, msgs) {
  return new Promise((resolve) => {
    const u = new (require("url").URL)(URL_.replace(/^ws/, "http"));
    const ch = spawn(PY, ["-c", PUB_RETAINED, site, u.hostname, u.port || "8000", JSON.stringify(msgs)], { stdio: "ignore" });
    ch.on("close", resolve);
  });
}
async function staleDamageState(check) {
  console.log("--- stale retained damage_state (real broker) ---");
  const site = SITE + "-stale";
  const L = fs.readFileSync(LOG, "utf8").split("\n").filter(Boolean);
  const meta = L[0], dmgLine = L.find((l) => /"type":"damage"/.test(l));
  const sampleAt = (sf) => L.find((l) => l.startsWith('{"type":"sample"') && JSON.parse(l).sf === sf);
  const status = JSON.stringify({ state: "online", epoch: JSON.parse(meta).epoch });
  const join = async () => {
    const pg = await openPage();
    pg.window.document.getElementById("mqUrl").value = URL_;
    pg.window.document.getElementById("mqSite").value = site;
    pg.window.document.getElementById("mqBtn").click();
    await until(() => { const s = pg.window.__resonance.state(); return s.meta && s.mqMaster === "online"; }, 30000, "retained meta + online status");
    await sleep(7500);   // retained state 1.5 s + reorder hold 2.5 s + margin
    const st = pg.window.__resonance.state();
    pg.window.document.getElementById("mqBtn").click();
    return st;
  };
  // run 2 has just started (state sf 5) but run 1's BEGIN (sf 235) is still the retained damage_state
  await setRetained(site, [["status", status], ["meta", meta], ["state", sampleAt(5)], ["damage_state", dmgLine]]);
  const a = await join();
  check(a.dl.length === 0, "late page in run 2 does not list run 1's retained BEGIN (damage lines " + a.dl.length + ", samples " + a.sm.length + ")");
  // the retained state is later than the BEGIN and says dmg 1: the page learns the alarm
  await setRetained(site, [["state", sampleAt(250)]]);
  const b = await join();
  check(b.dl.length === 1 && b.dl[0].state === "BEGIN", "late page during an active alarm lists the retained BEGIN (damage lines " + b.dl.length + ")");
  await setRetained(site, [["status", ""], ["meta", ""], ["state", ""], ["damage_state", ""]]);
}

// --twin: the interactive digital twin (tools/twin_server.py, real broker). Starts the server on a fresh random site at --twin-speed
// (default 20), opens the page, clicks Apply damage (storey 1, 30 %) and Kill sensor 2 and checks the log line, DAMAGE BEGIN and node DEAD.
async function twinChecks(check) {
  console.log("--- digital twin (real broker) ---");
  const site = "twin-" + Math.random().toString(16).slice(2, 10);
  const speed = arg("twin-speed", "20");
  const u = new (require("url").URL)(URL_.replace(/^ws/, "http"));
  const env = Object.assign({}, process.env); delete env.TELEGRAM_BOT_TOKEN; delete env.TELEGRAM_CHAT_ID;
  const pg = await openPage();
  const w = pg.window, R = w.__resonance, S = () => R.state(), doc = w.document;
  doc.getElementById("mqUrl").value = URL_; doc.getElementById("mqSite").value = site; doc.getElementById("mqBtn").click();
  await until(() => doc.getElementById("mqState").textContent === "MQTT connected", 30000, "MQTT connected");
  check(doc.getElementById("twinCard").hidden === true, "twin panel hidden while no twin/state exists on the site");
  const child = spawn(PY, [path.join(ROOT, "tools", "twin_server.py"), "--site", site, "--speed", speed, "--host", u.hostname, "--port", u.port || "8000", "--transport", "websockets", "--ws-path", u.pathname], { stdio: ["ignore", "pipe", "pipe"], env });
  let out = ""; child.stdout.on("data", (d) => (out += d)); child.stderr.on("data", (d) => (out += d));
  try {
    await until(() => !doc.getElementById("twinCard").hidden, 90000, "twin panel (retained twin/state)");
    const btn = (sel) => doc.querySelector(sel);
    const wasComm = S().twin && S().twin.commissioned === false;
    if (wasComm) check(btn('button[data-tw="damage"]').disabled === true, "buttons are disabled while the twin commissions");
    await until(() => S().twin && S().twin.commissioned === true && !btn('button[data-tw="damage"]').disabled, 120000, "commissioning complete (buttons enabled)");
    check(true, "twin panel shown, commands enabled after commissioning (sf " + S().twin.sf + ", speed " + S().twin.speed + "x)");
    doc.getElementById("twStorey").value = "1"; doc.getElementById("twLoss").value = "0.3";
    const t0 = Date.now();
    btn('button[data-tw="damage"]').click();
    await until(() => S().twinLog.some((l) => /damage storey 1 by 30 % applied/.test(l)), 30000, "twin log line for the damage command");
    const lat1 = (Date.now() - t0) / 1000;
    check(true, `click -> twin log line "${S().twinLog.filter((l) => /damage storey 1/.test(l))[0]}" after ${lat1.toFixed(2)} s`);
    await until(() => S().twin.damage && S().twin.truth.length >= 2, 15000, "twin/state with the new truth");
    const tr = S().twin.truth, last = tr[tr.length - 1];
    check(Math.abs(last[2] - 6.818) < 0.01 && Math.abs(S().twin.drop_true - 0.0389) < 0.002, "twin/state: true f1 " + last[2] + " Hz, expected drop " + (S().twin.drop_true * 100).toFixed(2) + " %");
    await until(() => S().dl.some((d) => d.state === "BEGIN"), 150000, "DAMAGE BEGIN from the firmware logic");
    const lat2 = (Date.now() - t0) / 1000;
    check(true, `click -> DAMAGE BEGIN on the page after ${lat2.toFixed(1)} s wall (speed ${speed}x)`);
    await sleep(2300);   // rate limit: 1 command per 2 s
    const t1 = Date.now();
    btn('button[data-tw="kill"][data-slave="2"]').click();
    await until(() => S().twinLog.some((l) => /sensor 2 killed/.test(l)), 30000, "twin log line for the kill command");
    const lat3 = (Date.now() - t1) / 1000;
    await until(() => S().nl.some((n) => n.slave === 2 && n.alive === false), 90000, "node DEAD for sensor 2");
    check(true, `click -> twin log ${lat3.toFixed(2)} s, -> sensor 2 DEAD on the page ${((Date.now() - t1) / 1000).toFixed(1)} s`);
    check(/dead sensor/.test(doc.getElementById("twState").textContent) && pg.errors.length === 0, "twin state line shows the dead sensor; no script errors" + (pg.errors.length ? ": " + pg.errors.slice(0, 2).join(" | ") : ""));
    doc.getElementById("mqBtn").click();
  } finally {
    child.kill();
    await sleep(500);
    if (!/SITE=/.test(out)) console.log("twin_server output: " + out.slice(-600));
  }
}

(async () => {
  let ok = true;
  const check = (c, m) => { console.log((c ? "PASS  " : "FAIL  ") + m); ok = ok && !!c; };

  // reference: the same file replayed through the page's own loader
  const ref = await openPage();
  const text = fs.readFileSync(LOG, "utf8");
  await ref.window.__resonance.loadText(text, "reference", false);
  const rs = ref.window.__resonance.state();
  const refSnap = snap(rs);
  console.log(`reference replay: ${rs.ev.length} events, ${rs.sm.length} samples, ${rs.nl.length} node lines, ${rs.rs.length} restart lines, ${rs.dl.length} damage lines`);
  const wantBegins = rs.ev.filter((e) => e.type === "begin" && (e.cls === 2 || e.cls === 3)).length;
  const wantDead = rs.nl.filter((n) => !n.alive).length;

  await offlineChecks(check);
  if (process.argv.includes("--twin")) { await twinChecks(check); console.log("SUMMARY " + (ok ? "OK" : "FAILED")); process.exit(ok ? 0 : 1); }
  if (process.argv.includes("--stale-damage")) { await staleDamageState(check); console.log("SUMMARY " + (ok ? "OK" : "FAILED")); process.exit(ok ? 0 : 1); }
  if (process.argv.includes("--offline")) { console.log("SUMMARY " + (ok ? "OK" : "FAILED")); process.exit(ok ? 0 : 1); }

  // MQTT page
  const pg = await openPage();
  const w = pg.window, R = w.__resonance;
  check(typeof w.mqtt !== "undefined" && typeof w.mqtt.connect === "function", "vendor/mqtt.min.js loaded in the page (global mqtt)");
  w.document.getElementById("mqUrl").value = URL_;
  w.document.getElementById("mqSite").value = SITE;
  w.document.getElementById("mqBtn").click();
  await until(() => w.document.getElementById("mqState").textContent === "MQTT connected", 30000, "MQTT connected");
  check(true, "page connected to " + URL_ + " (state pill: " + w.document.getElementById("mqState").textContent + ")");
  await sleep(4000);   // subscription + retained replay of an earlier run, if any

  const args = [path.join(ROOT, "tools", "sim_publisher.py"), "--site", SITE, "--jsonl", LOG, "--speed", SPEED,
    "--host", new (require("url").URL)(URL_.replace(/^ws/, "http")).hostname, "--port", new (require("url").URL)(URL_.replace(/^ws/, "http")).port || "1883",
    "--transport", "websockets", "--ws-path", new (require("url").URL)(URL_.replace(/^ws/, "http")).pathname];
  if (/^wss:/.test(URL_)) args.push("--tls");
  const t0 = Date.now();
  const child = spawn(PY, args, { stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  child.stdout.on("data", (d) => (out += d)); child.stderr.on("data", (d) => (out += d));
  const code = await new Promise((r) => child.on("close", r));
  console.log(`publisher exit ${code} in ${((Date.now() - t0) / 1000).toFixed(1)} s: ${out.trim().split(/\r?\n/).pop()}`);
  check(code === 0, "publisher exit code 0");

  const S = () => R.state();
  await until(() => S().mqMaster === "offline", 60000, "retained offline status");
  await until(() => S().mqBuf.length === 0 && S().sm.length >= rs.sm.length, 30000, "reorder buffer drained");
  await sleep(1500);
  const st = S();
  console.log(`page: received ${st.mqRecv} messages, ${st.ev.length} events, ${st.sm.length} samples, ${st.nl.length} node lines, ${st.rs.length} restarts; dup skipped ${st.mqDup}; counts text: "${w.document.getElementById("mqCounts").textContent}"`);
  check(st.mode === "live" && st.kind === "mqtt", "page is in live/mqtt mode");
  const ps = R.parseStatus;
  check(JSON.stringify(ps('{"state":"offline","epoch":5}')) === '{"state":"offline","epoch":5}' && ps("online").state === "online" && ps("junk").state === null, "status payload parser (JSON with epoch, legacy text, junk)");
  check(st.mqMaster === "offline" && st.meta && Number.isInteger(st.meta.epoch), "master status from the retained {state,epoch} message: " + st.mqMaster + ", meta epoch " + (st.meta && st.meta.epoch));
  check(st.stats.bad === 0 && st.stats.capped === 0, "no malformed or dropped lines (bad " + st.stats.bad + ", capped " + st.stats.capped + ")");
  check(st.sm.length === rs.sm.length && st.ev.length === rs.ev.length && st.nl.length === rs.nl.length && st.rs.length === rs.rs.length && st.dl.length === rs.dl.length,
    `same counts as the replayed file (events ${st.ev.length}/${rs.ev.length}, samples ${st.sm.length}/${rs.sm.length}, node ${st.nl.length}/${rs.nl.length}, restarts ${st.rs.length}/${rs.rs.length}, damage lines ${st.dl.length}/${rs.dl.length})`);
  const A = JSON.parse(snap(st)), B = JSON.parse(refSnap);
  const bad = Object.keys(B).filter((k) => JSON.stringify(A[k]) !== JSON.stringify(B[k]));
  for (const k of bad) {
    const x = A[k], y = B[k];
    if (Array.isArray(x)) { const i = x.findIndex((v, j) => JSON.stringify(v) !== JSON.stringify(y[j])); console.log("  differs in " + k + " at index " + i + ": live " + JSON.stringify(x[i]) + " vs replay " + JSON.stringify(y[i])); }
  }
  check(bad.length === 0, "parsed state (events, samples incl. ae/f1/drop/dmg, node lines, damage lines, restarts, segments, meta, floors) identical to the replay of the same file" + (bad.length ? " (differs: " + bad.join(", ") + ")" : ""));
  check(st.mqRecv >= rs.sm.length + rs.ev.length, "received count includes every line (" + st.mqRecv + " messages incl. retained state/status)");
  const begins = pg.notes.filter((n) => /vibration event/.test(n.title)).length, deads = pg.notes.filter((n) => /offline/.test(n.title)).length;
  check(begins === wantBegins && deads === wantDead, `browser notifications: ${begins} event begins (want ${wantBegins}), ${deads} sensor-offline (want ${wantDead}); first: ${JSON.stringify(pg.notes[0])}`);
  check(w.document.getElementById("liveBar").hidden === false && w.document.getElementById("mqBtn").textContent === "MQTT live: disconnect", "live bar visible, button offers disconnect");
  w.document.getElementById("mqBtn").click();
  check(w.document.getElementById("mqState").textContent === "MQTT idle" && w.document.getElementById("mqBtn").textContent === "MQTT live: connect", "disconnect works");

  // a late page: the retained meta (floors) and status arrive at once; the retained state is NOT fed because the master is offline (stale)
  const late = await openPage();
  late.window.document.getElementById("mqUrl").value = URL_;
  late.window.document.getElementById("mqSite").value = SITE;
  late.window.document.getElementById("mqBtn").click();
  await until(() => { const s = late.window.__resonance.state(); return s.meta && s.mqMaster; }, 30000, "retained meta + status in a late page");
  await sleep(2500);
  const ls = late.window.__resonance.state();
  check(ls.meta && ls.meta.floors && ls.meta.floors.length === 2 && ls.mqMaster === "offline", "late page learned floors " + JSON.stringify(ls.meta && ls.meta.floors) + " from the retained meta and master status '" + ls.mqMaster + "'");
  check(ls.sm.length + ls.ev.length === 0, "the stale retained state of an offline master is not fed into the page");
  late.window.document.getElementById("mqBtn").click();

  // the page without the vendored library still works and says so
  const nolib = await openPage();
  nolib.window.mqtt = undefined;
  nolib.window.document.getElementById("mqBtn").click();
  check(/MQTT library/.test(nolib.window.document.getElementById("msgs").textContent) && nolib.window.document.getElementById("mqState").textContent === "MQTT library missing", "without the library: clear message, page keeps working");

  const errs = pg.errors.filter((e) => !/Not implemented/.test(e));
  check(errs.length === 0, "no script errors in the page" + (errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""));
  console.log("SUMMARY " + (ok ? "OK" : "FAILED"));
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
