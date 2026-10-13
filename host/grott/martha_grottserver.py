#!/usr/bin/env python3
"""Run Grott's grottserver on localhost only, as the fallback of martha_grott.py.

grottserver emulates the Growatt server: it acknowledges the datalogger's records, so the
datalogger keeps sending when Growatt is unreachable. Its upstream grottserver.py binds
0.0.0.0, including the HTTP API (port 5782) that can write inverter registers; here both
listen on 127.0.0.1 only. This replaces grottserver's own __main__ block (same objects:
the module-level globals send_queuereg, loggerreg and commandresponse).

Usage (martha-grottserver.service): martha_grottserver.py
"""

import os
import sys
import threading
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GROTT = os.environ.get("MARTHA_GROTT_DIR", "/usr/local/lib/martha/grott")
HOST = "127.0.0.1"
DEVICE_PORT = 5781   # martha_grott.py FALLBACK
HTTP_PORT = 5782     # register read/write API, for steering later (doel 6)

sys.path[:0] = [os.path.join(HERE, "shims"), GROTT]

import grottserver as gs  # noqa: E402


def main():
    gs.verbose = os.environ.get("MARTHA_GROTT_VERBOSE") == "1"
    gs.send_queuereg = {}
    gs.loggerreg = {}
    gs.commandresponse = defaultdict(dict)
    print(f"\t - martha-grottserver - grottserver {gs.verrel} on {HOST}:{DEVICE_PORT} "
          f"(API {HOST}:{HTTP_PORT})", flush=True)
    http_server = gs.GrottHttpServer(HOST, HTTP_PORT, gs.send_queuereg)
    device_server = gs.sendrecvserver(HOST, DEVICE_PORT, gs.send_queuereg)
    threads = [threading.Thread(target=s.run, daemon=True) for s in (http_server, device_server)]
    for t in threads:
        t.start()
    # A server thread that dies ends the process, so systemd restarts it.
    while all(t.is_alive() for t in threads):
        time.sleep(5)
    sys.exit("martha-grottserver: a server thread stopped")


if __name__ == "__main__":
    main()
