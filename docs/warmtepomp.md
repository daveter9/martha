# CV-ketel tegen warmtepomp

Status: **gebouwd en live op martha (2026-10-19)**, bijgewerkt met de antwoorden van de
gebruiker (zelfde dag).

## Waarom

Bijhouden wat verwarming en warm water met de CV-ketel nu werkelijk kosten, en wat precies
dezelfde warmte zou kosten met een volledig elektrische warmtepomp, per minuut. Zo valt een
beslissing over een warmtepomp op eigen meetdata in plaats van op gemiddelden. In HA moet te zien
zijn hoe de berekening is gemaakt, waar hij op rust, en wat er nodig is om hem te verfijnen.
Dit sluit aan op V8 in [zonnepanelen.md](zonnepanelen.md) (TCO van een warmtepomp met eigen opwek).

## Gegevens van de gebruiker (2026-10-19)

| Wat | Antwoord | Gevolg |
|---|---|---|
| Koken | Inductie | Koken telt niet mee: het verandert niet met een warmtepomp. |
| Ketel | Vaillant HR-combiketel uit 2019 | Rendementen 90 % (verwarmen) en 75 % (warm water) blijven staan. |
| Afgifte | Radiatoren, aanvoer 55 °C | Stooklijn van de warmtepomp: 55 °C bij -10 °C. |
| Contract | Vast: gas € 1,27/m³, stroom € 0,27/kWh (normaal en dal), all-in | Helpers ingevuld; stroom ook bij Zonnepanelen. **Daarnaast de marktprijs.** |
| Gasverbruik per jaar | Ongeveer 800 m³ | IJking van het warmteverlies zolang de meting onzeker is. |
| Thermostaat | Honeywell | Type onbekend; OpenTherm mogelijk een verfijning. |
| Warmtepomp | Geen offerte; vergelijken met **all-electric** | Geen hybride, geen investering of terugverdientijd. |
| Douchen | Afleiden uit het werkelijke gebruik | Warm water wordt per meldinterval herkend, niet aangenomen. |

## Welke informatie nodig is

| Wat | Waarom | Bron nu | Beter |
|---|---|---|---|
| Gasverbruik per minuut | De warmtevraag | P1-meter, elke 5 minuten (gemeten) | OpenTherm (brander, modulatie) |
| Deel warm water | Een warmtepomp maakt warm water met een lagere COP | Herkend in de 5-minutendata (gemeten) | OpenTherm (tapwater aan/uit) |
| Rendement van de ketel | Van gas naar nuttige warmte | Aanname bij een HR-combiketel uit 2019 | Aanvoer- en retourtemperatuur |
| Buitentemperatuur per minuut | COP en warmteverlies | Weer-entity van Met.no (gemeten), anders Open-Meteo, anders referentiejaar | Eigen buitensensor |
| Aanvoertemperatuur | Bepaalt de COP het sterkst | 55 °C bij -10 °C (gebruiker), 30 °C bij de stookgrens | Proef met lagere aanvoer, OpenTherm |
| COP van de warmtepomp | Stroom per kWh warmte | Carnot-model, 45 % (aanname) | COP uit een datablad |
| Vermogen van de warmtepomp | Wat er niet past, doet een element | Automatisch, minimaal 4 kW | Offerte |
| Contractprijzen | Kosten | Ingevuld door de gebruiker | – |
| Marktprijzen | Vergelijking met een dynamisch contract | Nord Pool (stroom, per kwartier), EnergyZero (gas, per uur), plus opslag, energiebelasting en btw | Opslagen van een concreet contract |
| Vaste kosten gasaansluiting | Vallen weg met een warmtepomp | Aanname € 25/maand | Jaarafrekening |
| Onderhoud | Jaarlijkse kosten | Aanname | Servicecontract, offerte |
| Zonne-overschot per minuut | Een warmtepomp kan eigen opwek gebruiken | `solar_1m` (vanaf 2026-10-23) | – |
| Gasverbruik vorig jaar | IJking en controle van het warmteverlies | ~800 m³ (gebruiker) | Exact getal van de jaarafrekening |

## Beslissingen

W1–W7 zijn genomen door Claude op 2026-10-19, toen de gebruiker er niet bij was; W2 en W7 zijn
daarna aangepast op aanwijzing van de gebruiker.

| # | Beslissing | Afgewezen alternatieven |
|---|---|---|
| W1 | **Rekenlaag per minuut in TimescaleDB** (`heat_1m`, `heat_1d`), getoond via SQL-sensoren en HA-statistieken, net als `solar_1m`. | Template-sensoren in HA (geen historie, niet opnieuw te berekenen). |
| W2 | **Twee installaties: CV-ketel (werkelijk) en volledig elektrische lucht/water-warmtepomp** (gasaansluiting eraf). *Gewijzigd (gebruiker, 2026-10-19):* eerst ook een hybride, en investering en terugverdientijd; die zijn weg (C24). | Hybride. |
| W3 | **Alle aannames zijn helpers** (`input_number.heat_*`), met tekst en standaardwaarde in de tabel `heat_param`. LTSS bewaart elke wijziging (`heat_setting`). | Vaste waarden in SQL. |
| W4 | **Modelwaarden gelden voor alle historie** (een correctie, dus alles opnieuw rekenen); **prijzen gelden vanaf het moment van wijzigen**, behalve de eerste wijziging: die vervangt de standaardwaarde ook voor het verleden. | Alles vanaf het moment van wijzigen. |
| W5 | **Buitentemperatuur van de weer-entity** (Met.no) als template-sensor, zodat LTSS hem bewaart. Ontbreekt hij, dan Open-Meteo-historie, anders het referentiejaar. | Een eigen buitensensor (nog niet aanwezig). |
| W6 | **Jaarprognose met het warmteverlies (W/K)** en de uurtemperaturen van een referentiejaar (2019). Zolang de meting onzeker is, geijkt op het gasverbruik van vorig jaar. | Gemeten jaar extrapoleren (pas na een jaar mogelijk). |
| W7 | **Zonne-overschot telt mee per minuut** (eerst overschot, tegen de gemiste terugleververgoeding), niet in de jaarprognose. | Overschot negeren. |
| W8 | **Warm water gemeten** uit het patroon van de gasmeter (gebruiker, 2026-10-19). | Een vaste hoeveelheid per dag. |
| W9 | **Elke minuut twee keer geprijsd:** contract en marktprijs (gebruiker, 2026-10-19). Marktprijs = (day-ahead + opslag + energiebelasting) × (1 + btw); stroom van Nord Pool (al aanwezig), gas van EnergyZero via een REST-sensor (geen sleutel, geen extra integratie). | De EnergyZero-integratie (maakt veel sensoren die niet nodig zijn, C24). |

## Rekenmodel

Per minuut (`heat_recompute`, `host/db/heat.sql`). De formules staan ook op het dashboard.

1. **Gas.** De meter meldt elke 5 minuten zijn stand, voor de 5 minuten ervoor. HA krijgt hem
   binnen seconden, dus een stap in minuut *t* wordt gelijk verdeeld over *t−5 … t−1*.
2. **Warm water of verwarming** (`heat_gas`), per melding:
   - **tapwater:** gemiddeld minstens 8 kW over de 5 minuten, in een reeks van hooguit 20 minuten.
     Een combiketel brandt dan (bijna) vol; een douchebeurt is 10-22 kW;
   - **los:** een melding zonder verbruik 10 minuten ervoor en erna (kort tappen, of het
     warmhouden van de ketel: elke ~90 minuten 0,005 m³);
   - **verwarming:** de rest, ook een lange reeks op hoog vermogen (opwarmen na de nacht).

   Tapwater en los tellen als warm water. Gevonden in de data van 17–19 oktober: verwarmen gaat
   op 1-5 kW, douchebeurten op 10-22 kW gedurende 10-20 minuten.
3. **Nuttige warmte** = gas × 9,769 kWh/m³ × rendement (verwarming of warm water).
4. **Buitentemperatuur:** de sensor, lineair geïnterpoleerd tussen waarden die hooguit 3 uur uit
   elkaar liggen (anders 3 uur vastgehouden); daarna Open-Meteo (`heat_weather`, per uur); daarna
   het referentiejaar. De bron staat per minuut in `temp_source`.
5. **Stooklijn:** aanvoer = min + (ontwerp − min) × (stookgrens − buiten) / (stookgrens + 10),
   begrensd tussen min en ontwerp.
6. **COP** = Carnot-rendement × (aanvoer + 273,15) / max(aanvoer − buiten, 5), × (1 − ontdooiverlies)
   onder 6 °C, tussen 1 en het maximum (7). Warm water rekent met de boilertemperatuur (55 °C).
7. **Warmtepomp:** verwarming / COP + warm water / COP warm water. Het vermogen geldt per uur en
   alleen voor verwarming (warm water komt uit een boilervat); wat er niet past, levert een element
   met COP 1.
8. **Contractprijs:** gas uit de helper; stroom van Inkoopprijs normaal/dal (Zonnepanelen), volgens
   het tarief van de P1-meter (na 30 dagen: weekend en 23–07 uur dal), en voor de tijd dat die op 0
   stonden de helper *Stroomprijs contract*.
9. **Marktprijs:** (day-ahead + opslag + energiebelasting) × (1 + btw). Stroom: de prijs van het
   kwartier (`energy_price`). Gas: de prijs van het uur (`heat_gas_market`), of de laatste van de 48
   uur ervoor. Zonder marktprijs blijft de marktkost van die minuut leeg.
10. **Zonnestroom:** eerst uit het overschot van die minuut, tegen de gemiste terugleververgoeding
    (`solar_1m.price_export_net`), voor contract en markt.
11. **Vaste kosten:** gasaansluiting per maand / minuten in de maand (alleen CV); onderhoud per jaar
    / 525 960 minuten.

**Warmteverlies** (`heat_results`, elk uur): H = Σ warmte voor verwarming / Σ graaduren, over
volledige dagen met minstens 90 % gemeten temperatuur, met R² van de dagwaarden. Zolang dat minder
dan 30 dagen zijn of R² onder 0,5 ligt, wordt H geijkt op het jaarverbruik: (jaarverbruik − gemeten
warm water × 365) × 9,769 × rendement / Σ graaduren van het referentiejaar. Benodigd vermogen:
H × (stookgrens + 10).

**Prognose per jaar:** per uur van 2019: H × max(0, stookgrens − buiten), plus het gemeten warm
water per dag, met dezelfde COP- en vermogensregels. Contractprijzen van nu, en de gemiddelde
marktprijs van de laatste 30 dagen.

**Referentiejaar:** uurtemperaturen van 2019 op de locatie van `zone.home` (Open-Meteo, ERA5,
CC BY 4.0, in `host/db/heat_reference.sql`). Van 2016–2025 ligt 2019 met 2010 graaddagen (basis
16 °C) het dichtst bij het gemiddelde (1995).

## Zoals gebouwd (2026-10-19)

- **Bestanden:** `host/db/heat.sql` (schema, functies, jobs), `host/db/heat_reference.sql`
  (referentiejaar), `host/ha/packages/heat.yaml` (buitentemperatuur, gasmarktprijs en
  SQL-sensoren), `host/ha/setup_heat.py` (uitrol). Uitrollen: zie *Warmtepomp* in de readme.
- **Gasmarktprijs:** een REST-sensor haalt elk uur de prijzen van de laatste 3 dagen en morgen op bij
  EnergyZero (zonder btw). LTSS schrijft hem naar `ltss`, en de trigger `heat_capture` zet de prijzen
  in `heat_gas_market` (getest). Gemiste uren worden bij de volgende geslaagde ophaalbeurt
  aangevuld, tot 3 dagen terug; `setup_heat.py` haalt alles sinds de eerste gasdata.
- **Jobs:** `heat_job` (elke 5 minuten) rekent de minuten sinds de vorige run en altijd vanaf de
  middernacht vóór de laatste 6 uur, en na een gewijzigde instelling alle historie.
  `heat_results_job` (elk uur) maakt de fit en de prognose.
- **Dashboard** Verwarming (`/huis-verwarming`, C25): *Gas*, *Kosten* (verwarming, contract en markt per periode,
  dag en maand, de splitsing per dag, en het totaal met vaste kosten),
  *Berekening* (één minuut stap voor stap, met de tapbeurten van die dag, plus de formules),
  *Prognose*, *Aannames* (elke aanname met waarde, standaard of ingevuld, uitleg en hoe te
  verfijnen, plus de datakwaliteit) en *Instellingen*. Het warm water staat apart op het dashboard
  Tapwater (`/huis-tapwater`): gas en kosten voor warm water, per dag en per jaar, met de instellingen
  van de herkenning.
- **Splitsing verwarming en tapwater (C26):** per minuut `cv_cost_dhw`, `wp_cost_dhw` (en tegen
  marktprijs) en `wp_kwh_dhw`; verwarming = totaal min tapwater. Het zonne-overschot wordt naar
  verhouding van de kWh verdeeld. Vaste kosten horen bij geen van beide, dus totaal = verwarming +
  tapwater + vast (getest: het verschil is 0). `heat_split_days()` geeft de laatste 14 dagen als
  tabel, `year_split` de prognose per jaar.
- **Getest op martha (2026-10-19):** in transacties die werden teruggedraaid: de migratie vanaf de
  eerste versie, een verse installatie en opnieuw toepassen. Daarna live. De herkenning van warm
  water klopt met de gezien douchebeurten (bijv. 19-10 10:25–10:40, 0,485 m³).
- **Uitkomst over 11,8 dagen:** gas 8,73 m³, waarvan 5,56 m³ warm water (0,43 m³ per dag). Kosten:
  CV € 24,44 (markt € 29,13), warmtepomp € 9,34 (markt € 10,74), inclusief vaste kosten.
- **Prognose per jaar (geijkt op 800 m³):** H = 113 W/K (gemeten tot nu toe 22 W/K, R² 0,39, nog
  onbetrouwbaar: in oktober wordt nauwelijks gestookt). CV € 1.436 per jaar, warmtepomp € 634
  (contract); € 1.856 tegen € 785 (marktprijs). SCOP 3,8; 1.793 kWh stroom per jaar.

## Mogelijke verfijningen

- **OpenTherm** (als de Honeywell-thermostaat het ondersteunt, met een gateway): brander,
  modulatie, tapwater of verwarming, aanvoer- en retourtemperatuur. Dan is de splitsing exact en
  is de nodige aanvoertemperatuur bekend. Vraagt hardware en mogelijk een wheel in de bundle (C21).
- **Wind en zon** in het warmteverlies, zodra er een stookseizoen aan data is.
- **Bijhalen van Open-Meteo** na een lange periode zonder internet gebeurt nu alleen bij een nieuwe
  run van `setup_heat.py`; tot die tijd vult het referentiejaar het gat.
