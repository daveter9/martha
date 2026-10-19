#!/usr/bin/env python3
"""The dashboards per domain (DESIGN.md, C25): Elektriciteit, Verwarming and Tapwater.

Every setup script (setup_p1, setup_solar, setup_heat) ends with write(), which rebuilds all
three from what is installed, so the order of the scripts does not matter. It can also be run
on its own, as root on martha:

    sudo python3 /usr/local/lib/martha/dashboards.py

The cards themselves are defined next to the sensors they show, in views() of each setup
script. A domain without any views is not created; the dashboards of the earlier layout
(per source: P1-meter, Zonnepanelen, Warmtepomp) are removed.
"""

import os
import sys

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402

# url_path (HA requires a hyphen), title, icon
DASHBOARDS = {
    "electricity": ("huis-elektriciteit", "Elektriciteit", "mdi:flash"),
    "heating": ("huis-verwarming", "Verwarming", "mdi:radiator"),
    "dhw": ("huis-tapwater", "Tapwater", "mdi:shower-head"),
}
OLD = ("p1-meter", "zonne-energie", "warmte-pomp")


def collect(ws, token):
    """The views per domain, from each part that is installed."""
    import setup_heat
    import setup_p1
    import setup_solar
    out = {k: [] for k in DASHBOARDS}

    entry = setup_p1.dsmr_entry(token)
    if entry:
        p1 = setup_p1.views(setup_p1.by_key(ws, entry))
        out["electricity"] += p1["electricity"]
        out["heating"] += p1["gas"]

    out["electricity"] += setup_solar.views(setup_solar.sql_entities(ws))

    try:
        rows = setup_heat.params()
    except m.Error:   # heat.sql not applied
        rows = []
    if rows:
        heat = setup_heat.views(setup_heat.heat_entities(ws, wait=0), rows)
        out["heating"] += heat["heating"]
        out["dhw"] += heat["dhw"]
    return out


def write(ws, token):
    views = collect(ws, token)
    existing = {d["url_path"]: d for d in ws.call("lovelace/dashboards/list")}
    for key, (path, title, icon) in DASHBOARDS.items():
        if not views[key]:
            continue
        if path not in existing:
            ws.call("lovelace/dashboards/create", url_path=path, title=title, icon=icon,
                    show_in_sidebar=True, require_admin=False, mode="storage")
        ws.call("lovelace/config/save", url_path=path,
                config={"title": title, "views": views[key]})
        m.log(f"dashboard {title}: {m.HA_URL}/{path} ({len(views[key])} tabs)")
    for path in OLD:
        if path in existing:
            ws.call("lovelace/dashboards/delete", dashboard_id=existing[path]["id"])
            m.log(f"removed old dashboard /{path}")


def main():
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'martha-ha setup-gate' first")
    ws = m.WebSocket(m.HA_URL, token)
    try:
        write(ws, token)
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"dashboards: {e}")
