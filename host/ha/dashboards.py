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
import time

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402

# url_path (HA requires a hyphen), title, icon
DASHBOARDS = {
    "electricity": ("huis-elektriciteit", "Elektriciteit", "mdi:flash"),
    "heating": ("huis-verwarming", "Verwarming", "mdi:radiator"),
    "dhw": ("huis-tapwater", "Tapwater", "mdi:shower-head"),
}
OLD = ("p1-meter", "zonne-energie", "warmte-pomp")

# Every page starts with a title that says what is on it (C25), by dashboard and view path.
HEADINGS = {
    ("electricity", "meter"): ("Stroommeter", "Verbruik en teruglevering nu, spanning, stroom en de historie"),
    ("electricity", "zonnepanelen"): ("Zonnepanelen", "Wat de panelen opleveren: besparing, teruglevering en kosten"),
    ("electricity", "omvormer"): ("Omvormer", "Opwek, strings en het net volgens de Growatt"),
    ("electricity", "contract"): ("Stroomcontract", "Je vaste contract tegen een dynamisch contract"),
    ("electricity", "investering"): ("Investering zonnepanelen", "Terugverdienen: tot nu toe en de prognose"),
    ("electricity", "overschot"): ("Zonne-overschot", "Wat een batterij zou opleveren, en het overschot per uur"),
    ("electricity", "instellingen"): ("Instellingen stroom", "Stroomtarieven en de aannames van de zonnepanelen"),
    ("heating", "gas"): ("Gasmeter", "Gasverbruik per 5 minuten, per dag en per maand"),
    ("heating", "kosten"): ("Kosten verwarmen", "CV-ketel tegen warmtepomp, met contract- en marktprijs"),
    ("heating", "berekening"): ("Berekening", "Zo wordt één minuut uitgerekend, met de formules"),
    ("heating", "prognose"): ("Prognose per jaar", "Kosten met een gemiddeld jaar, en het warmteverlies van het huis"),
    ("heating", "aannames"): ("Aannames", "Waar de berekening op rust en wat je kunt verfijnen"),
    ("heating", "instellingen"): ("Instellingen verwarming", "Prijzen, huis en ketel, en de warmtepomp"),
    ("dhw", "tapwater"): ("Tapwater", "Gas en kosten voor warm water: CV-ketel tegen warmtepomp"),
}
# Built-in panels hidden from the sidebar (C24). They stay loaded: the dashboards need history
# (history graphs) and energy (period selection); logbook and media come with them.
HIDDEN_PANELS = ["home", "energy", "logbook", "history", "media-browser"]


def heading(title, subtitle):
    return {"type": "markdown", "text_only": True, "content": f"# {title}\n{subtitle}",
            "grid_options": {"columns": "full"}}


# The fixed layout of a page with use and costs over time (C25): today first, then ever longer
# ranges as you scroll down; details last. label, calendar period, graph period, days shown.
RANGES = [("Vandaag", "day", "hour", 1, "per uur"),
          ("Deze week", "week", "day", 7, "per dag"),
          ("Deze maand", "month", "day", 30, "per dag"),
          ("Dit jaar", "year", "month", 365, "per maand")]


def page(title, path, icon, usage=(), costs=(), today=(), details=()):
    """A sections view: Vandaag, Deze week, Deze maand, Dit jaar, Details. 'usage' and 'costs'
    are (entity_id, name) pairs of sensors with long-term statistics (state_class total or
    total_increasing); each range shows their change in that calendar period and a graph.
    'today' are extra cards at the top (what happens now), 'details' extra cards at the end.
    Returns None when there is nothing to show."""
    usage = [(e, n) for e, n in usage if e]
    costs = [(e, n) for e, n in costs if e]
    if not (usage or costs or today):
        return None

    def stat(entity, name, calendar):
        card = {"type": "statistic", "entity": entity, "stat_type": "change",
                "period": {"calendar": {"period": calendar}}}
        if name:
            card["name"] = name
        return card

    def graph(name, pairs, period, days):
        return {"type": "statistics-graph", "title": name, "chart_type": "bar",
                "entities": [{"entity": e, "name": n} if n else e for e, n in pairs],
                "stat_types": ["change"], "period": period, "days_to_show": days,
                "grid_options": {"columns": "full"}}

    sections = []
    for i, (label, calendar, period, days, per) in enumerate(RANGES):
        cards = [{"type": "heading", "heading": label}]
        if i == 0:
            cards += list(today)
        cards += [stat(e, n, calendar) for e, n in usage + costs]
        if usage:
            cards.append(graph(f"Gebruik {per}", usage, period, days))
        if costs:
            cards.append(graph(f"Kosten {per}", costs, period, days))
        sections.append({"type": "grid", "cards": cards})
    kept = []
    for c in details:
        if c and "entities" in c:
            c = {**c, "entities": [x for x in c["entities"]
                                   if x and (not isinstance(x, dict) or x.get("entity"))]}
        if c and (c.get("entities") or c.get("type") == "markdown" or c.get("entity")):
            kept.append(c)   # a card without entities is an error in the frontend
    details = kept
    if details:
        sections.append({"type": "grid", "cards": [{"type": "heading", "heading": "Details"}, *details]})
    return {"type": "sections", "max_columns": 4, "title": title, "path": path, "icon": icon,
            "sections": sections}


def collect(ws, token):
    """The views per domain, from each part that is installed."""
    import setup_heat
    import setup_p1
    import setup_solar
    out = {k: [] for k in DASHBOARDS}
    solar = setup_solar.sql_entities(ws)
    try:
        rows = setup_heat.params()
    except m.Error:   # heat.sql not applied
        rows = []
    heat = setup_heat.heat_entities(ws, wait=0) if rows else {}

    entry = setup_p1.dsmr_entry(token)
    if entry:
        # The meter pages show the costs the other parts calculate, when they are installed.
        p1 = setup_p1.views(setup_p1.by_key(ws, entry),
                            electricity_costs=[(solar.get("solar_total_cmp_fixed"), "Stroomkosten (netto)")],
                            gas_costs=[(heat.get("heat_total_cv"), "Gaskosten (met vaste kosten)")])
        out["electricity"] += p1["electricity"]
        out["heating"] += p1["gas"]

    out["electricity"] += setup_solar.views(solar)
    if rows:
        h = setup_heat.views(heat, rows)
        out["heating"] += h["heating"]
        out["dhw"] += h["dhw"]
    return out


def write(ws, token):
    views = collect(ws, token)
    existing = {d["url_path"]: d for d in ws.call("lovelace/dashboards/list")}
    for key, (path, title, icon) in DASHBOARDS.items():
        if not views[key]:
            continue
        for v in views[key]:
            top = heading(*HEADINGS.get((key, v["path"]), (v["title"], "")))
            if v.get("type") == "sections":
                v["sections"].insert(0, {"type": "grid", "column_span": 4, "cards": [top]})
            else:
                v["cards"].insert(0, top)
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
    shown = [DASHBOARDS[k] for k in DASHBOARDS if views[k]]
    write_sidebar(ws, [path for path, _, _ in shown])
    write_start(ws, shown)   # last: it may restart HA


def write_start(ws, dashboards):
    """The start screen: only a button per dashboard (C25), as the default panel for everyone
    instead of HA's generated home screen (weather, devices).

    Since HA 2026 the frontend only uses the default panel 'lovelace' when it is a dashboard
    entry with that url_path; otherwise it falls back to the home screen. HA turns the legacy
    default config into such an entry when it starts, so the first time this saves the legacy
    config and restarts HA; after that it writes to the entry. Call it last: a restart closes
    the websocket."""
    buttons = [{"type": "button", "name": title, "icon": icon, "show_state": False,
                "tap_action": {"action": "navigate", "navigation_path": f"/{path}"}}
               for path, title, icon in dashboards]
    config = {"title": "Thuis", "views": [
        {"title": "Thuis", "path": "thuis", "icon": "mdi:home",
         "cards": [heading("Thuis", "Kies een onderwerp"),
                   {"type": "grid", "columns": 3, "square": True, "cards": buttons}]}]}
    core = ws.call("frontend/get_system_data", key="core").get("value") or {}
    if core.get("default_panel") != "lovelace":
        ws.call("frontend/set_system_data", key="core", value={**core, "default_panel": "lovelace"})
        m.log("default panel: the start screen (/lovelace) instead of HA's home screen")
    entry = next((d for d in ws.call("lovelace/dashboards/list") if d["url_path"] == "lovelace"), None)
    if entry:
        ws.call("lovelace/config/save", url_path="lovelace", config=config)
        if entry.get("title") != "Thuis" or entry.get("icon") != "mdi:home":
            ws.call("lovelace/dashboards/update", dashboard_id=entry["id"], title="Thuis", icon="mdi:home")
        m.log(f"start screen: {len(buttons)} buttons")
        return
    ws.call("lovelace/config/save", config=config)
    m.log("start screen saved; restarting Home Assistant so it becomes the dashboard 'lovelace'")
    since = time.time()
    m.compose("restart")
    ok, msg = m.wait_healthy(since)
    if not ok:
        raise m.Error(f"Home Assistant unhealthy after restart: {msg}")
    token = m.read_env(m.ROOT_ENV)["HA_TOKEN"]
    ws2 = m.WebSocket(m.HA_URL, token)
    try:
        entry = next((d for d in ws2.call("lovelace/dashboards/list") if d["url_path"] == "lovelace"), None)
        if not entry:
            raise m.Error("HA did not create the dashboard 'lovelace' from the start screen")
        ws2.call("lovelace/dashboards/update", dashboard_id=entry["id"], title="Thuis", icon="mdi:home")
    finally:
        ws2.close()
    m.log(f"start screen: {len(buttons)} buttons")


def write_sidebar(ws, paths):
    """Sidebar of the token's user (the owner): the start screen and the dashboards, without
    HA's built-in panels. Sidebar settings are per user in HA; another user sets them under
    Profiel or by long-pressing the sidebar."""
    ws.call("frontend/set_user_data", key="sidebar",
            value={"panelOrder": ["lovelace", *paths], "hiddenPanels": HIDDEN_PANELS})
    m.log("sidebar: " + ", ".join(["lovelace", *paths]) + "; hidden: " + ", ".join(HIDDEN_PANELS))


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
