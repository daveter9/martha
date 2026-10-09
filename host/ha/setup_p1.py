#!/usr/bin/env python3
"""Set up the P1 meter (DSMR) in production Home Assistant, with a dashboard of graphs.

Run as root on martha, after onboarding and 'martha-ha setup-gate' (it uses the admin
token in /etc/martha/ha-root.env):

    sudo python3 /usr/local/lib/martha/setup_p1.py [--port /dev/ttyUSB0] [--interval 10]

Idempotent; each run:
  1. makes a backup (martha-ha backup before-p1);
  2. adds the DSMR integration (DSMR 5, serial port) if it is not there yet;
  3. sets the update interval (seconds between states written to the recorder and LTSS);
  4. enables every sensor of the meter, including those HA disables by default
     (voltage, current, power per phase, power failures, sags and swells);
  5. deploys packages/p1_gas.yaml (gas use per minute over 24 hours, a SQL sensor on
     ltss_1m) through martha-ha, with check_config, backup and automatic rollback;
  6. writes the dashboard 'P1-meter' (/p1-meter) with graphs of all those sensors and a
     tab with the gas table.

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

DASHBOARD = "p1-meter"
GAS_PACKAGE = "packages/p1_gas.yaml"
GAS_TEMPLATE = "/usr/local/lib/martha/p1_gas.yaml"   # host/ha/packages/p1_gas.yaml
GAS_TABLE = "sensor.gas_per_minuut_24_uur"           # from the name in p1_gas.yaml
STORAGE = m.CONFIG + "/packages/martha_storage.yaml"
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


def deploy_gas_table(gas_entity):
    """Put packages/p1_gas.yaml in production through martha-ha (sync, check_config,
    backup, restart, health check, automatic rollback)."""
    with open(STORAGE) as f:
        if f.read().count(GAS_TABLE) < 2:
            raise m.Error(f"exclude {GAS_TABLE} from the recorder and LTSS in {STORAGE} first "
                          "(see host/ha/packages/martha_storage.yaml)")
    with open(GAS_TEMPLATE) as f:
        content = f.read().replace("__GAS_ENTITY__", gas_entity)

    def build(base):
        with m.tempfile.TemporaryDirectory(dir=m.WORK) as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
            m.git("read-tree", base, env=env)
            blob = m.git("hash-object", "-w", "--stdin", input=content).stdout.strip()
            m.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{GAS_PACKAGE}", env=env)
            return m.git("write-tree", env=env).stdout.strip()

    with m.locked(wait=True):
        m.apply(build, "p1: gas use per minute (packages/p1_gas.yaml)")


def gas_view(table):
    """Markdown table; the frontend renders the ~1440 rows from the sensor's attribute."""
    content = (
        f"**Laatste 24 uur: {{{{ states('{table}') }}}} m³**\n\n"
        "De meter geeft de gasstand elke 5 minuten door, dus het verbruik van die 5 minuten "
        "staat in één minuut. Nieuwste minuut bovenaan; – betekent nog geen meetdata.\n\n"
        "| Minuut | Verbruik (m³) |\n|:--|--:|\n"
        f"{{{{ state_attr('{table}', 'tabel') }}}}"
    )
    return {"title": "Gas per minuut", "path": "gas", "icon": "mdi:fire",
            "cards": [{"type": "markdown", "content": content}]}


def dashboard(ids):
    """Lovelace config: only cards for sensors this meter actually has."""
    def pick(*keys):
        return [ids[k] for k in keys if k in ids]

    power = pick("current_electricity_usage", "current_electricity_delivery")
    phases = pick(*(f"instantaneous_active_power_l{n}_{d}"
                    for n in (1, 2, 3) for d in ("positive", "negative")))
    voltage = pick("instantaneous_voltage_l1", "instantaneous_voltage_l2", "instantaneous_voltage_l3")
    current = pick("instantaneous_current_l1", "instantaneous_current_l2", "instantaneous_current_l3")
    energy = pick("electricity_used_tariff_1", "electricity_used_tariff_2",
                  "electricity_delivered_tariff_1", "electricity_delivered_tariff_2")
    gas = pick("gas_meter_reading")
    events = pick("short_power_failure_count", "long_power_failure_count",
                  *(f"voltage_{t}_l{n}_count" for t in ("sag", "swell") for n in (1, 2, 3)))
    status = pick("electricity_active_tariff", "timestamp")

    def history(title, entities, hours=24):
        return {"type": "history-graph", "title": title, "hours_to_show": hours,
                "entities": entities}

    def statistics(title, entities, period, days, stat_types, chart_type="line"):
        return {"type": "statistics-graph", "title": title, "entities": entities,
                "period": period, "days_to_show": days, "stat_types": stat_types,
                "chart_type": chart_type}

    now = [{"type": "tile", "entity": e} for e in power + voltage + current + status]
    cards = [
        {"type": "grid", "columns": 2, "square": False, "cards": now},
        history("Vermogen (24 uur)", power),
        history("Vermogen per fase (24 uur)", phases),
        history("Spanning (24 uur)", voltage),
        history("Stroom (24 uur)", current),
        statistics("Vermogen per uur (7 dagen)", power, "hour", 7, ["mean", "min", "max"]),
        statistics("Spanning per uur (7 dagen)", voltage, "hour", 7, ["mean", "min", "max"]),
        statistics("Elektriciteit per dag (30 dagen)", energy, "day", 30, ["change"], "bar"),
        statistics("Elektriciteit per maand (1 jaar)", energy, "month", 365, ["change"], "bar"),
        statistics("Gas per dag (30 dagen)", gas, "day", 30, ["change"], "bar"),
        statistics("Gas per maand (1 jaar)", gas, "month", 365, ["change"], "bar"),
        {"type": "entities", "title": "Meterstanden", "entities": energy + gas},
        {"type": "entities", "title": "Storingen, dips en pieken", "entities": events},
        history("Storingen, dips en pieken (30 dagen)", events, 24 * 30),
    ]
    # A card without entities is an error in the frontend.
    cards = [c for c in cards if c.get("entities") or c.get("cards")]
    views = [{"title": "P1-meter", "path": "p1", "icon": "mdi:meter-electric", "cards": cards}]
    if gas:
        views.append(gas_view(GAS_TABLE))
    return {"title": "P1-meter", "views": views}


def write_dashboard(ws, ids):
    if not any(d["url_path"] == DASHBOARD for d in ws.call("lovelace/dashboards/list")):
        ws.call("lovelace/dashboards/create", url_path=DASHBOARD, title="P1-meter",
                icon="mdi:meter-electric", show_in_sidebar=True, require_admin=False,
                mode="storage")
    ws.call("lovelace/config/save", url_path=DASHBOARD, config=dashboard(ids))
    m.log(f"dashboard: {m.HA_URL}/{DASHBOARD}")


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
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'martha-ha setup-gate' first")

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
    finally:
        ws.close()
    m.log(f"{len(ids)} sensors: " + ", ".join(sorted(ids.values())))
    if "gas_meter_reading" in ids:
        deploy_gas_table(ids["gas_meter_reading"])
    ws = m.WebSocket(m.HA_URL, token)
    try:
        write_dashboard(ws, ids)
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"setup_p1: {e}")
