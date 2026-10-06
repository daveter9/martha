# ADR-001: Opslag van energie- en sensordata in Home Assistant

6 okt 2026

## Status
Geaccepteerd

## Context
Ik wil meetwaarden van meerdere bronnen langdurig opslaan, analyseren en apparaten via
Home Assistant aansturen.

- **Bronnen:** P1-meter (Landis+Gyr E360), omvormer (Growatt MIN 3000TL-XH), warmtepomp,
  en later losse sensoren (temperatuur, CO₂).
- **Standaardopslag van HA:** de Recorder (SQLite) bewaart ruwe states standaard 10 dagen
  en long-term statistics (5 min/uur) onbeperkt. Dat volstaat voor het energiedashboard,
  maar niet voor ruwe data op lange termijn of vrije analyses.
- **Analysebehoefte:** verbruik combineren met andere data, zoals EPEX day-ahead-prijzen
  voor kosten- en batterij-arbitrage. Dat vraagt joins.
- **Sturing:** ik wil bestaande HA-integraties gebruiken voor uitlezen én aansturen
  (bijv. Growatt-vermogen begrenzen). Per fysieke interface (RS485, seriële P1-poort)
  kan maar één eigenaar zijn.
- **Volume (schatting):** orde 275.000 punten per dag bij 10–60 s per bron, met
  compressie honderden MB tot ~1 GB per jaar.

### Overwogen architecturen

| Optie | Opzet | Afgevallen omdat |
|---|---|---|
| A. HA als hub | HA-integraties lezen alle apparaten; HA stuurt data door naar opslag | — gekozen |
| B. MQTT-databus | Eigen bridges per apparaat publiceren op MQTT; HA en opslag zijn afnemers | Vervangt HA-integraties door zelfbouw-bridges; meer beheer |
| Alleen Recorder | SQLite of Postgres als enige opslag | Ruwe data verdwijnt; HA-schema niet voor analyse bedoeld |

### Overwogen databases

| | InfluxDB 2.x | TimescaleDB |
|---|---|---|
| Querytaal | Flux (niet meer doorontwikkeld) | Standaard SQL |
| Joins met prijzen/weer | Lastig | Gewoon `JOIN` |
| Downsampling | Tasks + buckets | Continuous aggregates |
| HA-koppeling | Officiële integratie | LTSS (HACS) of via MQTT/Telegraf |
| Toekomstvastheid | Onrustig (1.x → 2.x → 3.x herschrijving) | Stabiel (PostgreSQL) |

## Besluit
Ik kies voor architectuur A: Home Assistant is via zijn integraties eigenaar van alle
apparaten, en TimescaleDB is de opslag voor de lange termijn.

- **Uitlezen en sturen:** elk apparaat via een HA-integratie. Sturing (zoals de
  Growatt-limiet bij negatieve prijzen) loopt via HA-automatiseringen.
- **Naar Timescale:** start met LTSS (HACS). Als terugvaloptie zonder HACS:
  `mqtt_statestream` → Telegraf → Timescale.
- **Recorder:** op dezelfde PostgreSQL-server, zodat er één database en één back-up is.
- **Granulariteit:** throttle bij de bron (`scan_interval`, DSMR-update-interval),
  filter met include/exclude en `ignore_attributes`.
- **Retentie:** ruwe data 30 dagen, continuous aggregates per minuut (2 jaar) en per uur
  (onbeperkt). Tellers aggregeren met `last`, momentane waarden met `avg`.
- **Conventies:** correcte `state_class`/`device_class`, vaste eenheden (W, kWh),
  database op SSD, niet op SD-kaart.

## Gevolgen

### Positief
- Bestaande HA-integraties dekken uitlezen én sturen; nauwelijks zelfbouw.
- Eén plek voor configuratie, eenheden en automatiseringen; nieuwe sensoren komen via de
  include-lijst mee.
- SQL met joins maakt analyses tegen EPEX-prijzen en batterijscenario's in één query mogelijk.
- PostgreSQL is stabiel en breed ondersteund (Grafana, Python, eigen apps); geen
  migratierisico zoals bij Flux.
- Eén databaseserver voor Recorder en Timescale.

### Negatief
- Valt HA uit, dan ontstaat een gat in de ruwe data. kWh-tellers zijn cumulatief, dus
  totalen herstellen zich; alleen vermogensverloop in het gat ontbreekt.
- LTSS is een community-integratie zonder onderhoudsgarantie. Mitigatie: overstap naar
  `mqtt_statestream` → Telegraf.
- HA schrijft alleen bij wijziging; queries moeten gaten vullen met de vorige waarde.
- Meer beheer dan alleen SQLite: PostgreSQL + Timescale-extensie, retentie- en
  aggregatiebeleid, back-up.

## Openstaand
- Merk en type warmtepomp, en welke lokale integratie (Modbus, eBUS, API) beschikbaar is.
- Of de Growatt via een HA-Modbus-integratie of via Grott wordt uitgelezen, gezien de
  RS485-poort mogelijk door de ShineLink bezet is.

## Uitwerking (6 okt 2026)
Bij de implementatie zijn twee punten aangepast aan de offline-eis (C1/C1a in
[DESIGN.md](../../DESIGN.md)):

- **LTSS niet via HACS.** HACS heeft internet nodig. LTSS (v2.1.1) staat daarom in de
  bundle (`offline/custom_components/ltss`), en `install.sh` kopieert hem naar de
  HA-config. De Python-pakketten die LTSS vraagt en die niet in het HA-image zitten
  (`psycopg2-binary`, `geoalchemy2`), staan als wheels in de bundle (`offline/wheels`).
- **`ignore_attributes`** kent LTSS niet. Overbodige attributen worden daarom in de
  database weggefilterd, met een trigger op de tabel `ltss`.

De details staan in DESIGN.md onder *Opslag van meetdata (ADR-001)*.
