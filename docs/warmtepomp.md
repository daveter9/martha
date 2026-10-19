# CV-ketel tegen warmtepomp

Status: **gebouwd en live op martha (2026-10-19)**, met standaardwaarden. De vragen aan de
gebruiker staan onder *Open vragen*; de antwoorden gaan als helper in HA.

## Waarom

Bijhouden wat de verwarming met de CV-ketel nu werkelijk kost, en wat precies dezelfde warmte
zou kosten met een warmtepomp, per minuut. Zo valt een beslissing over een warmtepomp op eigen
meetdata in plaats van op gemiddelden. In HA moet te zien zijn hoe de berekening is gemaakt,
waar hij op rust, en wat er nodig is om hem te verfijnen. Dit sluit aan op V8 in
[zonnepanelen.md](zonnepanelen.md) (TCO van een warmtepomp met eigen opwek).

## Welke informatie nodig is

| Wat | Waarom | Bron nu | Beter |
|---|---|---|---|
| Gasverbruik per minuut | De warmtevraag van het huis | P1-meter, elke 5 minuten (gemeten) | Gasmeter per minuut bestaat niet; OpenTherm (brander, modulatie) |
| Deel warm water en koken | Een warmtepomp maakt warm water met een lagere COP; koken gaat naar inductie | Vaste m³ per dag (aanname) | Gemeten op dagen zonder verwarming (automatisch zichtbaar); OpenTherm (tapwater aan/uit) |
| Rendement van de ketel | Van gas naar nuttige warmte | Aanname (90 % verwarmen, 75 % warm water) | Merk, type, bouwjaar; aanvoer- en retourtemperatuur |
| Buitentemperatuur per minuut | COP en warmteverlies | Weer-entity van Met.no (gemeten), anders Open-Meteo, anders referentiejaar | Eigen buitensensor |
| Aanvoertemperatuur die het huis nodig heeft | Bepaalt de COP het sterkst | Stooklijn 30-50 °C (aanname) | Proef met lage aanvoer, warmteverliesberekening, OpenTherm |
| COP van de warmtepomp | Stroom per kWh warmte | Carnot-model, 45 % (aanname) | COP uit het datablad of de offerte |
| Vermogen van de warmtepomp | Wat er niet past, doet een element (COP 1) | Automatisch: benodigd bij -10 °C, minimaal 4 kW | Offerte |
| Gasprijs, stroomprijs (normaal/dal) | Kosten | Gemiddelde 2026 (aanname); stroom van het contract zodra ingevuld | Contract |
| Vaste kosten gasaansluiting | Vallen weg bij volledig elektrisch | Aanname € 25/maand | Jaarafrekening |
| Onderhoud | Jaarlijkse kosten | Aanname | Servicecontract, offerte |
| Zonne-overschot per minuut | Een warmtepomp kan eigen opwek gebruiken | `solar_1m` (gemeten, vanaf 2026-10-23) | – |
| Investering, subsidie | Terugverdientijd | Aanname | Offerte, rvo.nl |
| Gasverbruik vorig jaar | Controle van de prognose | – | Jaarafrekening |

## Beslissingen

Genomen door Claude op 2026-10-19, zonder overleg, omdat de gebruiker er niet bij was en wel vroeg
om zo ver mogelijk zelfstandig te bouwen. Ze volgen het patroon van de zonnepanelen (Z1, Z7–Z9).
**Te bevestigen door de gebruiker.**

| # | Beslissing | Afgewezen alternatieven |
|---|---|---|
| W1 | **Rekenlaag per minuut in TimescaleDB** (`heat_1m`, `heat_1d`), getoond via SQL-sensoren en HA-statistieken, net als `solar_1m`. | Template-sensoren in HA (geen historie, niet opnieuw te berekenen). |
| W2 | **Drie scenario's naast elkaar:** CV-ketel (werkelijk), volledig elektrische lucht/water-warmtepomp (gasaansluiting eraf, koken op inductie) en hybride. | Alleen volledig elektrisch: de hybride is in Nederland de gangbare eerste stap. |
| W3 | **Alle aannames zijn helpers** (`input_number.heat_*`), met tekst en standaardwaarde in de tabel `heat_param`. LTSS bewaart elke wijziging (`heat_setting`). | Vaste waarden in SQL. |
| W4 | **Modelwaarden gelden voor alle historie** (een correctie, dus alles opnieuw rekenen); **prijzen gelden vanaf het moment van wijzigen**, behalve de eerste wijziging: die vervangt de standaardwaarde ook voor het verleden. | Alles vanaf het moment van wijzigen (dan blijft een foute aanname in de historie staan). |
| W5 | **Buitentemperatuur van de weer-entity** (Met.no, al aanwezig) als template-sensor, zodat LTSS hem bewaart. Ontbreekt hij (geen internet), dan Open-Meteo-historie, anders het referentiejaar. | Een eigen buitensensor (nog niet aanwezig). |
| W6 | **Jaarprognose met het warmteverlies (W/K) uit de metingen** en de uurtemperaturen van een referentiejaar (2019). | Gemeten jaar extrapoleren (pas na een jaar mogelijk); graaddagen per maand (geeft de COP per uur niet). |
| W7 | **Zonne-overschot telt mee per minuut** (eerst overschot, tegen de gemiste terugleververgoeding), niet in de jaarprognose. | Overschot negeren. |

## Rekenmodel

Per minuut (`heat_recompute`, `host/db/heat.sql`). De formules staan ook op het dashboard.

1. **Gas.** De meter meldt elke 5 minuten zijn stand, voor de 5 minuten ervoor. HA krijgt hem
   binnen seconden, dus een stap in minuut *t* wordt gelijk verdeeld over *t−5 … t−1*.
2. **Waarvoor.** Koken en warm water zijn een vaste hoeveelheid m³ per dag, naar rato van het
   deel van de dag dat voorbij is; wat overblijft is verwarming. Dat aandeel geldt per dag voor
   elke minuut. Op een dag met minder gas dan warm water plus koken is er geen verwarming.
3. **Nuttige warmte** = gas × 9,769 kWh/m³ × rendement (verwarming of warm water).
4. **Buitentemperatuur:** de sensor, lineair geïnterpoleerd tussen waarden die hooguit 3 uur uit
   elkaar liggen (anders 3 uur vastgehouden); daarna Open-Meteo (`heat_weather`, per uur); daarna
   het referentiejaar. De bron staat per minuut in `temp_source`.
5. **Stooklijn:** aanvoer = min + (ontwerp − min) × (stookgrens − buiten) / (stookgrens + 10),
   begrensd tussen min en ontwerp.
6. **COP** = Carnot-rendement × (aanvoer + 273,15) / max(aanvoer − buiten, 5), × (1 − ontdooiverlies)
   onder 6 °C, tussen 1 en het maximum (7). Het Carnot-rendement volgt uit de COP bij A7/W35 als
   die is ingevuld. Warm water rekent met de boilertemperatuur (55 °C).
7. **Volledig elektrisch:** verwarming / COP + warm water / COP warm water + koken × 4,4 kWh/m³.
   Het vermogen geldt per uur en alleen voor verwarming (warm water komt uit een boilervat); wat
   er niet past, levert een element met COP 1.
8. **Hybride:** de warmtepomp verwarmt tot zijn vermogen per uur, maar alleen als warmte uit stroom
   goedkoper is dan uit gas (stroomprijs / COP < gasprijs / (9,769 × rendement)). Warm water,
   koken en de rest blijven op gas.
9. **Stroomprijs:** het contract van de zonnepanelen (normaal of dal, volgens het tarief van de
   P1-meter, en na 30 dagen de standaardregel: weekend en 23–07 uur dal); zolang dat op 0 staat
   de helper *Stroomprijs als het contract ontbreekt*. Eerst gaat stroom uit het zonne-overschot
   van die minuut, tegen de gemiste terugleververgoeding (`solar_1m.price_export_net`).
10. **Kosten:** gas × gasprijs; vaste kosten gasaansluiting per maand / minuten in de maand (CV en
    hybride); onderhoud per jaar / 525 960 minuten.

**Warmteverlies** (`heat_results`, elk uur): H = Σ warmte voor verwarming / Σ graaduren, over
volledige dagen met minstens 90 % gemeten temperatuur, met R² van de dagwaarden (lijn door 0).
Het benodigde vermogen is H × (stookgrens + 10).

**Prognose per jaar:** per uur van 2019: H × max(0, stookgrens − buiten), plus warm water en koken
per dag, met dezelfde COP-, vermogens- en hybride-regels, de huidige prijzen en het gemeten aandeel
daltarief. Ook: SCOP, besparing, netto investering (investering − subsidie − vermeden vervanging) en
terugverdientijd. Met *Gasverbruik laatste volle jaar* ingevuld staat er een controle naast.

**Referentiejaar:** uurtemperaturen van 2019 op de locatie van `zone.home` (Open-Meteo, ERA5,
CC BY 4.0, in `host/db/heat_reference.sql`). Van 2016–2025 ligt 2019 met 2010 graaddagen (basis
16 °C) het dichtst bij het gemiddelde (1995).

## Zoals gebouwd (2026-10-19)

- **Bestanden:** `host/db/heat.sql` (schema, functies, jobs), `host/db/heat_reference.sql`
  (referentiejaar), `host/ha/packages/heat.yaml` (buitentemperatuur en SQL-sensoren),
  `host/ha/setup_heat.py` (uitrol). Uitrollen: zie *Warmtepomp* in de readme.
- **Jobs:** `heat_job` (elke 5 minuten) rekent de minuten sinds de vorige run en altijd vanaf de
  middernacht vóór de laatste 6 uur, en na een gewijzigde instelling (heat-helpers of de
  stroomtarieven van de zonnepanelen) alle historie. `heat_results_job` (elk uur) maakt de fit en
  de prognose; in automatische modus rekent hij alles opnieuw als het vermogen meer dan 10 %
  verschuift. Alles opnieuw over 12 dagen kost ongeveer 1 seconde; over een jaar naar schatting 30.
- **Dashboard** `/warmte-pomp` (HA eist een streepje): *Vergelijking* (kosten per periode, dag en
  maand), *Berekening* (één minuut stap voor stap met zijn getallen, plus de formules),
  *Prognose*, *Aannames* (elke aanname met waarde, standaard of ingevuld, uitleg en hoe te
  verfijnen, plus de datakwaliteit) en *Instellingen*.
- **Getest op martha (2026-10-19):** eerst in een transactie die werd teruggedraaid, daarna live.
  Op 11,7 dagen echte P1-data: 8,66 m³ gas, 59,9 kWh warmte; kosten tot nu toe CV € 26,13,
  warmtepomp € 10,10, hybride € 25,38. De prijshistorie (standaard → eerste wijziging voor alle
  historie → latere wijziging vanaf dat moment) en de volledige herberekening na een wijziging
  werken. De package slaagde voor `check_config` en de health-check. De sensorqueries kosten
  hooguit 34 ms.
- **Eerste uitkomst (nog onzeker):** H = 18 W/K (R² 0,22 over 11 dagen). Dat is erg laag; in
  oktober is er nauwelijks gestookt (0,2–1,6 m³ per dag), dus het meeste gas valt nu onder warm
  water. De prognose (355 m³ gas per jaar) is pas bruikbaar na een maand stookseizoen, of na
  controle tegen de jaarafrekening.

## Open vragen

Op 2026-10-19 via Telegram aan de gebruiker gesteld (de bot van Hermes, rechtstreeks via de
Bot-API). De antwoorden gaan als helper in HA, onder *Warmtepomp → Instellingen*.

1. Koken op gas of inductie? → *Gas voor koken per dag* (0 bij inductie).
2. Merk, type en bouwjaar van de ketel; combiketel of aparte boiler? → rendementen.
3. Radiatoren of vloerverwarming; op welke aanvoertemperatuur staat de ketel? → stooklijn.
4. Contracttarieven: gas all-in, vaste kosten gas per maand, stroom normaal/dal. → prijzen (stroom
   op het dashboard Zonnepanelen).
5. Gasverbruik van het laatste volle jaar → *Gasverbruik laatste volle jaar* (controle).
6. Welke thermostaat (OpenTherm)? → mogelijk de echte aanvoertemperatuur en brander/tapwater meten.
7. Offerte of voorkeur (volledig of hybride, vermogen, COP, prijs, subsidie)? → warmtepomp en investering.
8. Aantal personen en douchegedrag → *Gas voor warm water per dag*.

## Mogelijke verfijningen

- **OpenTherm-gateway** (bijvoorbeeld een OTGW met de integratie in HA): brander aan/uit, modulatie,
  tapwater of verwarming, aanvoer- en retourtemperatuur. Dan is de splitsing warm water/verwarming
  per minuut gemeten in plaats van aangenomen, en is de nodige aanvoertemperatuur bekend. Vraagt
  hardware en mogelijk een wheel in de bundle (C21).
- **Dynamisch contract** voor de warmtepomp: met `energy_price` per kwartier (Z5), net als V1.
- **Wind en zon** in het warmteverlies (meervoudige regressie), zodra er een stookseizoen aan data is.
- **Bijhalen van Open-Meteo** na een periode zonder internet gebeurt nu alleen bij een nieuwe run
  van `setup_heat.py`; tot die tijd vult het referentiejaar het gat.
