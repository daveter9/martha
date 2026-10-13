#!/usr/bin/env python3
"""Run Grott (proxy mode) for martha, with a local fallback when Growatt is unreachable.

The ShineLAN-X sends to martha:5279. Grott forwards each connection to Growatt, so the
Growatt app keeps working, and decodes every record (the extension martha_ha_ext posts it
to Home Assistant). Plain Grott closes the datalogger's connection when it cannot reach
Growatt, and then nothing is decoded at all (grottproxy.Proxy.on_accept). Here the forward
falls back to grottserver on 127.0.0.1:5781 (martha_grottserver.py), which acknowledges the
records like Growatt would, so data keeps coming in without internet (C1a).

When Growatt is reachable again, a watchdog ends the fallback connections; the datalogger
reconnects, and that new connection goes to Growatt again. What was sent during the
outage stays on martha only.

Usage (martha-grott.service): martha_grott.py /etc/martha/grott.ini
Grott itself comes from the offline bundle (offline/grott, see 1. download/download.ps1).
"""

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GROTT = os.environ.get("MARTHA_GROTT_DIR", "/usr/local/lib/martha/grott")
FALLBACK = (os.environ.get("MARTHA_GROTT_FALLBACK_HOST", "127.0.0.1"),
            int(os.environ.get("MARTHA_GROTT_FALLBACK_PORT", "5781")))
CONNECT_TIMEOUT = 5      # seconds per attempt; the datalogger waits meanwhile
WATCHDOG_INTERVAL = 600  # seconds between checks whether Growatt is back

# The stand-ins (libscrc, paho) and the extension come before Grott on the path.
sys.path[:0] = [os.path.join(HERE, "shims"), HERE, GROTT]

import grottproxy  # noqa: E402

fallback_socks = set()
lock = threading.Lock()


def log(msg):
    print(f"\t - martha-grott - {msg}", flush=True)


def connect(target):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(target)
    except OSError:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def start(self, host, port):
    """Replaces grottproxy.Forward.start: Growatt first, the local grottserver second."""
    self.forward.close()
    try:
        self.forward = connect((host, port))
        return self.forward
    except OSError as e:
        log(f"Growatt ({host}:{port}) unreachable: {e}; using the local grottserver")
    try:
        self.forward = connect(FALLBACK)
    except OSError as e:
        log(f"local grottserver {FALLBACK[0]}:{FALLBACK[1]} unreachable too: {e}")
        return False
    with lock:
        fallback_socks.add(self.forward)
    return self.forward


def watchdog(growatt):
    """End fallback connections once Growatt answers again. shutdown() makes Grott's
    select loop see the end of the stream and close both sides itself."""
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        with lock:
            fallback_socks.difference_update({s for s in fallback_socks if s.fileno() < 0})
            if not fallback_socks:
                continue
        try:
            connect(growatt).close()
        except OSError:
            continue
        with lock:
            socks = list(fallback_socks)
            fallback_socks.clear()
        log(f"Growatt reachable again; ending {len(socks)} fallback connection(s)")
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: martha_grott.py <grott.ini>")
    grottproxy.Forward.start = start
    from grottconf import Conf
    from grottproxy import Proxy
    sys.argv = [sys.argv[0], "-c", sys.argv[1]]
    with open(os.path.join(GROTT, "grott.py")) as f:
        verrel = next((line.split("=", 1)[1].strip().strip('"') for line in f
                       if line.startswith("verrel")), "unknown")
    conf = Conf(verrel)
    if conf.mode != "proxy":
        sys.exit(f"martha-grott runs Grott in proxy mode only, not '{conf.mode}'")
    if conf.verbose:
        conf.print()
    threading.Thread(target=watchdog, args=((conf.growattip, conf.growattport),),
                     daemon=True).start()
    proxy = Proxy(conf)   # binds the port; resolves grottip 'default' to 0.0.0.0
    log(f"Grott {verrel}: listening on {conf.grottip}:{conf.grottport}, forwarding to "
        f"{conf.growattip}:{conf.growattport}, fallback {FALLBACK[0]}:{FALLBACK[1]}")
    proxy.main(conf)


if __name__ == "__main__":
    main()
