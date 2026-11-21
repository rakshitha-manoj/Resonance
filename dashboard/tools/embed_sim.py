#!/usr/bin/env python
"""Embeds dashboard/demo_events_sim.jsonl into dashboard/index.html as <script type="text/plain" id="sim-data"> (the offline copy that
"Damage demo (simulated)" and ?replay=demo_events_sim.jsonl use when the browser cannot fetch files from file://).
Run after every regeneration of demo_events_sim.jsonl:   .venv/Scripts/python.exe dashboard/tools/embed_sim.py [--check]
--check only verifies that the embedded copy equals the file (exit 1 if not)."""
import re
import sys
from pathlib import Path

D = Path(__file__).resolve().parents[1]
PAGE = D / "index.html"
LOG = D / "demo_events_sim.jsonl"
BLOCK = re.compile(r'(<script type="text/plain" id="sim-data">)(.*?)(</script>)', re.S)


def main():
    page = PAGE.read_text(encoding="utf-8")
    log = LOG.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "</script" not in log
    m = BLOCK.search(page)
    if "--check" in sys.argv:
        ok = bool(m) and m.group(2).lstrip("\n") == log
        print("embedded copy %s demo_events_sim.jsonl" % ("equals" if ok else "DIFFERS from"))
        sys.exit(0 if ok else 1)
    block = '<script type="text/plain" id="sim-data">\n' + log + '</script>'
    if m:
        page = page[:m.start()] + block + page[m.end():]
    else:
        anchor = '<script src="vendor/mqtt.min.js"></script>'
        assert anchor in page
        page = page.replace(anchor, block + "\n" + anchor, 1)
    PAGE.write_text(page, encoding="utf-8", newline="\n")
    print("embedded %d lines (%d bytes) into %s" % (log.count("\n"), len(log), PAGE))


if __name__ == "__main__":
    main()
