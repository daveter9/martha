#!/usr/bin/env python3
"""Pair the Honeywell thermostat (Lyric T6, Apple HomeKit) with Home Assistant, locally over
Wi-Fi through the built-in integration HomeKit Device (homekit_controller), and show its
temperature and setpoint on the dashboard Verwarming.

Run as root on martha, after 'martha-ha token':

    sudo python3 /usr/local/lib/martha/setup_thermostat.py --code XXX-XX-XXX

The code is the HomeKit setup code on the sticker on the side of the thermostat; it is not
stored anywhere (the repository is public). The thermostat must not be paired with Apple Home:
a HomeKit accessory has one owner. Remove it there first, or reset its HomeKit pairing.

Idempotent; each run:
  1. makes a backup (martha-ha backup before-thermostat);
  2. pairs the thermostat if there is no HomeKit Device entry yet (needs --code then).
     HomeKit Device loads zeroconf (mDNS) to find it (DESIGN.md, C24);
  3. writes the dashboards per domain (host/ha/dashboards.py): one graph with the room
     temperature, the setpoint and gas for heating now (second axis) is on the overview of
     Verwarming.
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402
import dashboards  # noqa: E402

DOMAIN = "homekit_controller"
NAMES = ("tstat", "lyric", "honeywell", "t6")   # how the thermostat announces itself (Tstat-XXXXXX)


def entries(token):
    return m.ha_api("GET", f"/api/config/config_entries/entry?domain={DOMAIN}", token) or []


def pair(token, code, device=None):
    """Run the config flow: pick the thermostat among the unpaired HomeKit accessories, then
    pair with the setup code."""
    if not re.fullmatch(r"\d{3}-\d{2}-\d{3}", code or ""):
        raise m.Error("the setup code looks like 123-45-678 (on the sticker of the thermostat)")
    m.log("looking for unpaired HomeKit accessories (loads HomeKit Device and zeroconf)")
    # Right after zeroconf is loaded, or after the thermostat joined the network, HA may not
    # have its mDNS record cached yet ("no_devices"); try again a few times.
    for attempt in range(4):
        flow = m.ha_api("POST", "/api/config/config_entries/flow", token, {"handler": DOMAIN}, timeout=120)
        if flow.get("type") != "abort" or flow.get("reason") != "no_devices" or attempt == 3:
            break
        m.log("nothing found yet; trying again in 15 seconds")
        time.sleep(15)
    if flow.get("type") == "abort":
        raise m.Error(f"HomeKit Device found nothing to pair ({flow.get('reason')}); is the "
                      "thermostat removed from Apple Home and on the same network as martha?")
    schema = {f["name"]: f for f in flow.get("data_schema", [])}
    options = schema.get("device", {}).get("options", [])
    # The frontend gets the options as [[value, label], ...] or as {value: label}.
    if isinstance(options, dict):
        options = list(options.items())
    options = [tuple(o) if isinstance(o, (list, tuple)) else (o, o) for o in options]
    m.log("found: " + ", ".join(f"{label} ({value})" for value, label in options))
    if device:
        chosen = [o for o in options if device in (o[0], o[1])]
    else:
        chosen = [o for o in options if any(n in str(o[1]).lower() for n in NAMES)]
        if not chosen and len(options) == 1:
            chosen = options
    if len(chosen) != 1:
        m.ha_api("DELETE", f"/api/config/config_entries/flow/{flow['flow_id']}", token)
        raise m.Error("cannot tell which accessory is the thermostat; pass --device with one of "
                      "the ids above")
    value, label = chosen[0]
    m.log(f"pairing with {label}")
    step = m.ha_api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", token,
                    {"device": value}, timeout=60)
    if step.get("type") == "form" and step.get("step_id") == "pair":
        step = m.ha_api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", token,
                        {"pairing_code": code}, timeout=120)
    if step.get("type") != "create_entry":
        raise m.Error(f"pairing failed: {step.get('errors') or step.get('reason') or step}")
    m.log(f"paired: {step.get('title')}")


def climate_entity(ws, wait=60):
    """The thermostat's climate entity; it appears a moment after pairing."""
    deadline = time.monotonic() + wait
    while True:
        found = sorted(e["entity_id"] for e in ws.call("config/entity_registry/list")
                       if e.get("platform") == DOMAIN and e["entity_id"].startswith("climate."))
        if found or time.monotonic() > deadline:
            return found[0] if found else None
        time.sleep(2)


def extra(entity, gas=None, outdoor=None):
    """Cards under the table on the overview of Verwarming: the room temperature, the setpoint
    and the outdoor temperature (left axis) and gas for heating now (right axis), over the
    last hour, in our own card host/ha/www/martha-thermostat-graph.js."""
    if not entity:
        return []
    card = {"type": "custom:martha-thermostat-graph", "climate": entity, "hours": 1}
    if gas:
        card["gas"] = gas
    if outdoor:
        card["outdoor"] = outdoor
    return [card]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--code", help="HomeKit setup code on the sticker (only needed to pair)")
    p.add_argument("--device", help="id of the accessory, when more than one is found")
    a = p.parse_args()
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'sudo martha-ha token' first")

    m.backup("before-thermostat")
    if entries(token):
        m.log("HomeKit Device already set up")
    elif not a.code:
        raise m.Error("not paired yet: pass --code with the setup code on the thermostat")
    else:
        pair(token, a.code, a.device)
    ws = m.WebSocket(m.HA_URL, token)
    try:
        entity = climate_entity(ws)
        if not entity:
            raise m.Error("no climate entity of HomeKit Device found")
        state = next(s for s in ws.call("get_states") if s["entity_id"] == entity)
        attrs = state["attributes"]
        m.log(f"{entity}: {attrs.get('current_temperature')} °C, set to {attrs.get('temperature')} °C "
              f"({state['state']}, {attrs.get('hvac_action')})")
        dashboards.write(ws, token)
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"setup_thermostat: {e}")
