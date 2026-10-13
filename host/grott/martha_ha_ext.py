"""Grott extension: send every decoded record to a Home Assistant webhook.

Configured in grott.ini:

    [extension]
    extension = True
    extname = martha_ha_ext
    extvar = {"url": "http://127.0.0.1:8123/api/webhook/<id>"}

jsonmsg is Grott's JSON text: {"device", "time", "buffered", "values": {...}} with raw
integer values (divide them as in the record layout; packages/solar_growatt.yaml does).
Standard library only, no requests.
"""

import urllib.request


def grottext(conf, data, jsonmsg):
    try:
        req = urllib.request.Request(conf.extvar["url"], data=jsonmsg.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return 0
    except Exception as e:  # Grott prints a non-zero result and goes on
        return e
