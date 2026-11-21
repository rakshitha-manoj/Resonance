#!/usr/bin/env python
"""Alerts for the Resonance MQTT feed: subscribes to resonance/<site>/ and sends Telegram messages for
  * begin events whose class is LOCAL or BUILDING (and a change INTO LOCAL / BUILDING from another event class),
  * node DEAD / ALIVE (a slave lost / came back), restart (the master rebooted), damage (future schema 3 line),
  * the master device going offline (last-will message on .../status).
Telegram is used when the environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set (Bot API sendMessage over
HTTPS, urllib only); otherwise the alert text is just printed ("dry run"). Nothing secret is ever printed or logged: the token
is read from the environment only and scrubbed from every message the tool prints.
Rate limiting: the same alert key (kind + class + slave) is sent at most once per --cooldown seconds, and at most
--max-per-hour alerts per hour in total; suppressed alerts are counted and mentioned in the next message that does go out.

    set TELEGRAM_BOT_TOKEN=...   set TELEGRAM_CHAT_ID=...        (see dashboard/README.md for creating a bot)
    python tools/alerter.py --site rsn-822298ab                  (same broker options as sim_publisher.py)
    python tools/alerter.py --selftest-message                   (sends / prints one test message and exits)
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resonance_mqtt as rm  # noqa: E402

API = "https://api.telegram.org/bot%s/sendMessage"


def scrub(text, secrets):
    """Removes every secret value from text (defence in depth: error strings might echo a URL)."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


class RateLimiter:
    """Per-key cooldown plus a global hourly budget. allow() returns (ok, suppressed_since_last_sent)."""

    def __init__(self, cooldown_s=60.0, max_per_hour=30, clock=time.monotonic):
        self.cooldown = cooldown_s
        self.max_per_hour = max_per_hour
        self.clock = clock
        self.last = {}
        self.sent = collections.deque()
        self.suppressed = 0

    def allow(self, key):
        now = self.clock()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if key in self.last and now - self.last[key] < self.cooldown:
            self.suppressed += 1
            return False, 0
        if len(self.sent) >= self.max_per_hour:
            self.suppressed += 1
            return False, 0
        self.last[key] = now
        self.sent.append(now)
        n, self.suppressed = self.suppressed, 0
        return True, n


class Telegram:
    def __init__(self, token, chat_id, timeout=10, opener=None):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout
        self.opener = opener or urllib.request.urlopen

    def send(self, text):
        body = json.dumps({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(API % self.token, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(req, timeout=self.timeout) as r:
                ok = 200 <= r.status < 300
            return ok, "" if ok else "HTTP %d" % r.status
        except urllib.error.HTTPError as e:   # str(e) has no URL, but scrub anyway at the caller
            return False, "HTTP %d" % e.code
        except Exception as e:  # network down, DNS, timeout ...
            return False, type(e).__name__


class Alerter:
    def __init__(self, sender=None, limiter=None, out=print, secrets=()):
        self.sender = sender            # object with .send(text) -> (ok, err) or None (print only)
        self.limiter = limiter or RateLimiter()
        self.out = out
        self.secrets = secrets
        self.floors = None
        self.T = self.T_ae = None
        self.prev_key = None
        self.meta_epoch = None
        self.alive = {}
        self.sent = self.failed = self.printed = 0

    def floor(self, slave):
        if self.floors and slave in (1, 2):
            return "floor %s (sensor %d)" % (self.floors[slave - 1], slave)
        return "sensor %s" % slave

    def detector(self, o, cls, slave):
        """' [AE 9.5 >= 6]' / ' [std 3.4 >= 3]' / both: which detector of rule f is at or over its threshold on this line (schema 3)."""
        slaves = (slave,) if cls == "LOCAL" else (1, 2)
        ae, r = o.get("ae"), (o.get("r1"), o.get("r2"))
        hits = []
        for n in slaves:
            rv = r[n - 1] if n in (1, 2) else None
            av = ae[n - 1] if isinstance(ae, list) and len(ae) == 2 and n in (1, 2) else None
            if self.T is not None and isinstance(rv, (int, float)) and rv >= self.T:
                hits.append("std %.1f >= %g" % (rv, self.T))
            if self.T_ae is not None and isinstance(av, (int, float)) and av >= self.T_ae:
                hits.append("AE %.1f >= %g" % (av, self.T_ae))
        return " [%s]" % ", ".join(hits) if hits else ""

    def classify(self, o, retained=False):
        """Returns (key, text) for a line that deserves an alert, else None."""
        t = o.get("type")
        if t == "meta":
            self.prev_key = None            # a new run / reboot: the first event of it is compared with nothing
            self.meta_epoch = o.get("epoch") if isinstance(o.get("epoch"), int) else None
            self.T = o.get("T") if isinstance(o.get("T"), (int, float)) else None
            self.T_ae = o.get("T_ae") if isinstance(o.get("T_ae"), (int, float)) else None
            fl = o.get("floors")
            if isinstance(fl, list) and len(fl) == 2:
                self.floors = fl
            return None
        if retained:   # stale copies from before we subscribed
            return None
        if t in ("begin", "change"):
            cls, slave = o.get("class"), o.get("slave") or 0
            prev, self.prev_key = self.prev_key, (cls, slave)
            if cls not in ("LOCAL", "BUILDING") or prev == (cls, slave):
                return None   # an event ended / degraded, or nothing new
            if cls == "BUILDING":
                what = "BUILDING-WIDE vibration event"
            else:
                what = "LOCAL vibration event at " + self.floor(slave)
            if t == "change":
                what += " (changed from %s)" % (prev[0] if prev else "?")
            tail = self.detector(o, cls, slave)
            return ("%s:%s:%s" % (t, cls, slave), "%s%s [sf %s, t=%.1f s]" % (what, tail, o.get("sf"), (o.get("t_us") or 0) / 1e6))
        if t == "node":
            st = o.get("state")
            return ("node:%s:%s" % (o.get("slave"), st), "%s is %s (superframe %s)" % (self.floor(o.get("slave")).capitalize(), "OFFLINE (no valid report for 3 superframes)" if st == "DEAD" else "back ONLINE", o.get("sf")))
        if t == "restart":
            self.prev_key = None
            return ("restart", "The master restarted (new epoch %s); baselines are re-learned" % o.get("epoch"))
        if t == "damage":
            st, drop = o.get("state"), o.get("drop")
            f1, f10 = o.get("f1") or [None, None], o.get("f1_0") or [None, None]
            fmt = lambda v: "?" if v is None else "%.2f" % v  # noqa: E731
            hz = lambda a: "/".join(fmt(x) for x in a)  # noqa: E731
            if st == "BASELINE":
                n = o.get("slave")
                return ("damage:BASELINE:%s" % n, "Note: %s re-commissioned its reference frequency (f1_0 %s Hz); its drop restarts from 0 (sf %s)" % (self.floor(n), fmt(f10[n - 1] if n in (1, 2) else None), o.get("sf")))
            if st == "BEGIN" and isinstance(drop, (int, float)):
                return ("damage:BEGIN", "DAMAGE ALARM: natural frequency down %.1f %% (f1 %s Hz vs %s Hz at commissioning), sf %s" % (drop * 100.0, hz(f1), hz(f10), o.get("sf")))
            if st == "END" and isinstance(drop, (int, float)):
                return ("damage:END", "DAMAGE cleared: natural frequency back to %.1f %% below commissioning (f1 %s Hz), sf %s" % (drop * 100.0, hz(f1), o.get("sf")))
            rest = {k: v for k, v in o.items() if k != "type"}
            return ("damage", "DAMAGE indication: " + json.dumps(rest, separators=(",", ":"))[:300])
        return None

    def status(self, payload, retained):
        state, epoch = rm.parse_status(payload)
        if retained or state is None:
            return None
        if rm.offline_is_stale(state, epoch, self.meta_epoch):
            return None   # the will of an earlier boot's half-open session, fired after the new boot said "online"
        return ("status:" + state, "The master went OFFLINE (last will)" if state == "offline" else "The master is ONLINE")

    def emit(self, key, text, site):
        ok, suppressed = self.limiter.allow(key)
        if not ok:
            return False
        if suppressed:
            text += "  (+%d similar alerts suppressed)" % suppressed
        msg = "Resonance %s: %s" % (site, text)
        if self.sender is None:
            self.out("[dry run, no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID] " + msg)
            self.printed += 1
            return True
        good, err = self.sender.send(msg)
        if good:
            self.sent += 1
            self.out("sent: " + scrub(msg, self.secrets))
        else:
            self.failed += 1
            self.out("telegram send failed (%s): %s" % (scrub(err, self.secrets), scrub(msg, self.secrets)))
        return good


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    rm.add_connection_args(ap)
    ap.add_argument("--cooldown", type=float, default=60.0, help="seconds before the same alert key may be sent again (default 60)")
    ap.add_argument("--max-per-hour", type=int, default=30)
    ap.add_argument("--selftest-message", action="store_true", help="send (or print) one test message and exit")
    args = ap.parse_args()
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    sender = Telegram(token, chat) if token and chat else None
    secrets = [x for x in (token, args.password) if x]
    al = Alerter(sender, RateLimiter(args.cooldown, args.max_per_hour), out=lambda s: print(s, flush=True), secrets=secrets)
    rm.log("alerts: %s" % ("Telegram" if sender else "print only (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send)"))
    if args.selftest_message:
        al.emit("selftest", "test message from tools/alerter.py", args.site)
        return
    base = "resonance/%s/" % args.site
    import paho.mqtt.client as mqtt  # noqa: F401
    c = rm.make_client(args, client_id="resonance-alerter-%d" % os.getpid())

    def on_connect(cl, ud, flags, rc, props=None):
        rm.log("connected, subscribing to %s+" % base)
        cl.subscribe(base + "+", qos=1)

    def on_message(cl, ud, msg):
        leaf = msg.topic[len(base):]
        text = msg.payload.decode("utf-8", "replace")
        r = None
        if leaf == "status":
            r = al.status(msg.payload, msg.retain)
        elif leaf not in ("state", "damage_state"):
            try:
                o = json.loads(text)
            except ValueError:
                return
            if isinstance(o, dict):
                r = al.classify(o, msg.retain and o.get("type") != "meta")
        if r:
            al.emit(r[0], r[1], args.site)

    c.on_connect, c.on_message = on_connect, on_message
    c.reconnect_delay_set(2, 60)
    c.connect(args.host, args.port, keepalive=30)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        rm.log("stopped")


if __name__ == "__main__":
    main()
