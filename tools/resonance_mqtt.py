"""Shared MQTT helpers of the Resonance IoT tools (sim_publisher.py, alerter.py, e2e test).

Topic scheme (mirrors firmware/esp32/include/mqtt_logic.h, which is unit tested on the PC; keep the two in step):
    resonance/<site>/<type>   one JSON line per message, routed by the line's own "type" (unknown future types too)
    resonance/<site>/state    retained: the latest decision line (sample / begin / change / status)
    resonance/<site>/status   retained: "online" (published after every connect) / "offline" (last will)
    meta is published retained; the leaf names "state" and "status" are reserved, so a line of that type goes to
    "<type>_line" (the fusion `status` line lands on .../status_line).
The payload is the JSON line WITHOUT its trailing newline, byte for byte what the formatter wrote.
"""
import os
import re
import ssl
import sys
from collections import namedtuple

DEFAULT_SITE = "rsn-822298ab"          # random id chosen for the demo; anybody who knows it can read a public broker's topics
DEFAULT_HOST = "broker.hivemq.com"     # free public broker: simulator / demo data only
DEFAULT_PORT = 1883
DEFAULT_WSS = "wss://broker.hivemq.com:8884/mqtt"
SITE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
TYPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
DECISION_TYPES = ("sample", "begin", "change", "status")
EVENT_TYPES = ("begin", "change", "status")

Route = namedtuple("Route", "leaf kind retain to_state")


def site_ok(site):
    return isinstance(site, str) and bool(SITE_RE.match(site))


def json_type(line):
    """First "type":"..." of a JSON-looking line, or None (same rules as mqtt::json_type in mqtt_logic.h)."""
    key = '"type":"'
    if not line or line[0] != "{":
        return None
    i = line.find(key, 1)
    if i < 0:
        return None
    j = line.find('"', i + len(key))
    if j < 0:
        return None
    t = line[i + len(key):j]
    return t if TYPE_RE.match(t) else None


def route(line):
    t = json_type(line)
    if t is None:
        return Route("other", "other", False, False)
    leaf = t + "_line" if t in ("state", "status") else t
    return Route(leaf, t, t == "meta", t in DECISION_TYPES)


def topic(site, leaf):
    if not site_ok(site):
        raise ValueError("bad site id %r (a-z 0-9 - _, max 32)" % (site,))
    return "resonance/%s/%s" % (site, leaf)


def add_connection_args(ap):
    g = ap.add_argument_group("broker")
    g.add_argument("--host", default=os.environ.get("MQTT_HOST", DEFAULT_HOST))
    g.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", DEFAULT_PORT)))
    g.add_argument("--tls", action="store_true", default=os.environ.get("MQTT_TLS", "") == "1", help="TLS (port 8883 on a private broker)")
    g.add_argument("--user", default=os.environ.get("MQTT_USER"), help="or env MQTT_USER")
    g.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"), help="or env MQTT_PASSWORD (prefer the environment: arguments show up in process lists)")
    g.add_argument("--site", default=os.environ.get("RESONANCE_SITE", DEFAULT_SITE), help="site id (topic prefix resonance/<site>/), default %(default)s")
    g.add_argument("--transport", choices=("tcp", "websockets"), default=os.environ.get("MQTT_TRANSPORT", "tcp"),
                   help="websockets = MQTT over WebSocket (HiveMQ public: --port 8000, or 8884 with --tls), for networks that block 1883")
    g.add_argument("--ws-path", default="/mqtt")


def make_client(args, client_id=None, will_site=None):
    """paho client (v2 callback API) configured from add_connection_args(); optional retained last will on .../status."""
    import paho.mqtt.client as mqtt
    if not site_ok(args.site):
        raise SystemExit("bad --site %r (a-z 0-9 - _, max 32)" % (args.site,))
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id or "", protocol=mqtt.MQTTv311, transport=args.transport)
    if args.transport == "websockets":
        c.ws_set_options(path=args.ws_path)
    if args.user:
        c.username_pw_set(args.user, args.password or "")
    if args.tls:
        c.tls_set(cert_reqs=ssl.CERT_REQUIRED)  # system CA store
    if will_site:
        c.will_set(topic(will_site, "status"), b"offline", qos=1, retain=True)
    return c


def log(msg):
    print(msg, file=sys.stderr, flush=True)
