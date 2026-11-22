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
// (broker.hivemq.com:8884 / wss and 1883 are blocked on some networks; ws://...:8000/mqtt is the plain WebSocket port.)
"use strict";
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { JSDOM, VirtualConsole } = require("jsdom");

const arg = (n, d) => { const i = process.argv.indexOf("--" + n); return i > 0 ? process.argv[i + 1] : d; };
const ROOT = path.resolve(__dirname, "..", "..");
const URL_ = arg("url", "wss://broker.hivemq.com:8884/mqtt");
const SITE = arg("site", "rsn-822298ab");
const LOG = path.resolve(arg("log", path.join(ROOT, "dashboard", "demo_events_sim.jsonl")));
const PY = arg("python", "python");
const SPEED = arg("speed", "0");
const PAGE = path.join(ROOT, "dashboard", "index.html");

function canvasStub(window) {
  const ctx = new Proxy({}, { get: (t, k) => (k === "measureText" ? () => ({ width: 10 }) : k in t ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } });
  window.HTMLCanvasElement.prototype.getContext = () => ctx;
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener() {}, addListener() {} }));
}

function openPage(opts) {
  const vc = new VirtualConsole();
  const errors = [];
  vc.on("jsdomError", (e) => errors.push(String(e && e.message || e)));
  vc.on("error", (e) => errors.push(String(e)));
  const notes = [];
  return new Promise((resolve, reject) => {
    const dom = new JSDOM(fs.readFileSync(PAGE, "utf8"), {
      url: "file:///" + PAGE.replace(/\\/g, "/") + ((opts && opts.search) || ""), runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
      beforeParse(window) {
        canvasStub(window);
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
  const errs = [pgA, pgB, pgC, pgD, pgE, pgF, pgG, pgH, ...badPages].flatMap((p) => p.errors).filter((e) => !/Not implemented/.test(e));
  check(errs.length === 0, "no script errors in any offline page" + (errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""));
  console.log("--- end offline checks ---");
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
