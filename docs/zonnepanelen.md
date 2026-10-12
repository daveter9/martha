# Zonnepanelen: eisen

Status: **concept**, in bewerking met de gebruiker (gestart 2026-10-12). De panelen worden
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
  evenveel op als een zelf gebruikte. *Open:* die twee maanden apart rekenen, of vanaf de start
  het regime van 2027 hanteren?

### Doel 2: opbrengst van teruglevering
Wat levert teruglevering netto op, en wat kost het?

- **Meten:** teruglevering per minuut, de terugleververgoeding per tijdvak en de
  terugleverkosten (per kWh of vast per maand, afhankelijk van het contract).
- **Tonen:** dezelfde perioden als doel 1; som van teruglevering (kWh), vergoeding (€),
  terugleverkosten (€) en netto (€). Daarnaast: kWh en € teruggeleverd tegen een
  **negatieve** of nul-prijs (input voor V2).
- **Vergelijken (V1):** dezelfde teruglevering en inkoop doorrekenen met de tarieven van andere
  contracten (vast, variabel, dynamisch). Daarvoor moeten de ruwe kWh per tijdvak bewaard
  blijven, los van de prijs van het huidige contract.

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
Hoeveel overschot is er, en wanneer? Zodat ik per apparaat kan bepalen of het goedkoper is om
het op opwek te laten draaien.

Een vaste "kWh-prijs van zonnestroom" bestaat niet: het overschot is er alleen op bepaalde
momenten en in bepaalde hoeveelheden. Daarom vergelijk je het **gemeten overschot** met het
**verbruiksprofiel** van een apparaat.

- **Meten:** teruglevering per minuut is het overschot (wat er beschikbaar was).
- **Verbruiksprofielen:** vermogen per minuut van een run van een apparaat (wasmachine,
  vaatwasser, droger) of van een toekomstig apparaat (warmtepomp, EV-laden, batterij).
  *Open:* zelf meten (slimme stekker) of standaardprofielen?
- **Berekend:** voor een profiel op starttijd *t* de kosten = per minuut
  `min(profiel, overschot) × terugleververgoeding + max(0, profiel − overschot) × inkoopprijs`.
  Daarmee: de goedkoopste starttijd, en de besparing ten opzichte van starten op een vast
  moment.
- **Simulatie over historie** (V8, V9, V10): een warmtepomp, EV of batterij doorrekenen op de
  echte minuutdata van een heel jaar. Voor de batterij: overschot opslaan en later inzetten in
  plaats van inkopen, met capaciteit, laad-/ontlaadvermogen en rendement als parameters.
- Eigengebruik % (doel 1) laat zien hoeveel ruimte er in totaal nog is.

### Doel 5: voorspellen hoe de investering verloopt
De investering vergelijkbaar maken met andere (sparen, aflossen, beleggen).

- **Opwek voorspellen:** op korte termijn (morgen) uit een weersvoorspelling; op lange termijn
  uit de gemeten opwek per maand, plus een referentie (bijv. PVGIS) zolang er nog geen jaar
  aan data is.
- **Aannames, expliciet instelbaar:** degradatie van de panelen (%/jaar), levensduur panelen
  en omvormer, prijsscenario's voor inkoop en teruglevering, discontovoet, en het
  eigengebruik % (gemeten, of aangepast voor een toekomstige warmtepomp/EV/batterij).
- **Berekend:** verwachte kasstroom per jaar, NPV, IRR en verwachte terugverdientijd, per
  scenario. Zodra er meer meetdata is, vervangen gemeten jaren de voorspelde.

### Doel 6: sturen (nieuw, uit de titel "aansturen en monitoren")
De omvormer beperken of uitzetten wanneer terugleveren geld kost (V2).

- Voorwaarde: de omvormer is lokaal te sturen (vermogen beperken of uitzetten).
- Regel, bijvoorbeeld: terugleververgoeding − terugleverkosten < 0 in het huidige tijdvak →
  teruglevering beperken tot 0 W (het eigen verbruik blijft gedekt).
- Eerst alleen meten wat het had opgeleverd (doel 2, teruglevering tegen negatieve prijs);
  sturen pas als dat de moeite waard blijkt.

## Open vragen

1. **Omvormer:** welk merk en type? Heeft hij een lokale API (Modbus TCP, lokale HTTP)? Dat
   bepaalt of opwek per minuut zonder internet beschikbaar is (C1a) en of doel 6 kan.
2. **Energiecontract:** vast, variabel of dynamisch? Welke terugleververgoeding en
   terugleverkosten? Loopt het contract na 2027 door?
3. **Prijzen:** bij een dynamisch contract komen de prijzen per kwartier van internet. Is het
   acceptabel dat ze ontbreken zolang er geen internet is, mits ze daarna worden bijgehaald en
   voor altijd in de database worden bewaard?
4. **Waar komen de overzichten?** Het energiedashboard van HA dekt een deel van doel 1 en 2
   (inclusief eigen periode). Formules als NPV, IRR en simulaties niet. Opties: eigen
   HA-dashboards op SQL-sensoren, een aparte analyse vanaf de pc via de SSH-tunnel
   (notebook/spreadsheet), of een extra container (in strijd met C6, dus een nieuwe beslissing).
5. **Saldering nov–dec 2026:** apart rekenen of meteen het regime van 2027?
6. **Investering:** bedrag, aantal panelen, Wp, oriëntatie en hellingshoek (voor de
   opwekvoorspelling).
7. **Discontovoet** voor NPV: de spaarrente, de hypotheekrente, of iets anders?
8. **Verbruiksprofielen:** zelf meten met slimme stekkers of standaardprofielen?
