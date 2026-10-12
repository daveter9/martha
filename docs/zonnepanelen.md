# Zonnepanelen: eisen

Status: **concept**; eerste ronde vragen beantwoord op 2026-10-12. De panelen worden
geplaatst op vrijdag 2026-10-23.

## Waarom

Weten wat de panelen opleveren en wat ze kosten, zodat ik beslissingen kan nemen op basis van
eigen meetdata in plaats van op gemiddelden van anderen. Concreet wil ik deze vragen kunnen
beantwoorden:

| # | Vraag | Gevoed door |
|---|---|---|
| V1 | Moet ik overstappen van energieleverancier (terugleververgoeding, terugleverkosten, dynamisch contract)? | Doel 1, 2 |
| V2 | Moet ik de omvormer op bepaalde momenten uitzetten (negatieve prijzen, terugleverkosten hoger dan de vergoeding)? | Doel 2, 6 |
| V3 | Wat wordt mijn energierekening de komende jaren? | Doel 1, 2, 5 |
| V4 | Hoeveel verminderen de panelen mijn uitgaven? (voor het huishoudbudget) | Doel 1, 2, 3 |
| V5 | Was het een goede investering, en hoe vertel ik daar eerlijk over aan anderen? | Doel 3, 5 |
| V6 | Vervang ik de omvormer aan het eind van zijn levensduur? | Doel 3, 5 |
| V7 | Vervang ik de panelen aan het eind van hun levensduur? | Doel 3, 5 |
| V8 | Wat is de TCO van een warmtepomp, als die eigen opwek kan gebruiken? | Doel 4 |
| V9 | Wat is de TCO van een elektrische auto, als die eigen opwek kan gebruiken? | Doel 4 |
| V10 | Is een thuisbatterij winstgevend? | Doel 4 |
| V11 | Hoeveel ga ik morgen / komend jaar opwekken? | Doel 5 |

## Context die de eisen bepaalt

- **De salderingsregeling stopt op 2027-01-01.** Tot die datum wordt teruglevering verrekend
  met inkoop (tegen het volle tarief, inclusief energiebelasting). Daarna krijg je alleen de
  terugleververgoeding van de leverancier (wettelijk tot 2030 minimaal 50% van de kale
  leveringsprijs), en veel leveranciers rekenen terugleverkosten. Een teruggeleverde kWh is
  vanaf 2027 dus veel minder waard dan een zelf gebruikte kWh. Daarom draait bijna alles
  hieronder om de splitsing **eigen gebruik versus teruglevering**, per moment.
- **De P1-meter ziet alleen het net.** Hij meet inkoop en teruglevering, maar niet wat de
  panelen opwekken of wat je direct zelf verbruikt. Dat zie je pas als je ook de opwek
  (van de omvormer) meet.
- Bestaande meetopslag: LTSS → `ltss_1m` (per minuut, onbeperkt) en `ltss_1h` (zie *Opslag van
  meetdata* en *P1-meter* in [DESIGN.md](../DESIGN.md)).
- C1a: HA mag internet gebruiken (prijzen, weersvoorspelling), maar moet zonder blijven werken.
- **Omvormer:** Growatt MIN 3000TL-XH (max. 3 kW AC, "battery-ready" voor een Growatt
  ARK-batterij) met een ShineLAN-X-datalogger (LAN-kabel), die standaard naar de Growatt-cloud
  stuurt.
- **Energiecontract:** vast.

## Beslissingen

Genomen met de gebruiker op 2026-10-12.

| # | Beslissing | Gevolg |
|---|---|---|
| Z1 | **Alle overzichten en berekeningen staan in HA.** | Het energiedashboard van HA plus eigen dashboards. Wat HA niet zelf kan (eigen gebruik in €, NPV, IRR, simulaties) wordt een SQL-functie in TimescaleDB, getoond via SQL-sensoren. Geen extra container (C6 blijft) en geen analyse buiten HA. |
| Z2 | **Vanaf de start rekenen met het regime van 2027**, ook in nov–dec 2026. | Eén rekenmodel. De opbrengst van die twee maanden komt iets te laag uit; in de winter wordt weinig opgewekt, dus dat is geaccepteerd. |
| Z3 | **Eén instelbare discontovoet** voor de NPV. | Een invoerwaarde in HA, geen scenario's per discontovoet. |
| Z4 | **Het verbruiksprofiel is het complete huisprofiel**, uit P1 en opwek. | Geen metingen of standaardprofielen per apparaat. Doel 4 vergelijkt het overschot met het totale verbruik per minuut. |
| Z5 | **Prijzen per kwartier (day-ahead) worden opgehaald en voor altijd bewaard**, ook met een vast contract. | Nodig om te vergelijken met een dynamisch contract (V1) en om negatieve prijzen te zien (V2). Zonder internet ontbreken ze en worden ze later bijgehaald (C1a). Het vaste contract blijft de basis voor de werkelijke kosten. |

Ontwerpkeuzes voor de bouw in HA, genomen met de gebruiker op 2026-10-12 (uitwerking in
*Ontwerp in HA* hieronder):

| # | Beslissing | Afgewezen alternatieven |
|---|---|---|
| Z6 | **De Growatt wordt lokaal gelezen met Grott**, via de ShineLAN-X. | Modbus RS485: een kabel naar de omvormer, en hij botst met de RS485-meter van een latere Growatt-batterij. Growatt-cloud: alleen met internet (C1a). |
| Z7 | **Tarieven en investeringsgegevens zijn helpers in HA**; LTSS bewaart elke wijziging. | Een tabel in de database (wijzigen via de repo). Een correctietabel ernaast. |
| Z8 | **De rekenlaag is een tabel per minuut** (`solar_1m`). | Per kwartier (minder rijen, sluit aan op de prijzen). Alleen views (traag over jaren). |
| Z9 | **Perioden via HA-statistieken**: SQL-sensoren geven lopende totalen, HA maakt er statistieken van. | Begin- en einddatum-helpers. |
| Z10 | **Day-ahead-prijzen via de Nord Pool-integratie**, met een DB-trigger naar een permanente tabel. | EnergyZero. Een eigen ophaaljob op martha. |

## Ontwerp in HA

```
ShineLAN-X ──> Grott (martha) ──> Growatt-cloud (als er internet is)
                  │ webhook
                  v
Growatt-sensoren ─┐
P1-sensoren ──────┼─> LTSS ─> ltss_1m ─┐
helpers (Z7) ─────┘                    ├─ job ─> solar_1m ─> SQL-functies ─> SQL-sensoren
Nord Pool (Z10) ────> LTSS ─> trigger ─> energy_price                              │
                                                                                   v
                          energiedashboard (kWh) + dashboard "Zonnepanelen" (HA-statistieken)
```

Zoals bij de P1-meter zet een script `host/ha/setup_solar.py` alles neer: packages via
martha-ha (back-up, `check_config`, health-check, rollback), helpers en dashboards via de
websocket. Het schema komt in `host/db/solar.sql`, idempotent, toegepast door `install.sh`
na `timescale.sql`.

### Growatt via Grott (Z6)
- **Grott** (open source, Python) draait als systemd-service op martha (`martha-grott.service`,
  eigen gebruiker), niet als container (C6). De broncode staat op versie vastgepind in de bundle
  (C1); `download.ps1` haalt hem op.
- **De ShineLAN-X** stuurt naar martha in plaats van naar `server.growatt.com` (in te stellen
  in de webinterface van de stick). De stick kent geen `.local`, dus martha heeft een vast
  IP-adres nodig (DHCP-reservering in de router). Grott stuurt de berichten door naar Growatt,
  zodat de Growatt-app blijft werken.
- **Naar HA zonder MQTT:** een Grott-extensie stuurt elke meting als JSON naar een webhook van
  HA (`local_only`). In `packages/solar_growatt.yaml` vangen trigger-template-sensoren die op,
  met `unique_id`, `device_class` en `state_class`: opbrengst totaal en vandaag (kWh), AC-vermogen
  (W), spanning en stroom per string, temperatuur en status.
- **Interval:** de stick stuurt standaard eens per 5 minuten; waar het kan, gaat dat naar 1
  minuut.
- **Opwek per minuut:** de Growatt-tellers hebben een resolutie van 0,1 kWh, en dat is te grof
  per minuut (3 kW is 0,05 kWh per minuut). Daarom rekent `solar_1m` de opwek per minuut uit
  het AC-vermogen, en schaalt hij elke dag zo dat de som klopt met de dagteller. Tussen twee
  berichten wordt het vermogen lineair geïnterpoleerd.
- **Sturen (doel 6)** kan alleen als Grott de cloud helemaal vervangt (servermodus). Dat
  besluit valt pas in de laatste fase.

### Helpers (Z7)
Gemaakt door `setup_solar.py`; ze staan in `.storage` en daarmee in de config-repo. In de UI
pas je ze aan wanneer er iets verandert. LTSS bewaart elke wijziging voor altijd; daarvoor
komen `input_number` en `input_datetime` in de include van `martha_storage.yaml`. Rekenen
gebeurt steeds met de waarde die op dat moment gold. Een tarief achteraf corrigeren kan dus
niet: wijzig het op de dag dat het ingaat.

- Tarieven (all-in, incl. btw, €/kWh): inkoop normaal, inkoop dal, terugleververgoeding,
  terugleverkosten per kWh. Terugleverkosten per maand (€) als die vast zijn.
- Investering: startdatum, investeringsbedrag, bijkomende kosten (totaal tot nu toe; ophogen
  bij een nieuwe uitgave, LTSS legt vast wanneer).
- Voorspelling: discontovoet (Z3), degradatie (%/jaar), levensduur panelen en omvormer,
  vervangingskosten omvormer, prijsontwikkeling inkoop en teruglevering (%/jaar).
- Simulatie: batterijcapaciteit (kWh), laad-/ontlaadvermogen (kW), rendement (%).

### Rekenlaag `solar_1m` (Z8)
- Een tabel (hypertable, compressie na 30 dagen, onbeperkt bewaard), gevuld door een
  TimescaleDB-job (`add_job`) die elke 5 minuten de laatste uren opnieuw berekent (upsert),
  zodat late metingen meetellen. `solar_recompute(van, tot)` rekent een periode opnieuw uit.
- Per minuut: opwek, inkoop (normaal/dal), teruglevering, eigen gebruik (≥ 0), verbruik, het
  actieve tarief (normaal/dal, van de P1-meter: die staat maar 30 dagen ruw in `ltss`, dus hij
  wordt hier vastgelegd), de geldende tarieven, besparing (€), opbrengst teruglevering (€), de
  day-ahead-prijs, en een vlag voor minuten met ontbrekende data.
- Kostenformules: zie *Begrippen en formules*. Vaste terugleverkosten per maand worden niet
  per minuut verdeeld, maar per maand afgetrokken.
- Ontbreekt de Growatt (bijv. Grott stond stil), dan is de opwek onbekend. Zulke minuten
  krijgen de vlag en tellen niet mee voor eigen gebruik. Inkoop en teruglevering blijven
  gewoon kloppen.

### Prijzen (Z10)
- De Nord Pool-integratie (kern van HA, geen key). Een automation roept elke middag na
  publicatie `nordpool.get_prices_for_date` aan voor morgen, en na het opstarten of na een
  periode zonder internet ook voor de gemiste dagen. Het resultaat komt als attribuut op een
  template-sensor (één dag per update).
- LTSS schrijft die sensor naar `ltss`. Een trigger op `ltss` zet de prijzen per kwartier in
  de permanente tabel `energy_price` (upsert), zodat ze ook na 30 dagen bewaard blijven. Het
  grote attribuut gaat niet naar de Recorder.
- Dit zijn kale marktprijzen. Een dynamisch contract rekent er een opslag, energiebelasting en
  btw bij. Die komen als helpers bij de vergelijking (V1).

### Tonen (Z9)
- **Energiedashboard** van HA: net (inkoop normaal/dal, teruglevering) en zonnepanelen, met de
  tarief-helpers als prijs. Dat geeft de kWh-overzichten van doel 1 en 2, eigengebruik en
  zelfvoorzienendheid, met de eigen datumkiezer.
- **SQL-sensoren** met lopende totalen uit `solar_1m` (eigen gebruik in kWh, besparing in €,
  opbrengst teruglevering in €, terugleverkosten, teruglevering tegen een negatieve prijs).
  HA maakt er statistieken van. Bedragen zijn `monetary` met `state_class: total`. Ze lopen tot
  ~5 minuten achter.
- **Dashboard "Zonnepanelen"** (storage-modus, door `setup_solar.py`): statistics-kaarten per
  dag, week, maand en jaar, gekoppeld aan de datumkiezer van het energiedashboard voor een
  eigen periode (`energy_date_selection`, bij de bouw te verifiëren). Verder kaarten voor de
  investering (doel 3), de voorspelling (doel 5), het overschot per uur van de dag (doel 4) en de
  batterijsimulatie.
- **Berekeningen** als SQL-functies, getoond via SQL-sensoren: `solar_payback()` (doel 3),
  `solar_npv()` en `solar_irr()` (doel 5; IRR met bisectie), en `solar_simulate_battery(...)`
  (doel 4).

### Voorspelling (doel 5)
- **Korte termijn:** de Forecast.Solar-integratie (kern van HA, internet, C1a). LTSS bewaart
  de voorspelling, zodat je hem later kunt vergelijken met de werkelijke opwek.
- **Lange termijn:** een vaste referentie per maand van PVGIS voor deze installatie, één keer
  opgehaald en als tabel in `solar.sql`. Zodra er een maand gemeten is, telt de meting.

### Staging
Staging heeft geen echte database en geen Grott. SQL-sensoren geven daar fouten bij het
opzetten, en de health-check ziet die niet (zie *Open punten* in DESIGN.md). Wat in staging
wel getest wordt: of de packages laden, plus de webhook-sensoren met een testbericht.

### Fasen
Elke fase wordt apart getest op martha en gecommit. **Alles met de omvormer komt zo laat
mogelijk** (keuze van de gebruiker, 2026-10-12). Dat kan, omdat de P1-meter al veel ziet:
teruglevering is het overschot, en inkoop plus teruglevering zijn genoeg voor doel 2, voor de
batterijsimulatie (doel 4) en voor de vergelijking van contracten (V1). Alleen eigen gebruik en
opwek (doel 1, en het deel van doel 3 en 5 dat daarop rust) hebben de omvormer nodig.

| Fase | Inhoud | Omvormer nodig | Klaar als |
|---|---|---|---|
| 1 | **Prijzen:** Nord Pool, de automation met bijhalen, de trigger naar `energy_price` | nee | De prijzen worden elke dag bewaard, en de prijzen van een gemiste dag worden na een internetstoring bijgehaald. |
| 2 | **Rekenlaag op P1:** helpers voor tarieven, `solar_1m` en de job (opwek nog leeg), lopende totalen, energiedashboard (net), dashboard voor doel 2 (teruglevering, terugleverkosten, teruglevering tegen een negatieve prijs) en V1 | nee | Dag- en maandsommen van inkoop en teruglevering kloppen met de P1-tellers. |
| 3 | **Overschot en simulatie (doel 4):** overschot per uur van de dag, `solar_simulate_battery` | nee | Een simulatie is voor één dag met de hand nagerekend. |
| 4 | **Investering en voorspelling (doel 3 en 5):** helpers, PVGIS-referentie, Forecast.Solar, `solar_payback`, NPV en IRR. Tot fase 5 rekent de voorspelling met een aangenomen eigengebruik %. | nee | Terugverdientijd en IRR zijn met de hand nagerekend. |
| 5 | **Omvormer:** vast IP, Grott, webhook-sensoren, opwek en eigen gebruik in `solar_1m`, doel 1, zonnepanelen in het energiedashboard | ja | Opwek komt binnen in HA en `ltss_1m`, ook als de internetverbinding eruit is. De Growatt-app werkt nog. Dagsommen kloppen met de Growatt-dagteller. |
| 6 | **Sturen (doel 6)**, alleen als fase 2 laat zien dat het loont | ja | Nog te ontwerpen. |

**Gevolg:** tot fase 5 staat de opwek alleen in de Growatt-cloud, niet op
martha. Voor die periode is het eigen gebruik op martha onbekend. Inkoop en teruglevering
worden wel volledig gemeten. *Optioneel in fase 5:* de opwek van die periode bijhalen uit een
export van het Growatt-portaal (per 5 minuten of per dag), en `solar_recompute` over die periode
draaien.

## Begrippen en formules

Per tijdvak (minuut) en daarna opgeteld:

| Grootheid | Bron of formule |
|---|---|
| `opwek` | omvormer (kWh-teller) |
| `inkoop` | P1, levering tarief 1 + 2 |
| `teruglevering` | P1, teruglevering tarief 1 + 2 |
| `eigen_gebruik` | `opwek − teruglevering` (≥ 0) |
| `verbruik` (totaal huis) | `inkoop + eigen_gebruik` |
| `eigengebruik_pct` | `eigen_gebruik / opwek` |
| `zelfvoorzienend_pct` | `eigen_gebruik / verbruik` |
| `besparing_eigen_gebruik` | `eigen_gebruik × inkoopprijs_allin(t)` |
| `opbrengst_teruglevering` | `teruglevering × terugleververgoeding(t) − terugleverkosten` |
| `opbrengst` | `besparing_eigen_gebruik + opbrengst_teruglevering` |

**Correctie op de eerste versie:** "2 van de 3 waarden (opwek, gebruik, netinkoop)" klopt niet,
omdat het net twee richtingen heeft. Je hebt er **3** nodig: opwek, inkoop en teruglevering. Daaruit
volgen eigen gebruik en totaalverbruik. Zonder opwekmeting kun je eigen gebruik niet bepalen.

*Let op:* omvormer en P1 meten niet op exact hetzelfde moment en niet met dezelfde
nauwkeurigheid. Per minuut kan `eigen_gebruik` daardoor iets negatief worden; dat wordt op 0
afgekapt. Over een dag middelt het uit.

`inkoopprijs_allin` = kale prijs + inkoopvergoeding + energiebelasting, inclusief btw. Vaste
kosten (netbeheer, vastrecht) en de vermindering energiebelasting tellen niet mee per kWh: die
betaal je ook zonder panelen.

## Doelen

### Doel 1: besparing door eigen gebruik
Hoeveel betaal ik minder doordat ik zelf opgewekte stroom direct verbruik?

- **Meten:** opwek, inkoop, teruglevering (per minuut, in kWh) en de inkoopprijs per tijdvak.
- **Tonen:** per dag, week, maand, jaar en een vrij te kiezen periode de som per kolom:
  opwek, inkoop, teruglevering, eigen gebruik, verbruik, besparing (€), plus eigengebruik % en
  zelfvoorzienend %.
- **Saldering tot 2027:** in november en december 2026 levert een teruggeleverde kWh nog
  evenveel op als een zelf gebruikte. Toch rekenen we meteen met het regime van 2027 (Z2).

### Doel 2: opbrengst van teruglevering
Wat levert teruglevering netto op, en wat kost het?

- **Meten:** teruglevering per minuut, de terugleververgoeding per tijdvak en de
  terugleverkosten (per kWh of vast per maand, afhankelijk van het contract).
- **Tonen:** dezelfde perioden als doel 1; som van teruglevering (kWh), vergoeding (€),
  terugleverkosten (€) en netto (€). Daarnaast: kWh en € teruggeleverd tegen een
  **negatieve** of nul-prijs (input voor V2).
- **Vergelijken (V1):** dezelfde teruglevering en inkoop doorrekenen met de tarieven van andere
  contracten (vast, variabel, dynamisch). Daarvoor moeten de ruwe kWh per tijdvak bewaard
  blijven, los van de prijs van het huidige contract. Voor een dynamisch contract worden de
  day-ahead-prijzen per kwartier gebruikt (Z5).

### Doel 3: de investering bijhouden
Was het tot nu toe een verstandig besluit?

- **Invoer:** investeringsdatum en -bedrag (panelen, omvormer, installatie, eventueel
  aanpassingen aan de meterkast), en later bijkomende kosten (onderhoud, reparatie, vervanging
  omvormer, verzekering).
- **Berekend:**
  - totaal geïnvesteerd = investering + bijkomende kosten tot nu toe;
  - totaal opgeleverd = som van `opbrengst` (doel 1 + doel 2) sinds de start;
  - terugverdienratio = opgeleverd / geïnvesteerd;
  - terugverdientijd = moment van break-even − startdatum (*correctie:* in de eerste versie
    stond het andersom). Zolang break-even nog niet bereikt is, komt deze uit doel 5.

### Doel 4: ruimte op de panelen (overschot) benutten
Hoeveel overschot is er, en wanneer? Zodat ik kan bepalen of verbruik goedkoper wordt als ik het
naar momenten met opwek verschuif, en of een extra apparaat zou lonen.

Een vaste "kWh-prijs van zonnestroom" bestaat niet: het overschot is er alleen op bepaalde
momenten en in bepaalde hoeveelheden. Daarom vergelijk je het **gemeten overschot** met het
**gemeten verbruik**, per minuut.

- **Meten:** het overschot is de teruglevering per minuut. Het verbruiksprofiel is het complete
  huisverbruik per minuut (`inkoop + eigen_gebruik`), uit P1 en opwek (Z4). Er komen geen
  metingen per apparaat.
- **Tonen:** het overschot per uur van de dag (gemiddeld per maand), zodat je ziet op welke
  uren er ruimte is, en het eigengebruik % (doel 1) als maat voor de ruimte die in totaal nog
  over is.
- **Simulatie over historie** (V8, V9, V10): een extra verbruiker of batterij doorrekenen op de
  echte minuutdata.
  - Batterij: het overschot opslaan en later inzetten in plaats van inkopen, met capaciteit,
    laad-/ontlaadvermogen en rendement als parameters.
  - Warmtepomp en EV: een extra verbruik bovenop het gemeten huisprofiel, waarvan zoveel
    mogelijk wordt gedekt uit het overschot. Hoe dat extra verbruik eruitziet (vast blok,
    verdeeld over de dag, afhankelijk van de temperatuur), is een aanname per simulatie.
  - Per minuut: `min(extra, overschot) × terugleververgoeding + max(0, extra − overschot) ×
    inkoopprijs` = de kosten van het extra verbruik.

### Doel 5: voorspellen hoe de investering verloopt
De investering vergelijkbaar maken met andere (sparen, aflossen, beleggen).

- **Opwek voorspellen:** op korte termijn (morgen) uit een weersvoorspelling; op lange termijn
  uit de gemeten opwek per maand, plus een referentie (bijv. PVGIS) zolang er nog geen jaar
  aan data is.
- **Aannames, expliciet instelbaar:** degradatie van de panelen (%/jaar), levensduur panelen
  en omvormer, prijsscenario's voor inkoop en teruglevering, één discontovoet (Z3), en het
  eigengebruik % (gemeten, of aangepast voor een toekomstige warmtepomp/EV/batterij).
- **Berekend:** verwachte kasstroom per jaar, NPV, IRR en verwachte terugverdientijd, per
  scenario. Zodra er meer meetdata is, vervangen gemeten jaren de voorspelde.

### Doel 6: sturen (nieuw, uit de titel "aansturen en monitoren")
De omvormer beperken of uitzetten wanneer terugleveren geld kost (V2).

- Voorwaarde: de Growatt is lokaal te sturen (vermogen beperken of uitzetten).
- Regel, bijvoorbeeld: terugleververgoeding − terugleverkosten < 0 in het huidige tijdvak →
  teruglevering beperken tot 0 W (het eigen verbruik blijft gedekt). Met een vast contract
  gebeurt dat alleen als de terugleverkosten hoger zijn dan de vergoeding. De day-ahead-prijzen
  (Z5) laten zien wat sturen met een dynamisch contract zou opleveren.
- Eerst alleen meten wat het had opgeleverd (doel 2, teruglevering tegen negatieve prijs);
  sturen pas als dat de moeite waard blijkt.

## Open vragen

1. **Tarieven van het vaste contract:** de prijs per kWh (normaal/dal), de
   terugleververgoeding, de terugleverkosten (per kWh of vast) en de einddatum van het contract.
2. **Installatie:** het investeringsbedrag, het aantal panelen, het vermogen in Wp, de
   oriëntatie en de hellingshoek. Vult de gebruiker later in; het wordt instelbare invoer.

Na te gaan bij de bouw:

3. **Grott zonder internet:** blijft Grott meetwaarden doorgeven als het doorsturen naar
   Growatt mislukt? Test dit in fase 5.
4. **Interval van de ShineLAN-X:** kan hij naar 1 minuut, en hoe?
5. **Afhankelijkheden van Grott:** kan hij draaien met alleen de stdlib (zonder MQTT), of zijn
   er `.deb`'s nodig uit de offline mirror?
6. **`energy_date_selection`** op de statistics-kaarten van de huidige HA-versie.
7. **Nord Pool:** hoe ver terug kan `get_prices_for_date` prijzen ophalen?
