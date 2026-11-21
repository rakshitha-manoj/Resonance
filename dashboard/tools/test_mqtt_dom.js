// Headless test of the dashboard's "MQTT live" source (index.html + vendor/mqtt.min.js) in jsdom, against a real broker.
//   1. loads the page, connects it to the broker (MQTT over WebSocket) for a site id,
//   2. runs tools/sim_publisher.py (a separate process) publishing a bundled JSONL log,
//   3. waits for the master's retained "offline" status, then compares the page's parsed state (events, samples, node lines,
//      restarts, timeline segments) with the state of a second page that REPLAYED the same JSONL file through loadText(),
//      and checks the browser notifications that fired.
// Usage (jsdom is not a project dependency: install it anywhere, e.g. `npm i jsdom` in a scratch folder):
//   NODE_PATH=<folder>/node_modules node dashboard/tools/test_mqtt_dom.js [--url ws://broker.hivemq.com:8000/mqtt]
//        [--site rsn-822298ab] [--log dashboard/demo_events_faults_sim.jsonl] [--python <python>] [--speed 0]
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
const LOG = path.resolve(arg("log", path.join(ROOT, "dashboard", "demo_events_faults_sim.jsonl")));
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
      url: "file:///" + PAGE.replace(/\\/g, "/"), runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
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
const snap = (S) => JSON.stringify({ ev: S.ev, sm: S.sm, nl: S.nl, rs: S.rs.map((r) => ({ t: r.t, epoch: r.epoch, prev: r.prev })), segs: S.segs, floors: S.meta && S.meta.floors, schema: S.meta && S.meta.schema });

(async () => {
  let ok = true;
  const check = (c, m) => { console.log((c ? "PASS  " : "FAIL  ") + m); ok = ok && !!c; };

  // reference: the same file replayed through the page's own loader
  const ref = await openPage();
  const text = fs.readFileSync(LOG, "utf8");
  await ref.window.__resonance.loadText(text, "reference", false);
  const rs = ref.window.__resonance.state();
  const refSnap = snap(rs);
  console.log(`reference replay: ${rs.ev.length} events, ${rs.sm.length} samples, ${rs.nl.length} node lines, ${rs.rs.length} restart lines`);
  const wantBegins = rs.ev.filter((e) => e.type === "begin" && (e.cls === 2 || e.cls === 3)).length;
  const wantDead = rs.nl.filter((n) => !n.alive).length;

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
  check(st.sm.length === rs.sm.length && st.ev.length === rs.ev.length && st.nl.length === rs.nl.length && st.rs.length === rs.rs.length,
    `same counts as the replayed file (events ${st.ev.length}/${rs.ev.length}, samples ${st.sm.length}/${rs.sm.length}, node ${st.nl.length}/${rs.nl.length}, restarts ${st.rs.length}/${rs.rs.length})`);
  const A = JSON.parse(snap(st)), B = JSON.parse(refSnap);
  const bad = Object.keys(B).filter((k) => JSON.stringify(A[k]) !== JSON.stringify(B[k]));
  for (const k of bad) {
    const x = A[k], y = B[k];
    if (Array.isArray(x)) { const i = x.findIndex((v, j) => JSON.stringify(v) !== JSON.stringify(y[j])); console.log("  differs in " + k + " at index " + i + ": live " + JSON.stringify(x[i]) + " vs replay " + JSON.stringify(y[i])); }
  }
  check(bad.length === 0, "parsed state (events, samples, node lines, restarts, segments, floors) identical to the replay of the same file" + (bad.length ? " (differs: " + bad.join(", ") + ")" : ""));
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
