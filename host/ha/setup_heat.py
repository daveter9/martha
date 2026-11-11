#!/usr/bin/env python3
"""Set up the comparison gas boiler versus an all-electric heat pump in production Home
Assistant (docs/warmtepomp.md).

Run as root on martha, after install.sh (which applies host/db/heat.sql), setup_p1.py and
setup_solar.py:

    sudo python3 /usr/local/lib/martha/setup_heat.py [--no-backfill]

Idempotent; each run:
  1. makes a backup (martha-ha backup before-heat);
  2. writes the P1 gas meter to the table heat_entity;
  3. makes sure LTSS stores input_number (the helpers keep their history);
  4. writes packages/heat.yaml with the weather entity filled in (martha-ha put_files:
     check_config, restart, health check, the previous file back on errors);
  5. creates the helpers input_number.heat_* from the table heat_param that do not exist
     yet (existing helpers and their values are left alone), and removes helpers whose
     parameter is gone, plus registry entries of sensors no longer in the package;
  6. writes the outdoor temperature and gas market price sensors to heat_entity;
  7. fetches the hourly outdoor temperature of the last 92 days from Open-Meteo into
     heat_weather, and the gas market prices since the first gas data from EnergyZero into
     heat_gas_market (needs internet; skipped with a warning without it, or with
     --no-backfill);
  8. writes the dashboards per domain (host/ha/dashboards.py): the comparison on
     Verwarming, hot water on Tapwater;
  9. calculates all minutes since the first gas data, and the forecast.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402
import dashboards  # noqa: E402
import setup_p1  # noqa: E402
import setup_solar  # noqa: E402

TEMPLATES = "/usr/local/lib/martha/heat"   # host/ha/packages/heat.yaml
PACKAGE = "packages/heat.yaml"
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
              "&hourly=temperature_2m&past_days=92&forecast_days=1&timezone=UTC")
ENERGYZERO = ("https://api.energyzero.nl/v1/energyprices?fromDate={start}T00:00:00.000Z"
              "&tillDate={end}T23:59:59.999Z&interval=4&usageType=3&inclBtw=false")
GROUPS = [   # dashboard 'Instellingen': title, keys
    ("Contract (geldt vanaf het moment van wijzigen)",
     ["gas_price", "electricity_price", "gas_fixed_month", "maint_boiler", "maint_hp"]),
    ("Marktprijs (geldt vanaf het moment van wijzigen)",
     ["market_gas_markup", "gas_energy_tax", "market_elec_markup", "elec_energy_tax", "vat"]),
    ("Huis en ketel (geldt voor alle historie)",
     ["gas_kwh_per_m3", "boiler_eff_heating", "heating_limit", "gas_last_year"]),
    ("Warmtepomp (geldt voor alle historie)",
     ["supply_design", "supply_min", "carnot_eff", "cop_a7w35", "cop_max", "defrost_loss",
      "hp_capacity"]),
]
DHW_KEYS = ["dhw_min_kw", "dhw_max_minutes", "boiler_eff_dhw", "dhw_temp"]   # on Tapwater


def check_schema():
    if m.psql("SELECT to_regclass('public.heat_1m') IS NOT NULL") != "t":
        raise m.Error("table heat_1m is missing; run install.sh first (it applies host/db/heat.sql)")


def params():
    rows = m.psql("SELECT json_agg(p ORDER BY sort) FROM heat_param p")
    return json.loads(rows)


def set_role(role, entity_id):
    if not re.fullmatch(r"sensor\.[a-z0-9_]+", entity_id):
        raise m.Error(f"unexpected entity id {entity_id!r}")
    m.psql(f"INSERT INTO heat_entity (role, entity_id) VALUES ('{role}', '{entity_id}') "
           "ON CONFLICT (role) DO UPDATE SET entity_id = EXCLUDED.entity_id")
    m.log(f"{role}: {entity_id}")


def write_gas_role(token):
    entry = setup_p1.dsmr_entry(token)
    if not entry:
        raise m.Error("no DSMR integration; run setup_p1.py first")
    ws = m.WebSocket(m.HA_URL, token)
    try:
        ids = setup_p1.by_key(ws, entry)
    finally:
        ws.close()
    if "gas_meter_reading" not in ids:
        raise m.Error("P1 gas meter not found (DSMR key gas_meter_reading)")
    set_role("gas", ids["gas_meter_reading"])
    # The power now, for the electricity costs now on the start screen (heat_home).
    if "current_electricity_usage" in ids:
        set_role("elec_power", ids["current_electricity_usage"])


def weather_entity(ws):
    """The weather entity of the Met.no integration (HA's default), else any with a temperature."""
    registry = ws.call("config/entity_registry/list")
    met = [e["entity_id"] for e in registry
           if e["entity_id"].startswith("weather.") and e.get("platform") == "met"]
    states = {s["entity_id"]: s for s in ws.call("get_states")}
    for entity_id in met + sorted(states):
        s = states.get(entity_id)
        if s and entity_id.startswith("weather.") and isinstance(s["attributes"].get("temperature"), (int, float)):
            return entity_id
    raise m.Error("no weather entity with a temperature; add the Met.no integration in the UI")


def deploy_package(weather):
    with open(os.path.join(TEMPLATES, "heat.yaml")) as f:
        content = f.read().replace("__WEATHER__", weather)

    return m.put_files({PACKAGE: content}, "heat: gas boiler versus heat pump")


def create_helpers(ws, rows):
    """Create the helpers that are missing; never change the values of existing ones (only
    their display name, to match the label).
    Remove the heat_* helpers whose parameter no longer exists."""
    existing = {i["id"] for i in ws.call("input_number/list")}
    keys = {f"heat_{p['key']}" for p in rows}
    for item_id in sorted(i for i in existing if i.startswith("heat_") and i not in keys):
        ws.call("input_number/delete", input_number_id=item_id)
        m.log(f"removed helper input_number.{item_id} (parameter no longer used)")
    names = {e["entity_id"]: e.get("name") for e in ws.call("config/entity_registry/list")}
    created = 0
    for p in rows:
        item_id = f"heat_{p['key']}"
        if item_id in existing:
            # Keep the display name in step with the label; the value is left alone.
            if names.get(f"input_number.{item_id}") != p["label"]:
                setup_solar.rename(ws, f"input_number.{item_id}", p["label"])
            continue
        args = dict(name=item_id, min=p["min"], max=p["max"], step=p["step"],
                    initial=p["default_val"], mode="box", icon="mdi:heat-pump-outline")
        if p["unit"]:
            args["unit_of_measurement"] = p["unit"]
        ws.call("input_number/create", **args)
        setup_solar.rename(ws, f"input_number.{item_id}", p["label"])
        created += 1
    m.log(f"created {created} helpers" if created else "helpers already present")


PLATFORMS = ("sql", "template", "rest")


def package_unique_ids():
    with open(os.path.join(TEMPLATES, "heat.yaml")) as f:
        return set(re.findall(r"unique_id:\s*(heat_[a-z0-9_]+)", f.read()))


def heat_entities(ws, wait=30):
    """Entity ids of the sensors in packages/heat.yaml, by unique_id."""
    deadline = time.monotonic() + wait
    while True:
        ids = {e["unique_id"]: e["entity_id"] for e in ws.call("config/entity_registry/list")
               if e.get("platform") in PLATFORMS
               and str(e.get("unique_id", "")).startswith("heat_")}
        if {"heat_outdoor_temperature", "heat_gas_market"} <= set(ids) or time.monotonic() > deadline:
            return ids
        time.sleep(2)


def remove_old_sensors(ws):
    """Registry entries of heat_* sensors that are no longer in the package (they would stay
    'unavailable' forever)."""
    wanted = package_unique_ids()
    for e in ws.call("config/entity_registry/list"):
        uid = str(e.get("unique_id", ""))
        if e.get("platform") in PLATFORMS and uid.startswith("heat_") and uid not in wanted:
            ws.call("config/entity_registry/remove", entity_id=e["entity_id"])
            m.log(f"removed {e['entity_id']} (no longer in {PACKAGE})")


def backfill_weather(token):
    """Hourly temperatures of the last 92 days from Open-Meteo, for the minutes before the
    outdoor sensor existed (or while it was unavailable)."""
    config = m.ha_api("GET", "/api/config", token)
    url = OPEN_METEO.format(lat=round(config["latitude"], 4), lon=round(config["longitude"], 4))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
    except (OSError, ValueError) as e:
        m.log(f"WARNING: no Open-Meteo history ({e}); the reference year fills the gap. "
              "Run again when there is internet.")
        return
    now = time.strftime("%Y-%m-%dT%H:00", time.gmtime())
    rows = [(t, v) for t, v in zip(data["hourly"]["time"], data["hourly"]["temperature_2m"])
            if v is not None and t <= now and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", t)]
    if not rows:
        m.log("WARNING: Open-Meteo returned no temperatures")
        return
    values = ", ".join(f"('{t}:00+00', {float(v)})" for t, v in rows)
    m.psql("INSERT INTO heat_weather (time, temp) VALUES " + values +
           " ON CONFLICT (time) DO UPDATE SET temp = EXCLUDED.temp, fetched = now()")
    m.log(f"Open-Meteo: {len(rows)} hourly temperatures ({rows[0][0]} .. {rows[-1][0]})")


def backfill_gas_market():
    """Hourly gas market prices since the first gas data from EnergyZero (the REST sensor only
    covers the last 3 days)."""
    start = m.psql("SELECT to_char(coalesce(heat_first(), now()) - INTERVAL '1 day', 'YYYY-MM-DD')")
    end = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
    try:
        with urllib.request.urlopen(ENERGYZERO.format(start=start, end=end), timeout=30) as r:
            prices = json.load(r)["Prices"]
    except (OSError, ValueError, KeyError) as e:
        m.log(f"WARNING: no EnergyZero gas prices ({e}); the REST sensor fills them from now on. "
              "Run again when there is internet.")
        return
    rows = [(p["readingDate"], float(p["price"])) for p in prices
            if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:00:00Z", p.get("readingDate", ""))
            and isinstance(p.get("price"), (int, float))]
    if not rows:
        m.log("WARNING: EnergyZero returned no gas prices")
        return
    m.psql("INSERT INTO heat_gas_market (time, price) VALUES "
           + ", ".join(f"('{t}', {v})" for t, v in rows)
           + " ON CONFLICT (time) DO UPDATE SET price = EXCLUDED.price, fetched = now()")
    m.log(f"EnergyZero: {len(rows)} hourly gas prices ({rows[0][0]} .. {rows[-1][0]})")


def views(ids, rows, gas_meter=None):
    """The heat views: the comparison on the dashboard Verwarming, hot water on Tapwater
    (host/ha/dashboards.py). 'gas_meter' is the P1 gas meter, for the age of the measurement
    in the table on Tapwater."""
    def e(uid):
        return ids.get(uid)

    def graph(title, uids, period, days, chart_type="bar"):
        return {"type": "statistics-graph", "title": title, "chart_type": chart_type,
                "entities": [e(u) for u in uids if e(u)], "stat_types": ["change"],
                "period": period, "days_to_show": days}

    def md(title, uid, attribute):
        return {"type": "markdown", "title": title,
                "content": f"{{{{ state_attr('{e(uid)}', '{attribute}') }}}}"}

    def full(card):
        return {**card, "grid_options": {"columns": "full"}}

    calculation = [
        md("Hoe één minuut wordt berekend", "heat_explain", "uitleg"),
        {"type": "history-graph", "title": "Buitentemperatuur (48 uur)", "hours_to_show": 48,
         "entities": [e("heat_outdoor_temperature")]},
        graph("Warmtevraag per uur (48 uur)", ["heat_total_heat"], "hour", 2),
        {"type": "markdown", "title": "De formules", "content": FORMULAS},
    ]
    assumptions = [md("Aannames en wat je kunt verfijnen", "heat_assumptions", "tabel")]

    by_key = {p["key"] for p in rows}
    # Tapwater (user's request, 2026-11-11): only the table Nu, Dag, Week, Maand as on the
    # overviews, and the gas per hour of today as before.
    table = dashboards.now_table([
        (e("heat_home_dhw_now"), "Nu"), (gas_meter, "_age:last_changed"),
        (e("heat_home_dhw_cost_now"), "Kosten nu"),
        (e("heat_home_dhw_today"), "Vandaag"), (e("heat_home_dhw_cost_today"), "Kosten vandaag"),
        (e("heat_home_dhw_week"), "Deze week"), (e("heat_home_dhw_cost_week"), "Kosten deze week"),
        (e("heat_home_dhw_month"), "Deze maand"), (e("heat_home_dhw_cost_month"), "Kosten deze maand"),
    ], *dashboards.TABLES["dhw"])
    cards = [table] if table else []
    if e("heat_total_gas_dhw"):
        # HA's date picker starts on today and steps through the days, as the power on
        # Zonnepanelen.
        cards += dashboards.picked("dhw", {
            "type": "statistics-graph", "title": "Tapwater per uur", "chart_type": "bar",
            "entities": [{"entity": e("heat_total_gas_dhw"), "name": "Gas tapwater"}],
            "stat_types": ["change"], "period": "hour"})
    dhw = {"type": "sections", "max_columns": 4, "title": "Tapwater", "path": "tapwater",
           "icon": "mdi:shower-head",
           "sections": [{"type": "grid", "column_span": 4, "cards": [full(c) for c in cards]}]
           } if cards else None
    grouped = {k for _, keys in GROUPS for k in keys} | set(DHW_KEYS)
    missing = sorted(by_key - grouped)
    settings = []
    for title, keys in GROUPS + ([("Overig", missing)] if missing else []):
        settings.append({"type": "entities", "title": title,
                         "entities": [f"input_number.heat_{k}" for k in keys if k in by_key]})
    settings.append({"type": "markdown", "content":
        "De stroomprijs van het contract komt van **Inkoopprijs normaal/dal** (dashboard "
        "Elektriciteit, tab Instellingen); zolang die op 0 staan (en voor de tijd dat ze dat stonden), geldt *Stroomprijs "
        "contract als die bij Zonnepanelen ontbreekt*. Na een wijziging rekent martha binnen 5 minuten "
        "alles opnieuw uit."})

    def clean(cards):
        out = []
        for c in cards:
            if "entities" in c:
                c["entities"] = [x for x in c["entities"] if x]
                if not c["entities"]:
                    continue
            if "cards" in c:
                c["cards"] = [x for x in c["cards"] if x.get("entity")]
            if c.get("type") == "statistic" and not c.get("entity"):
                continue
            out.append(c)
        return out

    if not ids:
        return {"heating": [], "dhw": []}
    heating = [("Berekening", "berekening", "mdi:calculator-variant-outline", calculation),
               ("Aannames", "aannames", "mdi:clipboard-list-outline", assumptions),
               ("Instellingen", "instellingen", "mdi:cog", settings)]
    # Kosten and Prognose are not shown (user's request, 2026-11-11); their sensors stay.
    return {"heating": [{"title": t, "path": p, "icon": i, "cards": clean(c)} for t, p, i, c in heating],
            "dhw": [dhw] if dhw else []}


FORMULAS = """\
| Stap | Formule |
|---|---|
| Gas per minuut | meterstap / 5, over de 5 minuten vóór de melding |
| Warm water | een melding van minstens 8 kW, hooguit 20 minuten achter elkaar, of een losse melding; de rest is verwarming |
| Nuttige warmte | gas × 9,769 kWh/m³ × rendement ketel (verwarming of warm water) |
| Graaduren | max(0, stookgrens − buiten) / 60, per minuut |
| Stooklijn | aanvoer = min + (ontwerp − min) × (stookgrens − buiten) / (stookgrens + 10) |
| COP | Carnot-rendement × (aanvoer + 273,15) / max(aanvoer − buiten, 5) × (1 − ontdooien onder 6 °C), tussen 1 en het maximum |
| Warmtepomp | verwarming / COP + warm water / COP warm water; boven het vermogen per uur: element (COP 1) |
| Stroomkosten | eerst uit het zonne-overschot van die minuut (gemiste terugleververgoeding), de rest × stroomprijs |
| Gaskosten | m³ × gasprijs |
| Marktprijs | (day-ahead-prijs + opslag + energiebelasting) × (1 + btw); stroom per kwartier (Nord Pool), gas per uur (EnergyZero) |
| Vaste kosten | gasaansluiting per maand / minuten in de maand; onderhoud per jaar / minuten in het jaar |
| Warmteverlies H | Σ warmte verwarming / Σ graaduren over volledige, gemeten dagen; zolang onzeker geijkt op het jaarverbruik |
| Prognose | per uur van 2019: H × max(0, stookgrens − buiten), plus het gemeten warm water per dag |
"""


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--no-backfill", action="store_true",
                   help="do not fetch the outdoor temperature history from Open-Meteo")
    a = p.parse_args()
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'sudo martha-ha token' first")

    check_schema()
    m.backup("before-heat")
    write_gas_role(token)
    storage_changed = setup_solar.patch_storage()
    ws = m.WebSocket(m.HA_URL, token)
    try:
        weather = weather_entity(ws)
    finally:
        ws.close()
    m.log(f"weather entity: {weather}")
    deployed = deploy_package(weather)
    if storage_changed and not deployed:
        setup_solar.restart()
    rows = params()
    ws = m.WebSocket(m.HA_URL, token)
    try:
        create_helpers(ws, rows)
        remove_old_sensors(ws)
        ids = heat_entities(ws)
        for uid, role in (("heat_outdoor_temperature", "outdoor_temp"), ("heat_gas_market", "gas_market")):
            if uid not in ids:
                raise m.Error(f"sensor {uid} not found; check packages/heat.yaml")
            set_role(role, ids[uid])
        m.log(f"{len(ids)} heat sensors: " + ", ".join(sorted(ids.values())))
        dashboards.write(ws, token)
    finally:
        ws.close()
    if not a.no_backfill:
        backfill_weather(token)
        backfill_gas_market()
    # Calculate everything now instead of waiting for the job.
    m.psql("CALL heat_job(0, NULL)")
    m.psql("SELECT heat_results()")
    m.log("calculated: " + m.psql("SELECT format('%s days, %s minutes', round((sum(minutes) / 1440.0)::numeric, 1), "
                                  "sum(minutes)) FROM heat_1d"))


if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"setup_heat: {e}")
