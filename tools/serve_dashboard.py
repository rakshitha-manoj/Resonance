#!/usr/bin/env python3
"""Serve the dashboard/ folder (and ONLY that folder) on the local network so a phone on the same Wi-Fi can open it.

    .venv\\Scripts\\python.exe tools\\serve_dashboard.py [--port 8080] [--host 0.0.0.0]

Standard library only, read-only (GET / HEAD), no directory listing, no path outside dashboard/, no dot-files, no dashboard/tools/.
`/lan.json` returns the URLs this machine can be reached on: {"port": 8080, "urls": ["http://192.168.1.20:8080/", ...]}
(the dashboard's "Phone view" button reads it to build the QR code). Nothing leaves your network: no internet hosting.
"""
import argparse
import http.server
import io
import json
import os
import socket
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(os.path.dirname(HERE), "dashboard")
HIDDEN_DIRS = {"tools", "__pycache__"}      # not needed by a browser (validators, tests)


def lan_ipv4():
    """All non-loopback, non-link-local IPv4 addresses of this machine; the one used for the default route first."""
    found = []

    def add(ip):
        if ip and ip not in found and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0":
            found.append(ip)

    try:   # UDP connect sends nothing; it only asks the OS which interface would be used
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 9))
        add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def lan_urls(port, ips=None):
    return ["http://%s:%d/" % (ip, port) for ip in (lan_ipv4() if ips is None else ips)]


def safe_path(root, url_path):
    """Map a request path to a file below root, or None. Refuses '..', backslashes, NUL, drive letters, dot-files,
    hidden folders, anything whose real path leaves root (symlinks included) and directories without index.html."""
    p = url_path.split("?", 1)[0].split("#", 1)[0]     # not urlsplit: it reads "//x" as a host name
    try:
        p = urllib.parse.unquote(p, errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in p or "\\" in p or not p.startswith("/"):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    for x in parts:
        if x == ".." or x.startswith(".") or ":" in x or x in HIDDEN_DIRS:
            return None
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, *parts)) if parts else root_real
    if target != root_real and not target.startswith(root_real + os.sep):
        return None
    if os.path.isdir(target):
        target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return None                      # no directory listing
    return target if os.path.isfile(target) else None


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "ResonanceDashboard/1"
    root = DASHBOARD
    port = 8080

    def translate_path(self, path):          # used by SimpleHTTPRequestHandler.send_head
        t = safe_path(self.root, path)
        return t if t else os.path.join(os.path.realpath(self.root), "\x00missing")

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    def do_POST(self):
        self.send_error(405, "Read-only server")
    do_PUT = do_DELETE = do_PATCH = do_POST

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _lan(self):
        return json.dumps({"port": self.port, "urls": lan_urls(self.port)}).encode()

    def send_head(self):
        if self.path.split("?", 1)[0] == "/lan.json":
            body = self._lan()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)
        if safe_path(self.root, self.path) is None:
            self.send_error(404, "Not found")
            return None
        return super().send_head()


def make_server(port=8080, host="0.0.0.0", root=DASHBOARD):
    h = type("BoundHandler", (Handler,), {"root": root, "port": port})
    srv = http.server.ThreadingHTTPServer((host, port), h)
    srv.daemon_threads = True
    h.port = srv.server_address[1]           # the real port when 0 was asked for
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0 = every network interface)")
    a = ap.parse_args(argv)
    try:
        srv = make_server(a.port, a.host)
    except OSError as e:
        print("Cannot listen on %s:%d (%s). Try --port 8081." % (a.host, a.port, e), file=sys.stderr)
        return 1
    port = srv.server_address[1]
    urls = lan_urls(port)
    print("Serving %s (read-only, nothing else) on port %d." % (os.path.abspath(DASHBOARD), port))
    print("Open on this computer:  http://localhost:%d/" % port)
    if urls:
        print("Open on a phone (same Wi-Fi):")
        for u in urls:
            print("    " + u)
        print("Then press 'Phone view' on the page for a QR code.")
    else:
        print("No network address found: connect to Wi-Fi (or start a phone hotspot) and restart.")
    print()
    print("Windows will ask whether Python may use the network: tick 'Private networks' and Allow.")
    print("If the phone cannot open the address, the Wi-Fi probably isolates devices (campus / guest networks do):")
    print("start a hotspot on a phone, join the laptop and the other phones to it, restart this script, use the new URL.")
    print("Ctrl+C stops the server.")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
