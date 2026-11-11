#!/usr/bin/env python3
"""Set up the solar panel calculations in production Home Assistant (docs/zonnepanelen.md).

Run as root on martha, after install.sh (which applies host/db/solar.sql) and setup_p1.py:

    sudo python3 /usr/local/lib/martha/setup_solar.py [--start 2026-10-23] [--inverter [--host IP]]

Idempotent; each run:
  1. makes a backup (martha-ha backup before-solar);
  2. writes the P1 entities the calculations use to the table solar_entity;
  3. adds input_number and input_datetime to the LTSS include in
     packages/martha_storage.yaml, so the helpers keep their history;
  4. adds the Nord Pool integration (area NL, EUR) if it is not there yet; that needs
     internet, and without it the step is skipped with a warning;
  5. writes packages/solar.yaml and packages/solar_prices.yaml (martha-ha put_files:
     check_config, restart, health check, the previous files back on errors);
  6. creates the helpers (tariffs, investment, forecast, battery) that do not exist yet;
     existing helpers and their values are left alone;
  7. writes the dashboards per domain (host/ha/dashboards.py): the solar tabs are on
     Elektriciteit.

With --inverter (phase 5, the Growatt through Modbus TCP on its ShineLink), also:
  - the fixed IP address of the ShineLink (--host, once) in /etc/martha/inverter, and from
    there as martha_growatt_host in secrets.yaml;
  - packages/solar_growatt.yaml (Home Assistant's Modbus integration reads the inverter);
  - the roles pv_power and pv_total in solar_entity, so the calculations use the generation;
  - the tab 'Omvormer' on the dashboard Elektriciteit.
deploy.ps1 runs --inverter when /etc/martha/inverter exists.
"""

import argparse
import datetime
import os
import re
import sys
import time

sys.path.insert(0, "/usr/local/lib/martha")
import martha_ha as m  # noqa: E402
import dashboards  # noqa: E402
import setup_p1  # noqa: E402

TEMPLATES = "/usr/local/lib/martha/solar"   # host/ha/packages/solar*.yaml
PACKAGES = ("packages/solar.yaml", "packages/solar_prices.yaml")
GROWATT_PACKAGE = "packages/solar_growatt.yaml"
STORAGE = m.CONFIG + "/packages/martha_storage.yaml"
SECRETS = m.CONFIG + "/secrets.yaml"
HOST_SECRET = "martha_growatt_host"
INVERTER = "/etc/martha/inverter"   # IP address of the ShineLink; deploy.ps1 checks it exists
PV_ROLES = {"solar_growatt_power": "pv_power", "solar_growatt_energy_total": "pv_total"}
LTSS_DOMAINS = ("input_number", "input_datetime")
DEFAULT_START = "2026-10-23"   # the panels are installed

# DSMR sensor key -> role in solar_entity (host/db/solar.sql). In the Netherlands tariff 1
# is the low tariff and tariff 2 the normal one.
ROLES = {
    "electricity_used_tariff_1": "import_low",
    "electricity_used_tariff_2": "import_normal",
    "electricity_delivered_tariff_1": "export_low",
    "electricity_delivered_tariff_2": "export_normal",
    "electricity_active_tariff": "tariff",
    "current_electricity_delivery": "export_power",   # kW, for solar_home()
}

# Helpers: key -> (label, unit, min, max, step, initial). The entity is
# input_number.solar_<key>; host/db/solar.sql reads the history by that key.
# Prices are all-in (incl. VAT) unless the label says otherwise. A value first changed
# after the start date holds from the start date (solar_setting_ranges).
HELPERS = {
    # Contract (doel 1, 2)
    "import_normal": ("Inkoopprijs normaal", "EUR/kWh", 0, 2, 0.0001, 0),
    "import_low": ("Inkoopprijs dal", "EUR/kWh", 0, 2, 0.0001, 0),
    "feed_in": ("Terugleververgoeding", "EUR/kWh", -1, 2, 0.0001, 0),
    "feed_in_cost": ("Terugleverkosten per kWh", "EUR/kWh", 0, 2, 0.0001, 0),
    "feed_in_cost_month": ("Terugleverkosten per maand", "EUR", 0, 100, 0.01, 0),
    # Dynamic contract for the comparison (V1); excl. VAT
    "dyn_import_markup": ("Dynamisch: opslag inkoop (excl. btw)", "EUR/kWh", 0, 1, 0.0001, 0),
    "dyn_feed_in_markup": ("Dynamisch: opslag teruglevering (excl. btw)", "EUR/kWh", 0, 1, 0.0001, 0),
    "energy_tax": ("Energiebelasting (excl. btw)", "EUR/kWh", 0, 1, 0.0001, 0),
    "vat": ("Btw", "%", 0, 50, 0.1, 21),
    # Investment (doel 3)
    "investment": ("Investering zonnepanelen", "EUR", 0, 100000, 1, 0),
    "extra_costs": ("Bijkomende kosten (totaal)", "EUR", 0, 100000, 1, 0),
    # Forecast (doel 5)
    "expected_yield": ("Verwachte opwek per jaar", "kWh", 0, 50000, 1, 0),
    "self_use_assumed": ("Aangenomen eigengebruik", "%", 0, 100, 1, 30),
    "degradation": ("Degradatie panelen", "%/jaar", 0, 5, 0.01, 0.5),
    "lifetime_panels": ("Levensduur panelen", "jaar", 1, 50, 1, 25),
    "lifetime_inverter": ("Levensduur omvormer", "jaar", 1, 50, 1, 12),
    "inverter_replacement": ("Vervangingskosten omvormer", "EUR", 0, 20000, 1, 800),
    "growth_import": ("Prijsstijging inkoop", "%/jaar", -20, 20, 0.1, 2),
    "growth_feed_in": ("Prijsstijging teruglevering", "%/jaar", -20, 20, 0.1, 0),
    "discount_rate": ("Discontovoet", "%/jaar", 0, 20, 0.1, 3),
    # Battery simulation (doel 4)
    "battery_capacity": ("Batterij: capaciteit", "kWh", 0, 100, 0.1, 5),
    "battery_power": ("Batterij: laad- en ontlaadvermogen", "kW", 0, 50, 0.1, 2.5),
    "battery_efficiency": ("Batterij: rendement (heen en terug)", "%", 1, 100, 1, 90),
}


def check_schema():
    if m.psql("SELECT to_regclass('public.solar_1m') IS NOT NULL") != "t":
        raise m.Error("table solar_1m is missing; run install.sh first (it applies host/db/solar.sql)")


def write_roles(token):
    entry = setup_p1.dsmr_entry(token)
    if not entry:
        raise m.Error("no DSMR integration; run setup_p1.py first")
    ws = m.WebSocket(m.HA_URL, token)
    try:
        ids = setup_p1.by_key(ws, entry)
    finally:
        ws.close()
    missing = [k for k in ROLES if k not in ids]
    if missing:
        raise m.Error("P1 sensors not found: " + ", ".join(missing))
    values = ", ".join(f"('{ROLES[k]}', '{ids[k]}')" for k in ROLES
                       if re.fullmatch(r"sensor\.[a-z0-9_]+", ids[k]))
    m.psql("INSERT INTO solar_entity (role, entity_id) VALUES " + values +
           " ON CONFLICT (role) DO UPDATE SET entity_id = EXCLUDED.entity_id")
    m.log("P1 entities: " + ", ".join(f"{ROLES[k]}={ids[k]}" for k in ROLES))


def patch_storage():
    """Add the helper domains to the LTSS include. True when the file changed. The file is
    managed by install.sh and edited on the server, so only this list is touched."""
    with open(STORAGE) as f:
        text = f.read()
    ltss = text.split("\nltss:", 1)
    if len(ltss) != 2:
        raise m.Error(f"no 'ltss:' section in {STORAGE}")
    missing = [d for d in LTSS_DOMAINS if not re.search(rf"^\s+- {d}\s*$", ltss[1], re.M)]
    if not missing:
        return False
    match = re.search(r"^(\s+)domains:\s*\n((?:\1\s+(?:- \S+|#.*)\s*\n)+)", ltss[1], re.M)
    if not match:
        raise m.Error(f"add {', '.join(missing)} to ltss: include: domains: in {STORAGE} "
                      "by hand (see host/ha/packages/martha_storage.yaml) and run again")
    indent = re.match(r"(\s+)-", match.group(2).splitlines()[0]).group(1)
    added = "".join(f"{indent}- {d}\n" for d in missing)
    end = match.end(2)
    ltss[1] = ltss[1][:end] + added + ltss[1][end:]
    with open(STORAGE, "w") as f:
        f.write(ltss[0] + "\nltss:" + ltss[1])
    m.log(f"LTSS now also stores {', '.join(missing)} ({STORAGE})")
    return True


def add_nordpool(token):
    entries = m.ha_api("GET", "/api/config/config_entries/entry?domain=nordpool", token)
    if entries:
        m.log(f"Nord Pool integration already present ({entries[0]['title']})")
        return
    m.log("adding Nord Pool integration (area NL, EUR)")
    try:
        flow = m.ha_api("POST", "/api/config/config_entries/flow", token, {"handler": "nordpool"})
        result = m.ha_api("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", token,
                          {"areas": ["NL"], "currency": "EUR"}, timeout=60)
    except (m.urllib.error.URLError, OSError) as e:
        m.log(f"WARNING: Nord Pool not added ({e}); run again when there is internet")
        return
    if result.get("type") != "create_entry":
        m.log(f"WARNING: Nord Pool not added ({result.get('errors') or result}); "
              "it needs internet; run again later")


def inverter_host(host):
    """The IP address of the ShineLink: from --host (stored in /etc/martha/inverter), else
    the stored one."""
    if host:
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
            raise m.Error(f"--host must be an IPv4 address, got {host}")
        os.makedirs(os.path.dirname(INVERTER), exist_ok=True)
        with open(INVERTER, "w") as f:
            f.write(host + "\n")
        return host
    try:
        with open(INVERTER) as f:
            return f.read().strip()
    except OSError:
        raise m.Error(f"no {INVERTER}; give the fixed IP address of the ShineLink with --host")


def write_secret(key, value):
    """Set key in secrets.yaml (0600); True when it changed."""
    text = ""
    if os.path.exists(SECRETS):
        with open(SECRETS) as f:
            text = f.read()
    line = f'{key}: "{value}"'
    if re.search(rf"^{re.escape(line)}\s*$", text, re.M):
        return False
    if re.search(rf"^{key}:", text, re.M):
        text = re.sub(rf"^{key}:.*$", line, text, flags=re.M)
    else:
        text += ("\n" if text and not text.endswith("\n") else "") + line + "\n"
    with open(SECRETS + ".tmp", "w") as f:
        f.write(text)
    os.chmod(SECRETS + ".tmp", 0o600)
    os.replace(SECRETS + ".tmp", SECRETS)
    m.log(f"{key} in {SECRETS}: {value}")
    return True


def write_pv_roles(ws):
    ids = sql_entities(ws)
    missing = [uid for uid in PV_ROLES if uid not in ids]
    if missing:
        raise m.Error("Growatt sensors not found: " + ", ".join(missing))
    m.psql("INSERT INTO solar_entity (role, entity_id) VALUES " +
           ", ".join(f"('{role}', '{ids[uid]}')" for uid, role in PV_ROLES.items()
                     if re.fullmatch(r"sensor\.[a-z0-9_]+", ids[uid])) +
           " ON CONFLICT (role) DO UPDATE SET entity_id = EXCLUDED.entity_id")
    m.log("inverter entities: " + ", ".join(f"{r}={ids[u]}" for u, r in PV_ROLES.items()))


def deploy_packages(paths):
    """Write the packages through martha-ha. True when something changed."""
    contents = {}
    for path in paths:
        with open(os.path.join(TEMPLATES, os.path.basename(path))) as f:
            contents[path] = f.read()
    return m.put_files(contents, "solar: calculations and day-ahead prices")


def restart():
    since = time.time()
    m.compose("restart")
    ok, msg = m.wait_healthy(since)
    if not ok:
        raise m.Error(f"Home Assistant unhealthy after restart: {msg}")


def rename(ws, entity_id, name, wait=15):
    """Give a new helper its display name. The helper is created with its key as name, so
    the entity id is input_*.solar_<key>; its registry entry appears a moment later."""
    deadline = time.monotonic() + wait
    while True:
        try:
            ws.call("config/entity_registry/update", entity_id=entity_id, name=name)
            return
        except m.Error:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def create_helpers(ws, start):
    """Create the helpers that are missing; never change existing ones or their values."""
    numbers = {i["id"] for i in ws.call("input_number/list")}
    created = []
    for key, (label, unit, lo, hi, step, initial) in HELPERS.items():
        item_id = f"solar_{key}"
        if item_id in numbers:
            continue
        ws.call("input_number/create", name=item_id, min=lo, max=hi, step=step,
                initial=initial, mode="box", unit_of_measurement=unit, icon="mdi:solar-power")
        rename(ws, f"input_number.{item_id}", label)
        created.append(f"input_number.{item_id}")
    if "solar_start" not in {i["id"] for i in ws.call("input_datetime/list")}:
        ws.call("input_datetime/create", name="solar_start", has_date=True, has_time=False,
                initial=start, icon="mdi:calendar-start")
        rename(ws, "input_datetime.solar_start", "Startdatum zonnepanelen")
        created.append("input_datetime.solar_start")
    m.log(f"created {len(created)} helpers" if created else "helpers already present")


def sql_entities(ws):
    """Entity ids of the SQL and template sensors in the packages, by unique_id."""
    return {e["unique_id"]: e["entity_id"] for e in ws.call("config/entity_registry/list")
            if e.get("platform") in ("sql", "template", "modbus")
            and str(e.get("unique_id", "")).startswith("solar_")}


def views(ids):
    """The solar views on the dashboard Elektriciteit (host/ha/dashboards.py)."""
    def e(uid):
        return ids.get(uid)

    def graph(title, uids, period, days):
        return {"type": "statistics-graph", "title": title, "chart_type": "bar",
                "entities": [e(u) for u in uids if e(u)], "stat_types": ["change"],
                "period": period, "days_to_show": days}

    def helpers(title, keys):
        return {"type": "entities", "title": title,
                "entities": [f"input_number.solar_{k}" for k in keys]}

    # Zonnepanelen (user's request, 2026-11-11): only the table Nu, Dag, Week and Maand as on
    # the overviews (generation and net benefit, solar_home) and per day what went to the grid
    # and what to the house, stacked.
    table = dashboards.now_table([
        (e("solar_home_pv_now"), "Nu"), (e("solar_growatt_power"), "_age:last_reported"),
        (e("solar_home_benefit_now"), "Kosten nu"),
        (e("solar_home_pv_today"), "Vandaag"), (e("solar_home_benefit_today"), "Kosten vandaag"),
        (e("solar_home_pv_week"), "Deze week"), (e("solar_home_benefit_week"), "Kosten deze week"),
        (e("solar_home_pv_month"), "Deze maand"), (e("solar_home_benefit_month"), "Kosten deze maand"),
    ], "kWh", "€")
    split = [{"entity": x, "name": n} for x, n in ((e("solar_total_export"), "Naar het net"),
                                                   (e("solar_total_self_use"), "Naar het huis")) if x]
    cards = [c for c in (table,) if c]
    if split:
        cards.append({"type": "statistics-graph", "title": "Per dag naar het net en naar het huis",
                      "chart_type": "bar-stack", "entities": split, "stat_types": ["change"],
                      "period": "day", "days_to_show": 30, "grid_options": {"columns": "full"}})
    overview = {"type": "sections", "max_columns": 4, "title": "Zonnepanelen",
                "path": "zonnepanelen", "icon": "mdi:solar-power",
                "sections": [{"type": "grid", "column_span": 4,
                              "cards": [{**c, "grid_options": {"columns": "full"}} for c in cards]}]
                } if cards else None
    contract = dashboards.page(
        "Contract", "contract", "mdi:file-compare",
        costs=[(e("solar_total_cmp_fixed"), "Huidig contract"),
               (e("solar_total_cmp_dynamic"), "Dynamisch contract")],
        details=[
            {"type": "markdown", "content":
                "Netto stroomkosten (inkoop min teruglevering plus terugleverkosten) over de minuten "
                "met een day-ahead-prijs. Vaste kosten per maand tellen niet mee. Vul de opslagen en "
                "de energiebelasting van het dynamische contract in onder *Instellingen*."},
            {"type": "entities", "title": "Day-ahead-prijzen",
             "entities": [e("solar_day_ahead_prices"), e("solar_day_ahead_missing")]},
        ])
    investment = [
        {"type": "entities", "title": "Tot nu toe", "entities": [
            e("solar_payback_ratio"), e("solar_break_even"),
            {"type": "attribute", "entity": e("solar_payback_ratio"), "attribute": "geinvesteerd",
             "name": "Geïnvesteerd", "suffix": "EUR"},
            {"type": "attribute", "entity": e("solar_payback_ratio"), "attribute": "opgeleverd",
             "name": "Opgeleverd", "suffix": "EUR"},
        ]},
        {"type": "entities", "title": "Prognose",
         "entities": [e("solar_npv"), e("solar_irr"), e("solar_payback_years")]},
        {"type": "markdown", "title": "Kasstroom per jaar",
         "content": f"{{{{ state_attr('{e('solar_forecast_table')}', 'tabel') }}}}"},
        helpers("Investering", ["investment", "extra_costs"]),
        helpers("Aannames prognose", ["expected_yield", "self_use_assumed", "degradation",
                                      "lifetime_panels", "lifetime_inverter",
                                      "inverter_replacement", "growth_import",
                                      "growth_feed_in", "discount_rate"]),
    ]
    surplus = [
        {"type": "entities", "title": "Batterij (simulatie, per jaar)", "entities": [
            e("solar_battery_saving"),
            {"type": "attribute", "entity": e("solar_battery_saving"), "attribute": "toelichting",
             "name": "Toelichting"},
        ]},
        helpers("Batterij", ["battery_capacity", "battery_power", "battery_efficiency"]),
        {"type": "markdown", "title": "Gemiddeld per uur van de dag (30 dagen)",
         "content": f"{{{{ state_attr('{e('solar_surplus_per_hour')}', 'tabel') }}}}"},
    ]
    settings = [
        {"type": "entities", "title": "Start", "entities": ["input_datetime.solar_start"]},
        helpers("Contract (incl. btw)", ["import_normal", "import_low", "feed_in",
                                         "feed_in_cost", "feed_in_cost_month"]),
        helpers("Dynamisch contract (vergelijking)", ["dyn_import_markup", "dyn_feed_in_markup",
                                                      "energy_tax", "vat"]),
        {"type": "markdown", "content":
            "Wijzig een tarief op de dag dat het ingaat: elke waarde geldt vanaf het moment dat "
            "je hem invult. Uitzondering: de eerste wijziging na de startdatum geldt vanaf de "
            "startdatum."},
    ]

    growatt = ["solar_growatt_power", "solar_growatt_energy_today", "solar_growatt_status",
               "solar_growatt_temperature", "solar_growatt_last_message"]
    strings = ["solar_growatt_pv1_voltage", "solar_growatt_pv1_current",
               "solar_growatt_pv2_voltage", "solar_growatt_pv2_current"]
    inverter = [
        {"type": "grid", "columns": 2, "square": False,
         "cards": [{"type": "tile", "entity": e(u)} for u in growatt]},
        {"type": "history-graph", "title": "Vermogen (24 uur)", "hours_to_show": 24,
         "entities": [e("solar_growatt_power")]},
        graph("Opwek per dag (30 dagen)", ["solar_growatt_energy_total"], "day", 30),
        graph("Opwek per maand (1 jaar)", ["solar_growatt_energy_total"], "month", 365),
        {"type": "history-graph", "title": "Strings (24 uur)", "hours_to_show": 24,
         "entities": [e(u) for u in strings]},
        {"type": "entities", "title": "Net", "entities": [
            e("solar_growatt_grid_voltage"), e("solar_growatt_frequency"),
            e("solar_growatt_energy_total")]},
    ]

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

    views = [("Investering", "investering", "mdi:finance", investment),
             ("Overschot", "overschot", "mdi:sun-clock", surplus),
             ("Instellingen", "instellingen", "mdi:cog", settings)]
    if e("solar_growatt_power"):
        views.insert(0, ("Omvormer", "omvormer", "mdi:solar-panel", inverter))
    if not ids:
        return []
    return ([v for v in (overview, contract) if v]
            + [{"title": t, "path": p, "icon": i, "cards": clean(c)} for t, p, i, c in views])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", default=DEFAULT_START,
                   help=f"start date of the investment, for a new helper (default {DEFAULT_START})")
    p.add_argument("--inverter", action="store_true",
                   help="also read the Growatt through Modbus TCP on its ShineLink (phase 5)")
    p.add_argument("--host", help=f"fixed IP address of the ShineLink (stored in {INVERTER})")
    a = p.parse_args()
    datetime.date.fromisoformat(a.start)
    if os.geteuid() != 0:
        raise m.Error("run as root (sudo)")
    token = m.read_env(m.ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise m.Error(f"no HA_TOKEN in {m.ROOT_ENV}; run 'sudo martha-ha token' first")

    check_schema()
    m.backup("before-solar")
    write_roles(token)
    storage_changed = patch_storage()
    add_nordpool(token)
    packages = list(PACKAGES)
    secret_changed = False
    if a.inverter:
        # Before the deploy: check_config needs the secret.
        secret_changed = write_secret(HOST_SECRET, inverter_host(a.host))
        packages.append(GROWATT_PACKAGE)
    deployed = deploy_packages(packages)
    if (storage_changed or secret_changed) and not deployed:
        restart()
    ws = m.WebSocket(m.HA_URL, token)
    try:
        create_helpers(ws, a.start)
        if a.inverter:
            write_pv_roles(ws)
        ids = sql_entities(ws)
        m.log(f"{len(ids)} solar sensors: " + ", ".join(sorted(ids.values())))
        dashboards.write(ws, token)
    finally:
        ws.close()
    # Fill solar_1m from the start now instead of waiting for the job.
    n = m.psql("SELECT solar_recompute(solar_start(), now())")
    m.log(f"calculated {n} minutes since the start")

if __name__ == "__main__":
    try:
        main()
    except m.Error as e:
        sys.exit(f"setup_solar: {e}")
