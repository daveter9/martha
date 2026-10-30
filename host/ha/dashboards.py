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

import copy
import hashlib
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

HEADINGS_GAS = ("Gasmeter", "Gasverbruik per 5 minuten, per dag en per maand")
# Every page starts with a title that says what is on it (C25), by dashboard and view path.
HEADINGS = {
    ("electricity", "gebruik-woning"): ("Gebruik woning", "Vermogen, en gebruik en kosten per uur, dag, week, maand en jaar"),
    ("electricity", "zonnepanelen"): ("Zonnepanelen", "Wat de panelen opleveren: besparing, teruglevering en kosten"),
    ("electricity", "omvormer"): ("Omvormer", "Opwek, strings en het net volgens de Growatt"),
    ("electricity", "contract"): ("Stroomcontract", "Je vaste contract tegen een dynamisch contract"),
    ("electricity", "investering"): ("Investering zonnepanelen", "Terugverdienen: tot nu toe en de prognose"),
    ("electricity", "overschot"): ("Zonne-overschot", "Wat een batterij zou opleveren, en het overschot per uur"),
    ("electricity", "instellingen"): ("Instellingen stroom", "Stroomtarieven en de aannames van de zonnepanelen"),
    ("heating", "gas"): HEADINGS_GAS,
    ("heating", "kosten"): ("Kosten verwarmen", "CV-ketel tegen warmtepomp, met contract- en marktprijs"),
    ("heating", "berekening"): ("Berekening", "Zo wordt één minuut uitgerekend, met de formules"),
    ("heating", "prognose"): ("Prognose per jaar", "Kosten met een gemiddeld jaar, en het warmteverlies van het huis"),
    ("heating", "aannames"): ("Aannames", "Waar de berekening op rust en wat je kunt verfijnen"),
    ("heating", "instellingen"): ("Instellingen verwarming", "Prijzen, huis en ketel, en de warmtepomp"),
    ("dhw", "tapwater"): ("Tapwater", "Gas en kosten voor warm water: CV-ketel tegen warmtepomp"),
    ("dhw", "gas"): HEADINGS_GAS,
}
# Built-in panels hidden from the sidebar (C24). They stay loaded: the dashboards need history
# (history graphs) and energy (period selection); logbook and media come with them.
HIDDEN_PANELS = ["home", "energy", "logbook", "history", "media-browser"]


def heading(title, subtitle=""):
    content = f"# {title}\n{subtitle}" if subtitle else f"# {title}"
    return {"type": "markdown", "text_only": True, "content": content,
            "grid_options": {"columns": "full"}}


# The fixed layout of a page with use and costs over time (C25): today first, then ever longer
# ranges as you scroll down; details last. label, calendar period, graph period, days shown.
RANGES = [("Deze dag", "day", "hour", 1, "per uur"),
          ("Deze week", "week", "day", 7, "per dag"),
          ("Deze maand", "month", "day", 30, "per dag"),
          ("Dit jaar", "year", "month", 365, "per maand")]


def page(title, path, icon, usage=(), costs=(), today=(), details=()):
    """A sections view: Deze dag, Deze week, Deze maand, Dit jaar, Details. 'usage' and 'costs'
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

    p1_ids = {}
    entry = setup_p1.dsmr_entry(token)
    if entry:
        p1_ids = setup_p1.by_key(ws, entry)
        # The meter pages show the costs the other parts calculate, when they are installed.
        p1 = setup_p1.views(p1_ids,
                            electricity_costs=[(solar.get("solar_total_cmp_fixed"), "Stroomkosten (netto)")],
                            gas_costs=[(heat.get("heat_total_cv"), "Gaskosten (met vaste kosten)")])
        out["electricity"] += p1["electricity"]
        out["heating"] += p1["gas"]

    out["electricity"] += setup_solar.views(solar)
    if rows:
        h = setup_heat.views(heat, rows)
        out["heating"] += h["heating"]
        out["dhw"] += h["dhw"]
    # Start screen and overviews: use and costs now and today (heat_home in heat.sql).
    gas_meter = p1_ids.get("gas_meter_reading")
    home = {"electricity": [(p1_ids.get("current_electricity_usage"), "Nu"),
                            (p1_ids.get("current_electricity_usage"), "_age:last_reported"),
                            (heat.get("heat_home_elec_today"), "Vandaag"),
                            (heat.get("heat_home_elec_cost_now"), "Kosten nu"),
                            (heat.get("heat_home_elec_cost_today"), "Kosten vandaag"),
                            # Week and month: only on the overviews, not on the start screen.
                            (heat.get("heat_home_elec_week"), "Deze week"),
                            (heat.get("heat_home_elec_cost_week"), "Kosten deze week"),
                            (heat.get("heat_home_elec_month"), "Deze maand"),
                            (heat.get("heat_home_elec_cost_month"), "Kosten deze maand")],
            "heating": [(heat.get("heat_home_space_now"), "Nu"),
                        (gas_meter, "_age:last_changed"),
                        (heat.get("heat_home_space_today"), "Vandaag"),
                        (heat.get("heat_home_space_cost_now"), "Kosten nu"),
                        (heat.get("heat_home_space_cost_today"), "Kosten vandaag"),
                        (heat.get("heat_home_space_week"), "Deze week"),
                        (heat.get("heat_home_space_cost_week"), "Kosten deze week"),
                        (heat.get("heat_home_space_month"), "Deze maand"),
                        (heat.get("heat_home_space_cost_month"), "Kosten deze maand")],
            "dhw": [(heat.get("heat_home_dhw_now"), "Nu"),
                    (gas_meter, "_age:last_changed"),
                    (heat.get("heat_home_dhw_today"), "Vandaag"),
                    (heat.get("heat_home_dhw_cost_now"), "Kosten nu"),
                    (heat.get("heat_home_dhw_cost_today"), "Kosten vandaag"),
                    (heat.get("heat_home_dhw_week"), "Deze week"),
                    (heat.get("heat_home_dhw_cost_week"), "Kosten deze week"),
                    (heat.get("heat_home_dhw_month"), "Deze maand"),
                    (heat.get("heat_home_dhw_cost_month"), "Kosten deze maand")]}
    return out, home, (p1_ids, solar, heat)


def write(ws, token):
    views, home, (p1_ids, solar_ids, heat_ids) = collect(ws, token)
    write_energy(ws, p1_ids, solar_ids)
    existing = {d["url_path"]: d for d in ws.call("lovelace/dashboards/list")}
    # Under the blocks of Elektriciteit: where the power comes from and goes to, now. Our own
    # card: HA's power-sankey always shows "untracked consumption" (C25).
    extra = {}
    if "current_electricity_usage" in p1_ids and write_resource(ws, "martha-power-flow.js"):
        card = {"type": "custom:martha-power-flow", "grid_import": p1_ids["current_electricity_usage"]}
        if p1_ids.get("current_electricity_delivery"):
            card["grid_export"] = p1_ids["current_electricity_delivery"]
        if solar_ids.get("solar_growatt_power"):
            card["solar"] = solar_ids["solar_growatt_power"]
        extra["electricity"] = [card]
    # Under the table of Verwarming: room temperature and setpoint of the thermostat, with gas
    # for heating now on a second axis. Our own card: HA's history-graph has one axis.
    import setup_thermostat
    climate = setup_thermostat.climate_entity(ws, wait=0)
    if climate and write_resource(ws, "martha-thermostat-graph.js"):
        extra["heating"] = setup_thermostat.extra(climate, heat_ids.get("heat_home_space_now"))
    # The page Gasmeter: first the last hour per meter report, split into heating and hot water
    # as the detection chose; the page is on Tapwater too (user's request, 2026-10-30).
    # A tap on it opens the page Gas verdeeld with the graph large, to zoom and move through
    # time (gas_split_page).
    gas_page = next((v for v in views["heating"] if v["path"] == "gas"), None)
    split = None
    if gas_page:
        if heat_ids.get("heat_gas_recent") and write_resource(ws, "martha-gas-split.js"):
            split = {"type": "custom:martha-gas-split", "entity": heat_ids["heat_gas_recent"], "hours": 1}
        if views["dhw"]:
            views["dhw"].append(copy.deepcopy(gas_page))
        for key in ("heating", "dhw"):
            page_ = next((v for v in views[key] if v["path"] == "gas"), None)
            if split and page_:
                page_["sections"][0]["cards"].insert(1, {
                    **split, "navigation_path": f"/{DASHBOARDS[key][0]}/gas-verdeeld"})
    for key, (path, title, icon) in DASHBOARDS.items():
        if not views[key]:
            continue
        pages = views[key]
        # Progressive disclosure: an overview, the pages as subviews that go back to it.
        first = overview(path, title, pages, home.get(key, []), extra.get(key, []),
                         TABLES.get(key))
        views[key] = [titled(first, (title, ""), START)] + [
            {**titled(v, HEADINGS.get((key, v["path"]), (v["title"], "")), f"/{path}/overzicht"),
             "subview": True, "back_path": f"/{path}/overzicht"} for v in pages]
        if split and any(v["path"] == "gas" for v in pages):
            views[key].append(gas_split_page(split, path))
        if path not in existing:
            ws.call("lovelace/dashboards/create", url_path=path, title=title, icon=icon,
                    show_in_sidebar=True, require_admin=False, mode="storage")
        ws.call("lovelace/config/save", url_path=path,
                config={"title": title, "views": views[key]})
        m.log(f"dashboard {title}: {m.HA_URL}/{path} ({len(views[key])} pages)")
    for path in OLD:
        if path in existing:
            ws.call("lovelace/dashboards/delete", dashboard_id=existing[path]["id"])
            m.log(f"removed old dashboard /{path}")
    shown = [k for k in DASHBOARDS if views[k]]
    write_sidebar(ws, [DASHBOARDS[k][0] for k in shown])
    write_start(ws, [(*DASHBOARDS[k], home.get(k, [])) for k in shown])   # last: it may restart HA


def gas_split_page(card, path):
    """The gas per meter report, heating and hot water, large and interactive; a subview of
    the page Gas (no button on the overview)."""
    view = {"title": "Gas verdeeld", "path": "gas-verdeeld", "icon": "mdi:chart-areaspline",
            "type": "sections", "max_columns": 4,
            "sections": [{"type": "grid", "column_span": 4,
                          "cards": [{**card, "height": 480, "grid_options": {"columns": "full"}}]}]}
    back = f"/{path}/gas"
    return {**titled(view, ("Gas verdeeld", "Per melding van de gasmeter: verwarming en tapwater. "
                            "Zoom met scrollen of twee vingers, sleep om door de tijd te gaan."), back),
            "subview": True, "back_path": back}


CARDS = m.CONFIG + "/www/martha/"   # our own cards, installed by install.sh


def write_resource(ws, name):
    """Our own card 'name' as a Lovelace resource, with a hash of the file in the URL so
    browsers load a new version. Returns False when install.sh has not installed the card."""
    try:
        with open(CARDS + name, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return False
    url = f"/local/martha/{name}?v={digest}"
    resources = ws.call("lovelace/resources")
    mine = [r for r in resources if r["url"].split("?")[0] == f"/local/martha/{name}"]
    if any(r["url"] == url for r in mine):
        return True
    for r in mine:
        ws.call("lovelace/resources/delete", resource_id=r["id"])
    ws.call("lovelace/resources/create", res_type="module", url=url)
    m.log(f"lovelace resource: {url}")
    return True


def write_energy(ws, p1, solar):
    """HA's energy settings (C25): the grid per tariff
    from the P1 meter, with the power now on the first, and the solar panels once the
    inverter is connected (setup_solar.py --inverter). Written only when it changed."""
    if "electricity_used_tariff_1" not in p1:
        return
    sources = []
    for n, power in ((1, True), (2, False)):
        src = {"type": "grid", "stat_energy_from": p1.get(f"electricity_used_tariff_{n}"),
               "stat_energy_to": p1.get(f"electricity_delivered_tariff_{n}"),
               "stat_cost": None, "entity_energy_price": None, "number_energy_price": None,
               "stat_compensation": None, "entity_energy_price_export": None,
               "number_energy_price_export": None, "cost_adjustment_day": 0.0,
               "name": "Net dal" if n == 1 else "Net normaal"}
        if power and "current_electricity_usage" in p1:
            src["power_config"] = {"stat_rate_from": p1["current_electricity_usage"],
                                   "stat_rate_to": p1.get("current_electricity_delivery")
                                   or p1["current_electricity_usage"]}
        sources.append(src)
    if solar.get("solar_growatt_energy_total"):
        src = {"type": "solar", "stat_energy_from": solar["solar_growatt_energy_total"],
               "config_entry_solar_forecast": None, "name": "Zonnepanelen"}
        if solar.get("solar_growatt_power"):
            src["stat_rate"] = solar["solar_growatt_power"]
        sources.append(src)
    try:
        current = ws.call("energy/get_prefs").get("energy_sources")
    except m.Error:   # no energy settings yet
        current = None
    wanted = [{k: v for k, v in x.items()} for x in sources]

    def key(srcs):
        return [{k: x.get(k) for k in ("type", "stat_energy_from", "stat_energy_to", "name")}
                for x in srcs or []]
    if key(current) != key(wanted):
        ws.call("energy/save_prefs", energy_sources=wanted)
        m.log(f"energy settings: {len(wanted)} sources (grid per tariff" + (", solar)" if len(wanted) > 2 else ")"))


def overview(path, title, subviews, values, extra=(), table=None):
    """Progressive disclosure (C25): a dashboard opens on an overview with its use now and
    today and a button per page; the pages are subviews (no tab bar, a back arrow). With
    'table' (the unit labels of the use and the costs row) the use is a table instead of
    blocks (now_table)."""
    buttons = [{"type": "button", "name": v["title"], "icon": v.get("icon"), "show_state": False,
                "tap_action": {"action": "navigate", "navigation_path": f"/{path}/{v['path']}"}}
               for v in subviews]
    top = now_table(values, *table) if table else now_today(values, horizontal=True)
    cards = [top, *extra,
             {"type": "grid", "columns": 3, "square": False, "cards": buttons}]
    return {"title": title, "path": "overzicht", "icon": "mdi:view-dashboard",
            "cards": [c for c in cards if c]}


START = "/lovelace/thuis"


def titled(view, head, back):
    """The view as a sections view whose first row is a back button next to the title and
    subtitle (C25). A masonry view's cards become one full-width section."""
    top = {"type": "grid", "column_span": 4, "cards": [
        {"type": "button", "icon": "mdi:arrow-left", "show_name": False, "show_state": False,
         "tap_action": {"action": "navigate", "navigation_path": back},
         "grid_options": {"columns": 2, "rows": 1}},
        {**heading(*head), "grid_options": {"columns": 10}},
    ]}
    if view.get("type") == "sections":
        sections = view["sections"]
    else:
        sections = [{"type": "grid", "column_span": 4,
                     "cards": [{**c, "grid_options": {"columns": "full"}} for c in view["cards"]]}]
    keep = {k: view[k] for k in ("title", "path", "icon") if k in view}
    return {**keep, "type": "sections", "max_columns": 4, "sections": [top, *sections]}


def value(entity):
    """Jinja for a value with its unit, with a decimal comma; euros as '1,23€' or '0,12€/u'."""
    return ("{%% set u = state_attr('%(e)s', 'unit_of_measurement') %%}"
            "{%% set v = states('%(e)s') %%}"
            "{%% if v in ['unknown', 'unavailable'] %%}–"
            "{%% elif u in ['EUR', 'EUR/h', '€', '€/u'] %%}{{ ('%%.2f' %% (v | float(0))) | replace('.', ',') }}€{{ '/u' if u in ['EUR/h', '€/u'] else '' }}"
            "{%% elif v | is_number %%}{{ ('%%.2f' %% (v | float)) | replace('.', ',') }} {{ u | replace('/h', '/u') }}"
            "{%% else %%}{{ v }} {{ u }}{%% endif %%}") % {"e": entity}


def number(entity):
    """Jinja for a value without its unit: two decimals with a decimal comma, or '–'."""
    return ("{%% set v = states('%(e)s') %%}"
            "{%% if v | is_number %%}{{ ('%%.2f' %% (v | float)) | replace('.', ',') }}"
            "{%% else %%}–{%% endif %%}") % {"e": entity}


def ago(entity, attribute):
    """Jinja: how long ago a state was reported or changed, in seconds, whole minutes or
    whole hours. It re-renders every minute (now()) and with every state change."""
    return ("{%% set t = states.%(e)s.%(a)s if states.%(e)s else none %%}"
            "{%% if t %%}{%% set s = ((now() - t).total_seconds()) | int %%}"
            "{%% if s < 60 %%}{{ s }} s geleden"
            "{%% elif s < 7200 %%}{{ s // 60 }} min geleden"
            "{%% else %%}{{ s // 3600 }} uur geleden{%% endif %%}{%% endif %%}") % {"e": entity, "a": attribute}


# The blocks of now_today(): label, then the names of the use and the costs in 'values'.
BLOCKS = [("Nu", "Nu", "Kosten nu"), ("Dag", "Vandaag", "Kosten vandaag"),
          ("Week", "Deze week", "Kosten deze week"),
          ("Maand", "Deze maand", "Kosten deze maand")]


# Overviews that show the use as a table, with the unit labels of the use and the costs row.
TABLES = {"electricity": ("kWh", "€"), "heating": ("m³", "€"), "dhw": ("m³", "€")}


def now_table(values, use_unit, cost_unit):
    """The use and costs per period as one table, the units named once per row and how long
    ago the measurement behind 'Nu' is in the top-left cell (asked by the user for
    Elektriciteit):

        12 s geleden    Nu    Dag   Week   Maand
        kWh           0,17   2,68  30,87  125,23
        €             0,05   0,74   8,33   33,81
    """
    by = {n: e for e, n in values if e}
    age = next(((e, n.split(":", 1)[1]) for n, e in by.items() if n.startswith("_age:")), None)
    cols = [(label, use, cost) for label, use, cost in BLOCKS if use in by or cost in by]
    if not cols:
        return None

    # HTML instead of a markdown table, so it can be centred (align passes HA's sanitizer).
    def row(unit, i):
        cells = [number(by[c[i]]) if c[i] in by else "–" for c in cols]
        return f"<tr><td>{unit}</td>" + "".join(f'<td align="right">{x}</td>' for x in cells) + "</tr>"
    corner = f"<small>{ago(*age)}</small>" if age else ""
    head = f"<tr><th>{corner}</th>" + "".join(f'<th align="right">{c[0]}</th>' for c in cols) + "</tr>"
    return {"type": "markdown",
            "content": f'<table align="center">{head}{row(use_unit, 1)}{row(cost_unit, 2)}</table>'}


def now_today(values, horizontal=False, labels=None):
    """Use and costs now and today (C25), as the user asked:

        **Nu**          **Dag**         **Week**        **Maand**
        0,17 kW         5,95 kWh        41,20 kWh       180,33 kWh
        0,05€/u         1,61€           11,12€          48,69€
        12 s geleden

    'values' are (entity_id, name) with the names of BLOCKS; a block without values is left
    out. 'labels' limits the blocks (Thuis shows only Nu and Dag)."""
    by = {n: e for e, n in values if e}
    age = next(((e, n.split(":", 1)[1]) for n, e in by.items() if n.startswith("_age:")), None)
    blocks = []
    for label, use, cost in BLOCKS:
        if labels and label not in labels:
            continue
        lines = [value(by[k]) for k in (use, cost) if k in by]
        # How long ago the measurement behind 'Nu' is: small, at the bottom of the Nu block.
        if label == "Nu" and lines and age:
            lines.append(f"<small>{ago(*age)}</small>")
        if lines:
            blocks.append(f"<b>{label}</b><br>" + "<br>".join(lines))
    if not blocks:
        return None
    if horizontal:   # on the overviews: the blocks side by side
        return {"type": "horizontal-stack",
                "cards": [{"type": "markdown", "content": b} for b in blocks]}
    return {"type": "markdown", "content": "\n\n".join(blocks)}


def write_start(ws, dashboards):
    """The start screen (C25): per dashboard a button, with its use now and today under it,
    as the default panel for everyone instead of HA's generated home screen (weather,
    devices). 'dashboards' are (url_path, title, icon, [(entity_id, name), ...]).

    Since HA 2026 the frontend only uses the default panel 'lovelace' when it is a dashboard
    entry with that url_path; otherwise it falls back to the home screen. HA turns the legacy
    default config into such an entry when it starts, so the first time this saves the legacy
    config and restarts HA; after that it writes to the entry. Call it last: a restart closes
    the websocket."""
    # The title is HA's location name (Instellingen > Systeem > Algemeen), so the address
    # stays out of this repository.
    name = ws.call("get_config").get("location_name") or "Thuis"
    # Three columns, one button each: the icon and name, then Nu and Dag with use and
    # costs. A markdown card, because it can hold all of that and still has a tap action.
    buttons = []
    for path, title, icon, values in dashboards:
        body = now_today(values, labels=("Nu", "Dag"))
        content = (f'<center><ha-icon icon="{icon}"></ha-icon><br><b>{title}</b>'
                   + ("<br><br>" + body["content"].replace("\n\n", "<br><br>") if body else "")
                   + "</center>")
        buttons.append({"type": "markdown", "content": content,
                        "tap_action": {"action": "navigate", "navigation_path": f"/{path}"}})
    config = {"title": name, "views": [
        {"title": name, "path": "thuis", "icon": "mdi:home",
         "cards": [heading(name),
                   {"type": "grid", "columns": 3, "square": False, "cards": buttons}]}]}
    core = ws.call("frontend/get_system_data", key="core").get("value") or {}
    if core.get("default_panel") != "lovelace":
        ws.call("frontend/set_system_data", key="core", value={**core, "default_panel": "lovelace"})
        m.log("default panel: the start screen (/lovelace) instead of HA's home screen")
    entry = next((d for d in ws.call("lovelace/dashboards/list") if d["url_path"] == "lovelace"), None)
    if entry:
        ws.call("lovelace/config/save", url_path="lovelace", config=config)
        if entry.get("title") != name or entry.get("icon") != "mdi:home":
            ws.call("lovelace/dashboards/update", dashboard_id=entry["id"], title=name, icon="mdi:home")
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
        ws2.call("lovelace/dashboards/update", dashboard_id=entry["id"], title=name, icon="mdi:home")
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
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'sudo martha-ha token' first")
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
