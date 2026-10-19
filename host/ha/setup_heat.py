#!/usr/bin/env python3
"""Set up the comparison gas boiler versus heat pump in production Home Assistant
(docs/warmtepomp.md).

Run as root on martha, after install.sh (which applies host/db/heat.sql), setup_p1.py and
setup_solar.py:

    sudo python3 /usr/local/lib/martha/setup_heat.py [--no-backfill]

Idempotent; each run:
  1. makes a backup (martha-ha backup before-heat);
  2. writes the P1 gas meter to the table heat_entity;
  3. makes sure LTSS stores input_number (the helpers keep their history);
  4. deploys packages/heat.yaml through martha-ha (check_config, backup, restart, health
     check, automatic rollback), with the weather entity filled in;
  5. creates the helpers input_number.heat_* from the table heat_param that do not exist
     yet; existing helpers and their values are left alone;
  6. writes the outdoor temperature sensor to heat_entity;
  7. fetches the hourly outdoor temperature of the last 92 days from Open-Meteo into
     heat_weather (needs internet; skipped with a warning without it, or with --no-backfill);
  8. writes the dashboard 'Warmtepomp' (/warmte-pomp);
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
import setup_p1  # noqa: E402
import setup_solar  # noqa: E402

DASHBOARD = "warmte-pomp"   # HA requires a hyphen in a dashboard URL
TEMPLATES = "/usr/local/lib/martha/heat"   # host/ha/packages/heat.yaml
PACKAGE = "packages/heat.yaml"
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
              "&hourly=temperature_2m&past_days=92&forecast_days=1&timezone=UTC")
GROUPS = [   # dashboard 'Instellingen': title, kinds or keys
    ("Prijzen (gelden vanaf het moment van wijzigen)", "price"),
    ("Huis, ketel en gebruik (geldt voor alle historie)",
     ["gas_kwh_per_m3", "boiler_eff_heating", "boiler_eff_dhw", "dhw_m3_day", "cooking_m3_day",
      "cooking_kwh_per_m3", "heating_limit"]),
    ("Warmtepomp (geldt voor alle historie)",
     ["supply_design", "supply_min", "carnot_eff", "cop_a7w35", "cop_max", "defrost_loss",
      "dhw_temp", "hp_capacity", "hybrid_capacity"]),
    ("Investering (alleen voor de prognose)", "investment"),
    ("Controle", "check"),
]


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

    def build(base):
        with m.tempfile.TemporaryDirectory(dir=m.WORK) as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
            m.git("read-tree", base, env=env)
            blob = m.git("hash-object", "-w", "--stdin", input=content).stdout.strip()
            m.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{PACKAGE}", env=env)
            return m.git("write-tree", env=env).stdout.strip()

    with m.locked(wait=True):
        before = m.head()
        after = m.apply(build, "heat: gas boiler versus heat pump (packages/heat.yaml)")
    return after != before


def create_helpers(ws, rows):
    """Create the helpers that are missing; never change existing ones or their values."""
    existing = {i["id"] for i in ws.call("input_number/list")}
    created = 0
    for p in rows:
        item_id = f"heat_{p['key']}"
        if item_id in existing:
            continue
        args = dict(name=item_id, min=p["min"], max=p["max"], step=p["step"],
                    initial=p["default_val"], mode="box", icon="mdi:heat-pump-outline")
        if p["unit"]:
            args["unit_of_measurement"] = p["unit"]
        ws.call("input_number/create", **args)
        setup_solar.rename(ws, f"input_number.{item_id}", p["label"])
        created += 1
    m.log(f"created {created} helpers" if created else "helpers already present")


def heat_entities(ws, wait=30):
    """Entity ids of the sensors in packages/heat.yaml, by unique_id."""
    deadline = time.monotonic() + wait
    while True:
        ids = {e["unique_id"]: e["entity_id"] for e in ws.call("config/entity_registry/list")
               if e.get("platform") in ("sql", "template")
               and str(e.get("unique_id", "")).startswith("heat_")}
        if "heat_outdoor_temperature" in ids or time.monotonic() > deadline:
            return ids
        time.sleep(2)


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


def dashboard(ids, rows):
    def e(uid):
        return ids.get(uid)

    def stat(uid, title):
        return {"type": "statistic", "entity": e(uid), "name": title, "stat_type": "change",
                "period": "energy_date_selection"}

    def graph(title, uids, period, days, chart_type="bar"):
        return {"type": "statistics-graph", "title": title, "chart_type": chart_type,
                "entities": [e(u) for u in uids if e(u)], "stat_types": ["change"],
                "period": period, "days_to_show": days}

    def md(title, uid, attribute):
        return {"type": "markdown", "title": title,
                "content": f"{{{{ state_attr('{e(uid)}', '{attribute}') }}}}"}

    costs = ["heat_total_cv", "heat_total_wp", "heat_total_hyb"]
    comparison = [
        {"type": "energy-date-selection"},
        {"type": "grid", "columns": 3, "square": False, "cards": [
            stat("heat_total_cv", "CV-ketel"),
            stat("heat_total_wp", "Warmtepomp"),
            stat("heat_total_hyb", "Hybride"),
        ]},
        {"type": "statistics-graph", "title": "Kosten in de gekozen periode", "chart_type": "bar",
         "entities": [e(u) for u in costs if e(u)], "stat_types": ["change"],
         "energy_date_selection": True},
        {"type": "markdown", "content":
            "Wat de verwarming, het warme water en het koken **werkelijk** kosten met de CV-ketel, "
            "en wat precies dezelfde warmte zou kosten met een **volledig elektrische** warmtepomp "
            "(gasaansluiting eraf, koken op inductie) of een **hybride** warmtepomp. Per minuut berekend "
            "uit het gasverbruik van de P1-meter en de buitentemperatuur, inclusief vaste kosten van de "
            "gasaansluiting en onderhoud. Hoe: tab *Berekening*. Op welke aannames: tab *Aannames*."},
        graph("Kosten per dag (30 dagen)", costs, "day", 30),
        graph("Kosten per maand (1 jaar)", costs, "month", 365),
        graph("Warmte en stroom per dag (30 dagen)",
              ["heat_total_heat", "heat_total_wp_kwh", "heat_total_hyb_kwh"], "day", 30),
        {"type": "entities", "title": "Tot nu toe", "entities": [
            e("heat_total_cv"), e("heat_total_wp"), e("heat_total_hyb"), e("heat_total_heat"),
            e("heat_total_wp_kwh"), e("heat_total_hyb_kwh"), e("heat_total_hyb_gas")]},
    ]
    calculation = [
        md("Hoe één minuut wordt berekend", "heat_explain", "uitleg"),
        {"type": "history-graph", "title": "Buitentemperatuur (48 uur)", "hours_to_show": 48,
         "entities": [e("heat_outdoor_temperature")]},
        graph("Warmtevraag per uur (48 uur)", ["heat_total_heat"], "hour", 2),
        {"type": "markdown", "title": "De formules", "content": FORMULAS},
    ]
    forecast = [
        md("Per jaar, met de temperaturen van een gemiddeld jaar", "heat_forecast", "tabel"),
        {"type": "entities", "title": "Warmteverlies van het huis", "entities": [
            e("heat_loss"),
            {"type": "attribute", "entity": e("heat_loss"), "attribute": "vermogen_bij_min10_kw",
             "name": "Benodigd vermogen bij -10 °C", "suffix": "kW"},
            {"type": "attribute", "entity": e("heat_loss"), "attribute": "gas_zonder_verwarming_m3_per_dag",
             "name": "Gemeten gas op dagen zonder verwarming", "suffix": "m³/dag"},
            {"type": "attribute", "entity": e("heat_loss"), "attribute": "toelichting",
             "name": "Toelichting"},
        ]},
        {"type": "markdown", "content":
            "Het **warmteverlies** (W/K) is de warmte die het huis per uur verliest per graad verschil "
            "tussen binnen en buiten. Het volgt uit de metingen: de warmte voor verwarming per dag gedeeld "
            "door de graaduren (stookgrens min buitentemperatuur, opgeteld per minuut). Hoe meer koude en "
            "zachte dagen, hoe betrouwbaarder (R² naar 1). De prognose rekent daarmee een heel jaar door "
            "met de uurtemperaturen van 2019, het jaar dat het dichtst bij het gemiddelde van 2016-2025 ligt."},
    ]
    assumptions = [md("Aannames en wat je kunt verfijnen", "heat_assumptions", "tabel")]

    by_key = {p["key"]: p for p in rows}
    settings = []
    for title, sel in GROUPS:
        keys = [p["key"] for p in rows if p["kind"] == sel] if isinstance(sel, str) else \
               [k for k in sel if k in by_key]
        settings.append({"type": "entities", "title": title,
                         "entities": [f"input_number.heat_{k}" for k in keys]})
    settings.append({"type": "markdown", "content":
        "De stroomprijs komt van **Inkoopprijs normaal/dal** op het dashboard Zonnepanelen; zolang "
        "die op 0 staan, geldt *Stroomprijs als het contract ontbreekt*. Na een wijziging rekent "
        "martha binnen 5 minuten alles opnieuw uit."})

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

    views = [("Vergelijking", "vergelijking", "mdi:scale-balance", comparison),
             ("Berekening", "berekening", "mdi:calculator-variant-outline", calculation),
             ("Prognose", "prognose", "mdi:calendar-cash", forecast),
             ("Aannames", "aannames", "mdi:clipboard-list-outline", assumptions),
             ("Instellingen", "instellingen", "mdi:cog", settings)]
    return {"title": "Warmtepomp",
            "views": [{"title": t, "path": p, "icon": i, "cards": clean(c)} for t, p, i, c in views]}


FORMULAS = """\
| Stap | Formule |
|---|---|
| Gas per minuut | meterstap / 5, over de 5 minuten vóór de melding |
| Koken, warm water | vaste m³ per dag (naar rato van het deel van de dag); de rest is verwarming |
| Nuttige warmte | gas × 9,769 kWh/m³ × rendement ketel (verwarming of warm water) |
| Graaduren | max(0, stookgrens − buiten) / 60, per minuut |
| Stooklijn | aanvoer = min + (ontwerp − min) × (stookgrens − buiten) / (stookgrens + 10) |
| COP | Carnot-rendement × (aanvoer + 273,15) / max(aanvoer − buiten, 5) × (1 − ontdooien onder 6 °C), tussen 1 en het maximum |
| Warmtepomp | verwarming / COP + warm water / COP warm water + koken × 4,4 kWh/m³; boven het vermogen per uur: element (COP 1) |
| Hybride | verwarming tot het vermogen per uur, alleen als stroomprijs / COP < gasprijs / (9,769 × rendement); de rest op gas |
| Stroomkosten | eerst uit het zonne-overschot van die minuut (gemiste terugleververgoeding), de rest × stroomprijs (normaal/dal) |
| Gaskosten | m³ × gasprijs |
| Vaste kosten | gasaansluiting per maand / minuten in de maand; onderhoud per jaar / minuten in het jaar |
| Warmteverlies H | Σ warmte verwarming / Σ graaduren over volledige, gemeten dagen |
| Prognose | per uur van 2019: H × max(0, stookgrens − buiten), plus warm water en koken per dag |
"""


def write_dashboard(ws, ids, rows):
    if not any(d["url_path"] == DASHBOARD for d in ws.call("lovelace/dashboards/list")):
        ws.call("lovelace/dashboards/create", url_path=DASHBOARD, title="Warmtepomp",
                icon="mdi:heat-pump-outline", show_in_sidebar=True, require_admin=False,
                mode="storage")
    ws.call("lovelace/config/save", url_path=DASHBOARD, config=dashboard(ids, rows))
    m.log(f"dashboard: {m.HA_URL}/{DASHBOARD}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--no-backfill", action="store_true",
                   help="do not fetch the outdoor temperature history from Open-Meteo")
    a = p.parse_args()
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'martha-ha setup-gate' first")

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
        ids = heat_entities(ws)
        if "heat_outdoor_temperature" not in ids:
            raise m.Error("sensor heat_outdoor_temperature not found; check packages/heat.yaml")
        set_role("outdoor_temp", ids["heat_outdoor_temperature"])
        m.log(f"{len(ids)} heat sensors: " + ", ".join(sorted(ids.values())))
        write_dashboard(ws, ids, rows)
    finally:
        ws.close()
    if not a.no_backfill:
        backfill_weather(token)
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
