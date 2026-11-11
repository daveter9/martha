#!/usr/bin/env python3
"""Set up the P1 meter (DSMR) in production Home Assistant, with a dashboard of graphs.

Run as root on martha, after onboarding and 'martha-ha token' (it uses the admin token
in /etc/martha/ha-root.env):

    sudo python3 /usr/local/lib/martha/setup_p1.py [--port /dev/ttyUSB0] [--interval 10]

Idempotent; each run:
  1. makes a backup (martha-ha backup before-p1);
  2. adds the DSMR integration (DSMR 5, serial port) if it is not there yet;
  3. sets the update interval (seconds between states written to the recorder and LTSS);
  4. enables every sensor of the meter, including those HA disables by default
     (voltage, current, power per phase, power failures, sags and swells);
  5. writes the dashboards per domain (host/ha/dashboards.py): electricity on
     Elektriciteit, gas on Verwarming.

Storage is not configured here: LTSS stores every sensor.* state, and ltss_1m keeps a
row per minute forever (host/db/timescale.sql).
"""

import argparse
import glob
import os
import sys
import time

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402
import dashboards  # noqa: E402

DSMR_VERSION = "5"   # Landis+Gyr E360: DSMR 5.0 (1-3:0.2.8(50))


def find_port():
    """The tty of the only USB serial adapter; HA's container has no /dev/serial/by-id."""
    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if len(ports) != 1:
        raise m.Error(f"expected one USB serial adapter, found {len(ports)}; pass --port")
    return os.path.realpath(ports[0])


def dsmr_entry(token):
    entries = m.ha_api("GET", "/api/config/config_entries/entry?domain=dsmr", token)
    return entries[0] if entries else None


def add_integration(token, port):
    entry = dsmr_entry(token)
    if entry:
        m.log(f"DSMR integration already present ({entry['title']})")
        return entry
    m.log(f"adding DSMR integration on {port}, DSMR {DSMR_VERSION}")
    flow = m.ha_api("POST", "/api/config/config_entries/flow", token, {"handler": "dsmr"})
    # Validation reads a telegram from the meter; it gives up after 30 seconds.
    result = m.ha_api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", token,
                      {"port": port, "dsmr_version": DSMR_VERSION}, timeout=90)
    if result.get("type") != "create_entry":
        raise m.Error(f"DSMR config flow failed: {result.get('errors') or result}")
    return dsmr_entry(token)


def set_interval(token, entry, seconds):
    """True when the interval changed. The REST API does not return an entry's options,
    so the current value is read from the form's default."""
    flow = m.ha_api("POST", "/api/config/config_entries/options/flow", token,
                    {"handler": entry["entry_id"]})
    current = {f["name"]: f.get("default") for f in flow.get("data_schema", [])}
    if current.get("time_between_update") == seconds:
        m.ha_api("DELETE", f"/api/config/config_entries/options/flow/{flow['flow_id']}", token)
        m.log(f"update interval already {seconds} s")
        return False
    result = m.ha_api("POST", f"/api/config/config_entries/options/flow/{flow['flow_id']}",
                      token, {"time_between_update": seconds})
    if result.get("type") != "create_entry":
        raise m.Error(f"DSMR options flow failed: {result.get('errors') or result}")
    m.log(f"update interval: {seconds} s")
    return True


def entities(ws, entry, wait=60):
    """Registry entries of the meter. DSMR creates them with the first telegram after
    a (re)load, so wait until they exist."""
    deadline = time.monotonic() + wait
    while True:
        found = [e for e in ws.call("config/entity_registry/list")
                 if e.get("config_entry_id") == entry["entry_id"]]
        if found or time.monotonic() > deadline:
            return found
        time.sleep(2)


def enable_all(ws, entry):
    """True when sensors were enabled."""
    disabled = [e for e in entities(ws, entry) if e.get("disabled_by")]
    for e in disabled:
        ws.call("config/entity_registry/update", entity_id=e["entity_id"], disabled_by=None)
    if disabled:
        m.log(f"enabled {len(disabled)} sensors")
    return bool(disabled)


def restart():
    """Restart HA instead of reloading the entry. An options change and enabling entities
    each schedule a reload; overlapping DSMR reloads leave the old entities registered
    ("unique ID ... already exists"), and those sensors stop updating."""
    since = time.time()
    m.compose("restart")
    ok, msg = m.wait_healthy(since)
    if not ok:
        raise m.Error(f"Home Assistant unhealthy after restart: {msg}")


def by_key(ws, entry):
    """Entity ids by DSMR sensor key: the unique_id is '<serial>_<key>', except for the
    gas meter (M-Bus), whose unique_id is its serial; that one has a translation_key."""
    found = {}
    for e in entities(ws, entry):
        serial, _, key = e["unique_id"].partition("_")
        key = key or e.get("translation_key") or serial
        found[key] = e["entity_id"]
    return found


# Short names on the dashboards (DSMR names every sensor "Electricity Meter ...").
NAMES = {
    "current_electricity_usage": "Verbruik nu",
    "current_electricity_delivery": "Teruglevering nu",
    "electricity_used_tariff_1": "Verbruik dal",
    "electricity_used_tariff_2": "Verbruik normaal",
    "electricity_delivered_tariff_1": "Teruglevering dal",
    "electricity_delivered_tariff_2": "Teruglevering normaal",
    "electricity_active_tariff": "Tarief nu",
    "timestamp": "Laatste meting",
    "gas_meter_reading": "Gas",
    **{f"instantaneous_voltage_l{n}": f"Spanning L{n}" for n in (1, 2, 3)},
    **{f"instantaneous_current_l{n}": f"Stroom L{n}" for n in (1, 2, 3)},
}


def views(ids, electricity_costs=(), gas_costs=(), use_total=None):
    """The P1 pages: Gebruik woning for the dashboard Elektriciteit, Gas (in the fixed layout
    of dashboards.page) for Verwarming. The costs come from the other parts when installed.
    Only cards for sensors this meter actually has. 'use_total' is the use of the house
    wherever it comes from (solar_total_use), for the use per hour."""
    def named(*keys):
        return [(ids[k], NAMES.get(k)) for k in keys if k in ids]

    power = named("current_electricity_usage", "current_electricity_delivery")
    # Use as one bar per period: low and normal tariff stacked.
    used = [(ids[k], n) for k, n in (("electricity_used_tariff_1", "Dal"),
                                     ("electricity_used_tariff_2", "Normaal")) if k in ids]
    costs = [(e, n) for e, n in electricity_costs if e]
    gas = named("gas_meter_reading")

    def ents(pairs):
        return [{"entity": e, "name": n} if n else e for e, n in pairs]

    def history(title, entities, hours=24):
        return {"type": "history-graph", "title": title, "hours_to_show": hours,
                "entities": entities, "grid_options": {"columns": "full"}}

    def statistics(title, entities, period, days, stat_types, chart_type="line"):
        return {"type": "statistics-graph", "title": title, "entities": entities,
                "period": period, "days_to_show": days, "stat_types": stat_types,
                "chart_type": chart_type, "grid_options": {"columns": "full"}}

    # Gebruik woning (user's request, 2026-10-30): only the power over 24 hours, and use and
    # costs per hour, day, week, month and year. label, period, days shown.
    sections = [{"type": "grid", "cards": [history("Vermogen (24 uur)", ents(power))]}] if power else []
    for label, period, days in (("uur", "hour", 1), ("dag", "day", 30), ("week", "week", 26 * 7),
                                ("maand", "month", 365), ("jaar", "year", 5 * 365)):
        cards = [{"type": "heading", "heading": f"Per {label}"}]
        # Per hour one bar, without low and normal tariff (user's request, 2026-11-11).
        if period == "hour" and use_total:
            cards.append(statistics(f"Gebruik per {label}", [{"entity": use_total, "name": "Gebruik"}],
                                    period, days, ["change"], "bar"))
        elif used:
            cards.append(statistics(f"Gebruik per {label}", ents(used), period, days,
                                    ["change"], "bar-stack"))
        if costs:
            cards.append(statistics(f"Kosten per {label}", ents(costs), period, days,
                                    ["change"], "bar"))
        sections.append({"type": "grid", "cards": cards})
    home = {"type": "sections", "max_columns": 4, "title": "Gebruik woning", "path": "gebruik-woning",
            "icon": "mdi:home-lightning-bolt", "sections": sections}
    gas_page = dashboards.page(
        "Gas", "gas", "mdi:meter-gas", usage=gas, costs=gas_costs,
        # The meter reports gas every 5 minutes; HA's 5-minute statistics give the use
        # per interval (kept as long as the recorder's purge_keep_days).
        today=[statistics("Gas per 5 minuten (24 uur)", ents(gas), "5minute", 1, ["change"])] if gas else [],
        details=[{"type": "entities", "title": "Meterstand", "entities": ents(gas)}])
    return {"electricity": [home] if used else [],
            "gas": [gas_page] if gas_page and gas else []}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", help="serial port of the P1 cable (default: the only USB serial adapter)")
    p.add_argument("--interval", type=int, default=10,
                   help="seconds between updates (default 10; the meter sends every second)")
    a = p.parse_args()
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'sudo martha-ha token' first")

    m.backup("before-p1")
    entry = add_integration(token, a.port or find_port())
    ws = m.WebSocket(m.HA_URL, token)
    try:
        changed = enable_all(ws, entry)
    finally:
        ws.close()
    if set_interval(token, entry, a.interval) or changed:
        restart()
    ws = m.WebSocket(m.HA_URL, token)
    try:
        ids = by_key(ws, entry)
        m.log(f"{len(ids)} sensors: " + ", ".join(sorted(ids.values())))
        dashboards.write(ws, token)
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"setup_p1: {e}")
