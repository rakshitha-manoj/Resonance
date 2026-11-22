"""Plain-assert tests of tools/serve_dashboard.py (loopback only, no network): python tools/test_serve_dashboard.py"""
import http.client
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import serve_dashboard as sd  # noqa: E402

REPO = os.path.dirname(HERE)
tests = []


def test(f):
    tests.append(f)
    return f


def start(root=sd.DASHBOARD):
    srv = sd.make_server(0, "127.0.0.1", root)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.putrequest(method, path, skip_host=False)     # the path is sent verbatim, no client-side normalisation
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, r.getheader("Content-Type"), body


@test
def lan_json_lists_urls_with_the_real_port():
    srv = start()
    try:
        st, ct, body = get(srv, "/lan.json")
        assert st == 200 and ct.startswith("application/json"), (st, ct)
        j = json.loads(body)
        assert j["port"] == srv.server_address[1]
        assert j["urls"] == sd.lan_urls(j["port"])
        for u in j["urls"]:
            assert u.startswith("http://") and u.endswith(":%d/" % j["port"]), u
            assert not u.startswith("http://127.") and "169.254." not in u
        assert get(srv, "/lan.json?x=1")[0] == 200
    finally:
        srv.shutdown()


@test
def lan_ipv4_excludes_loopback_and_link_local():
    for ip in sd.lan_ipv4():
        assert not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0" and ip.count(".") == 3, ip


@test
def index_and_dashboard_files_are_served():
    srv = start()
    try:
        st, ct, body = get(srv, "/")
        assert st == 200 and b"Resonance Monitor" in body and ct.startswith("text/html")
        assert get(srv, "/index.html")[0] == 200
        assert get(srv, "/demo_events_sim.jsonl")[0] == 200
        st, _, body = get(srv, "/vendor/mqtt.min.js")
        assert st == 200 and len(body) > 1000
        assert get(srv, "/index.html", "HEAD")[0] == 200
        st, _, body = get(srv, "//README.md")           # same as /README.md: the DASHBOARD readme, never the repository's
        assert st == 200 and body == open(os.path.join(sd.DASHBOARD, "README.md"), "rb").read()
        assert get(srv, "/index.html?replay=demo_events_sim.jsonl")[0] == 200
    finally:
        srv.shutdown()


@test
def traversal_attempts_are_refused():
    srv = start()
    try:
        secret = open(os.path.join(REPO, "README.md"), "rb").read()[:40] if os.path.exists(os.path.join(REPO, "README.md")) else b"@@none@@"
        attempts = ["/../README.md", "/..%2fREADME.md", "/%2e%2e/README.md", "/%2e%2e%2fREADME.md", "/%2E%2E%2FREADME.md", "/..\\README.md",
                    "/..%5cREADME.md", "/%2e%2e%5cREADME.md", "/vendor/../../README.md", "/vendor/..%2f..%2fREADME.md",
                    "/....//README.md", "/%252e%252e/README.md", "/..%00/README.md", "/index.html%00.txt", "/C:/Windows/win.ini",
                    "/%2e%2e/%2e%2e/Windows/win.ini", "/vendor/%2e%2e/%2e%2e/tools/serve_dashboard.py", "/..;/README.md", "/.%2e/README.md"]
        for a in attempts:
            st, _, body = get(srv, a)
            assert st in (400, 404), (a, st)
            assert secret not in body and b"def safe_path" not in body, a
    finally:
        srv.shutdown()


@test
def only_dashboard_files_nothing_else():
    srv = start()
    try:
        for a in ["/tools/", "/tools/validate_logs.py", "/tools/embed_sim.py", "/tools/test_mqtt_dom.js", "/__pycache__/", "/.git/config", "/.gitignore", "/vendor/", "/vendor", "/nope.html",
                  "/serve_dashboard.py", "/dashboard/index.html"]:
            st, _, body = get(srv, a)
            assert st == 404, (a, st)
            assert b"Index of" not in body and b"<li>" not in body, a
    finally:
        srv.shutdown()


@test
def windows_spelling_tricks_do_not_reach_hidden_folders_or_files():
    # NTFS ignores case, trailing dots and spaces: 'TOOLS', 'tools.', 'tools ' all name the folder 'tools' (cases from the verifier's attack.py)
    srv = start()
    try:
        for a in ["/TOOLS/validate_logs.py", "/Tools/test_mqtt_dom.js", "/tools./validate_logs.py", "/tools%20/validate_logs.py", "/tools.../embed_sim.py",
                  "/tools::$INDEX_ALLOCATION/validate_logs.py", "/TOOLS~1/validate_logs.py", "/__PYCACHE__/", "/TOOLS/__PYCACHE__/x.pyc", "/tools/__pycache__/",
                  "/%2egitignore", "/.GIT/config"]:
            assert get(srv, a)[0] in (400, 404), a
        # the page itself under other spellings: refused (trailing dot / space) or served identically (case) - never anything else
        for a in ["/index.html.", "/index.html%20", "/index.html::$DATA"]:
            assert get(srv, a)[0] in (400, 404), a
        st, _, body = get(srv, "/INDEX.HTML")
        assert st == 404 or (st == 200 and b"Resonance Monitor" in body)
        assert get(srv, "/VENDOR/QRCODE.JS")[0] in (200, 404)
        # and the real names still work
        assert get(srv, "/vendor/qrcode.js")[0] == 200
    finally:
        srv.shutdown()


@test
def a_second_instance_cannot_take_the_same_port():
    srv = start()
    try:
        port = srv.server_address[1]
        try:
            other = sd.make_server(port, "127.0.0.1")
        except OSError:
            return                           # what main() turns into "Try --port 8081"
        other.server_close()
        raise AssertionError("second server bound the same port")
    finally:
        srv.shutdown()


@test
def startup_hint_names_the_listening_binary_and_the_profile_advice():
    import socket
    import subprocess
    import time
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ)
    pr = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "serve_dashboard.py"), "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
    try:
        out = ""
        t0 = time.time()
        while "Ctrl+C stops" not in out and time.time() - t0 < 15:
            out += pr.stdout.readline()
        assert sd.python_binary() in out, out
        assert "Public" in out and "Private" in out and "isolates devices" in out and "Open on this computer" in out, out
        # a second copy on the same port reports the busy port instead of starting
        pr2 = subprocess.run([sys.executable, os.path.join(HERE, "serve_dashboard.py"), "--port", str(port)], capture_output=True, text=True, timeout=20, env=env)
        assert pr2.returncode == 1 and "--port 8081" in pr2.stderr, (pr2.returncode, pr2.stderr)
    finally:
        pr.terminate()
        pr.wait(timeout=10)


@test
def no_directory_listing_in_a_folder_without_index():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub"))
        open(os.path.join(d, "a.txt"), "w").write("hello")
        open(os.path.join(d, "sub", "b.txt"), "w").write("inner")
        srv = start(d)
        try:
            assert get(srv, "/")[0] == 404          # no index.html -> no listing
            assert get(srv, "/sub/")[0] == 404
            st, _, body = get(srv, "/a.txt")
            assert st == 200 and body == b"hello"
            assert get(srv, "/sub/b.txt")[0] == 200
            assert get(srv, "/sub/../a.txt")[0] == 404          # any '..' segment is refused, even one that would stay inside
            assert get(srv, "/sub/../../" + os.path.basename(d) + "/a.txt")[0] == 404
        finally:
            srv.shutdown()


@test
def symlink_or_junction_escape_is_refused():
    with tempfile.TemporaryDirectory() as outer:
        root = os.path.join(outer, "root")
        os.makedirs(root)
        open(os.path.join(outer, "secret.txt"), "w").write("secret")
        try:
            os.symlink(os.path.join(outer, "secret.txt"), os.path.join(root, "link.txt"))
        except (OSError, NotImplementedError):
            print("  (symlinks not permitted here, skipped)")
            return
        srv = start(root)
        try:
            assert get(srv, "/link.txt")[0] == 404
        finally:
            srv.shutdown()


@test
def write_methods_are_refused():
    srv = start()
    try:
        for m in ("POST", "PUT", "DELETE"):
            assert get(srv, "/index.html", m)[0] in (405, 501), m
    finally:
        srv.shutdown()


if __name__ == "__main__":
    bad = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:      # noqa: BLE001
            bad += 1
            print("FAIL", t.__name__, repr(e))
    print("%d/%d passed" % (len(tests) - bad, len(tests)))
    sys.exit(1 if bad else 0)
