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
- **Omvormer:** Growatt (type nog onbekend). Growatt werkt standaard via de cloud; lokaal
  uitlezen en sturen kan via Modbus (RS485, of de wifi-stick met andere firmware). Hoe dat
  hier gaat, is nog open.
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

1. **Growatt:** welk type? Wordt hij lokaal uitgelezen via Modbus RS485 (kabel naar martha) of
   via de wifi-stick met andere firmware? Dat bepaalt of de opwek zonder internet binnenkomt
   (C1a) en of doel 6 kan.
2. **Tarieven van het vaste contract:** de prijs per kWh (normaal/dal), de
   terugleververgoeding, de terugleverkosten (per kWh of vast) en de einddatum van het contract.
3. **Installatie:** het investeringsbedrag, het aantal panelen, het vermogen in Wp, de
   oriëntatie en de hellingshoek. Vult de gebruiker later in; het wordt instelbare invoer.
4. **Bron van de day-ahead-prijzen** (Z5): welke integratie werkt in HA zonder HACS (C21)?
