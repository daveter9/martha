-- Heating: what the gas boiler costs, and what an all-electric heat pump would cost instead
-- (docs/warmtepomp.md).
--
-- Applied by install.sh on every run, after solar.sql (it uses solar_counter, solar_tariff,
-- solar_setting_ranges, solar_1m and energy_price), so every statement must be idempotent.
-- heat_1m and heat_1d hold derived data only: heat_job can always rebuild them.
--
-- Data flow:
--   ltss (trigger heat_capture)  -> heat_setting (helper values input_number.heat_*, changes only)
--                                -> heat_gas_market (EnergyZero gas prices, from the REST sensor)
--   ltss_1m (gas meter, outdoor temperature) + heat_weather (Open-Meteo, filled by
--   setup_heat.py) + heat_reference (hourly temperatures of 2019, heat_reference.sql)
--   + solar_1m (export, feed-in price) + solar_setting (contract electricity tariffs)
--   + energy_price (day-ahead electricity, Nord Pool) + heat_gas_market
--     -> heat_1m (per minute) -> heat_1d
--   heat_1d + heat_reference -> heat_result (heat loss fit, forecast per year; hourly)
--
-- Two installations, computed for every minute from the gas actually used:
--   cv      the current gas boiler (what is really paid for gas);
--   wp      an all-electric air/water heat pump for heating and hot water, gas connection
--           removed (cooking is on induction already, so it does not change).
-- Each with two prices: the contract (fixed) and the market (dynamic contract: day-ahead
-- price plus markup and energy tax, plus VAT).
--
-- Hot water is measured, not assumed: a combi boiler heats tap water at (nearly) full power
-- for a short time, and the house at a low power for a longer time (heat_gas).
--
-- Units: energy in kWh, gas in m³, money in EUR, temperatures in °C. Days are local days.

-- --- Configuration ----------------------------------------------------------------------
-- Entity id per role, written by host/ha/setup_heat.py:
--   gas           P1 gas meter (m³, total)
--   outdoor_temp  outdoor temperature (template sensor on the weather entity, packages/heat.yaml)
--   gas_market    REST sensor with the EnergyZero gas prices (packages/heat.yaml)
CREATE TABLE IF NOT EXISTS heat_entity (
    role      text PRIMARY KEY,
    entity_id text NOT NULL
);

CREATE OR REPLACE FUNCTION heat_entity_id(p_role text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT entity_id FROM heat_entity WHERE role = p_role
$$;

-- Every parameter of the model. setup_heat.py creates a helper input_number.heat_<key> for
-- each row (and removes helpers whose row is gone); the dashboard and heat_assumptions()
-- show the texts. kind:
--   model   a property of the house or the equipment. Its current value holds for all
--           history: a change is a correction, and everything is recomputed.
--   price   holds from the moment it is set. The first change replaces the default for all
--           history (so the real contract price can be filled in later).
--   check   not used in the calculation; shown next to the forecast to check the model.
CREATE TABLE IF NOT EXISTS heat_param (
    key         text PRIMARY KEY,
    kind        text NOT NULL,
    label       text NOT NULL,
    unit        text NOT NULL,
    min         double precision NOT NULL,
    max         double precision NOT NULL,
    step        double precision NOT NULL,
    default_val double precision NOT NULL,
    sort        double precision NOT NULL,
    explain     text NOT NULL,   -- what it is and where the default comes from
    refine      text NOT NULL    -- how to make it more accurate
);

-- Parameters of an earlier version (cooking, hybrid heat pump, investment, hot water per day).
DELETE FROM heat_param WHERE key IN ('cooking_m3_day', 'cooking_kwh_per_m3', 'dhw_m3_day',
    'hybrid_capacity', 'maint_hybrid', 'inv_hp', 'sub_hp', 'inv_hybrid', 'sub_hybrid',
    'boiler_replacement');

INSERT INTO heat_param (key, kind, label, unit, min, max, step, default_val, sort, explain, refine) VALUES
('gas_price', 'price', 'Gasprijs contract (all-in)', 'EUR/m³', 0, 10, 0.0001, 1.48, 10,
 'Prijs per m³ van je contract, inclusief energiebelasting en btw. Standaard: landelijk gemiddelde 2026 (Energievergelijk.nl, augustus 2026).',
 'De variabele gasprijs uit je contract.'),
('electricity_price', 'price', 'Stroomprijs contract als die bij Zonnepanelen ontbreekt', 'EUR/kWh', 0, 2, 0.0001, 0.28, 11,
 'Alleen gebruikt zolang Inkoopprijs normaal/dal op het dashboard Zonnepanelen op 0 staan (of voordat ze werden ingevuld). Standaard: landelijk gemiddelde 2026.',
 'Je contractprijs; vul hem ook in als Inkoopprijs normaal en dal bij Zonnepanelen.'),
('gas_fixed_month', 'price', 'Vaste kosten gasaansluiting per maand', 'EUR', 0, 200, 0.01, 25, 12,
 'Vaste leveringskosten van de leverancier plus netbeheerkosten gas, incl. btw. Vervallen met een warmtepomp (gasaansluiting eraf). Standaard: schatting.',
 'Staat op je jaarafrekening: vaste leveringskosten gas + netbeheerkosten gas.'),
('maint_boiler', 'price', 'Onderhoud CV-ketel per jaar', 'EUR', 0, 2000, 1, 120, 13,
 'Onderhoudsbeurt of servicecontract van de ketel.',
 'Bedrag van je servicecontract.'),
('maint_hp', 'price', 'Onderhoud warmtepomp per jaar', 'EUR', 0, 2000, 1, 150, 14,
 'Onderhoud van een warmtepomp. Standaard: schatting.',
 'Uit een offerte of servicecontract.'),
('market_gas_markup', 'price', 'Marktprijs: opslag gas (excl. btw)', 'EUR/m³', 0, 2, 0.0001, 0.06, 15,
 'Inkoopvergoeding van een dynamisch gascontract bovenop de marktprijs (EnergyZero). Standaard: gangbaar 2026.',
 'Opslag van een concreet dynamisch contract.'),
('gas_energy_tax', 'price', 'Marktprijs: energiebelasting gas (excl. btw)', 'EUR/m³', 0, 5, 0.00001, 0.60066, 16,
 'Energiebelasting op aardgas 2026, tot 1.000 m³ (Belastingdienst).',
 'Verandert per 1 januari.'),
('market_elec_markup', 'price', 'Marktprijs: opslag stroom (excl. btw)', 'EUR/kWh', 0, 1, 0.0001, 0.02, 17,
 'Inkoopvergoeding van een dynamisch stroomcontract bovenop de day-ahead-prijs (Nord Pool). Standaard: gangbaar 2026.',
 'Opslag van een concreet dynamisch contract.'),
('elec_energy_tax', 'price', 'Marktprijs: energiebelasting stroom (excl. btw)', 'EUR/kWh', 0, 1, 0.00001, 0.09161, 18,
 'Energiebelasting op elektriciteit 2026, tot 10.000 kWh (Belastingdienst).',
 'Verandert per 1 januari.'),
('vat', 'price', 'Marktprijs: btw', '%', 0, 50, 0.1, 21, 19,
 'Btw over marktprijs, opslag en energiebelasting.',
 'Verandert alleen bij een wetswijziging.'),

('gas_kwh_per_m3', 'model', 'Verbrandingswaarde aardgas', 'kWh/m³', 5, 15, 0.001, 9.769, 20,
 'Energie in 1 m³ gas: 35,17 MJ/m³ (bovenwaarde, Groningen-kwaliteit, waarmee in Nederland wordt afgerekend) = 9,769 kWh.',
 'Vrijwel altijd goed; de calorische waarde staat op de jaarafrekening.'),
('boiler_eff_heating', 'model', 'Rendement CV-ketel bij verwarmen', '%', 50, 110, 0.5, 90, 21,
 'Deel van de gasenergie dat als warmte in huis komt (op bovenwaarde). Een HR-ketel haalt 85-95 %, afhankelijk van de retourtemperatuur; bij 55 °C aanvoer condenseert hij maar deels.',
 'Met een OpenTherm-koppeling is de echte aanvoer- en retourtemperatuur te meten.'),
('boiler_eff_dhw', 'model', 'Rendement CV-ketel bij warm water', '%', 30, 110, 0.5, 75, 22,
 'Een combiketel condenseert bij warm water nauwelijks en verliest warmte bij korte tapbeurten en het warmhouden.',
 'Ketelgegevens (CW-klasse, rendement tapwater).'),
('dhw_min_kw', 'model', 'Warm water herkennen: vermogen vanaf', 'kW', 2, 40, 0.5, 8, 23,
 'Een meldinterval van 5 minuten met minstens dit gemiddelde vermogen is warm water (douchen, tappen): de combiketel brandt dan (bijna) vol. Verwarmen gaat op een lager vermogen.',
 'Kijk op Berekening naar de herkende tapbeurten; ligt verwarmen bij vorst ook zo hoog, verhoog dan deze grens.'),
('dhw_max_minutes', 'model', 'Warm water herkennen: hooguit zo lang', 'min', 5, 120, 5, 20, 24,
 'Een reeks intervallen boven de grens die langer duurt, is verwarmen (opwarmen na de nacht). Ook een losse melding zonder verbruik 10 minuten ervoor en erna telt als warm water (tappen, warmhouden van de ketel).',
 'De langste douchebeurt in je huishouden, afgerond op 5 minuten.'),
('heating_limit', 'model', 'Stookgrens', '°C', 5, 22, 0.5, 16, 26,
 'Buitentemperatuur waarboven het huis niet verwarmd hoeft te worden (interne warmte en zon zijn genoeg). Basis van de graaduren en de stooklijn.',
 'De temperatuur waarboven het gas voor verwarming (Vergelijking) naar nul gaat.'),
('supply_design', 'model', 'Warmtepomp: aanvoertemperatuur bij -10 °C', '°C', 25, 75, 0.5, 50, 30,
 'Hoe warm het water naar de radiatoren moet op de koudste dag. Hoe lager, hoe hoger de COP. Radiatoren: meestal 45-55 °C; vloerverwarming 35 °C.',
 'Zet de ketel een paar koude dagen op een lagere aanvoer en kijk of het huis warm blijft; of een warmteverliesberekening per ruimte.'),
('supply_min', 'model', 'Warmtepomp: aanvoertemperatuur bij de stookgrens', '°C', 20, 60, 0.5, 30, 31,
 'Onderkant van de stooklijn. Tussen stookgrens en -10 °C loopt de aanvoertemperatuur lineair op.',
 'Uit de instellingen van een warmtepomp of de installateur.'),
('carnot_eff', 'model', 'Warmtepomp: Carnot-rendement', '%', 20, 70, 0.5, 45, 32,
 'COP = Carnot-rendement x (aanvoer + 273,15) / (aanvoer - buiten). 45 % geeft COP 5,0 bij A7/W35 en 2,4 bij A-7/W50, gangbaar voor lucht/water.',
 'Vul de COP uit een datablad in (volgende instelling); dan wordt dit rendement daaruit afgeleid.'),
('cop_a7w35', 'model', 'Warmtepomp: COP bij A7/W35 volgens fabrikant (0 = niet gebruikt)', '', 0, 10, 0.01, 0, 33,
 'Als dit groter dan 0 is, volgt het Carnot-rendement hieruit: COP / (308,15 / 28).',
 'Staat in het datablad van de warmtepomp (EN 14511).'),
('defrost_loss', 'model', 'Warmtepomp: ontdooiverlies onder 6 °C', '%', 0, 40, 0.5, 10, 34,
 'Bij buitentemperaturen rond het vriespunt bevriest de verdamper en moet hij ontdooien.',
 'Uit metingen aan een echte warmtepomp; meestal 5-15 %.'),
('cop_max', 'model', 'Warmtepomp: hoogste COP', '', 1, 10, 0.1, 7, 35,
 'Bij een kleine temperatuursprong (zacht weer) geeft de Carnot-formule onrealistisch hoge waarden; in de praktijk zakt het rendement dan door pendelen en deellast.',
 'Uit het datablad (COP bij A12/W35 of A15/W35).'),
('dhw_temp', 'model', 'Warmtepomp: temperatuur warm water', '°C', 40, 70, 0.5, 55, 36,
 'Een warmtepomp verwarmt een boilervat tot deze temperatuur; de COP voor warm water rekent daarmee.',
 'Instelling van de boiler; lager is zuiniger (legionella-programma niet meegerekend).'),
('hp_capacity', 'model', 'Warmtepomp: vermogen (0 = automatisch)', 'kW', 0, 30, 0.1, 0, 37,
 'Warmte per uur voor verwarming die de warmtepomp kan leveren. Is de vraag in een uur groter, dan levert een elektrisch element (COP 1) de rest. Warm water komt uit een boilervat en telt niet mee. 0 = het berekende benodigde vermogen bij -10 °C, minimaal 4 kW (het kleinste gangbare formaat).',
 'Vermogen uit een offerte, bij -7 °C.'),

('gas_last_year', 'check', 'Gasverbruik laatste volle jaar (jaarafrekening, 0 = onbekend)', 'm³', 0, 20000, 1, 0, 50,
 'Zolang het gemeten warmteverlies onzeker is (minder dan 30 bruikbare dagen of R² onder 0,5), wordt het warmteverlies hierop geijkt: (dit verbruik - gemeten warm water x 365) als warmte, gedeeld door de graaduren van het referentiejaar. Daarna is het alleen een controle. Een zacht of koud jaar scheelt tot 10 %.',
 'Het exacte getal van de jaarafrekening of uit de app van de netbeheerder.')
ON CONFLICT (key) DO UPDATE SET
    kind = EXCLUDED.kind, label = EXCLUDED.label, unit = EXCLUDED.unit, min = EXCLUDED.min,
    max = EXCLUDED.max, step = EXCLUDED.step, default_val = EXCLUDED.default_val,
    sort = EXCLUDED.sort, explain = EXCLUDED.explain, refine = EXCLUDED.refine;

-- History of the helpers input_number.heat_* (changes only).
CREATE TABLE IF NOT EXISTS heat_setting (
    entity_id text             NOT NULL,
    time      timestamptz      NOT NULL,
    value     double precision NOT NULL,
    PRIMARY KEY (entity_id, time)
);
DELETE FROM heat_setting WHERE entity_id LIKE 'input\_number.heat\_%'
    AND substr(entity_id, length('input_number.heat_') + 1) NOT IN (SELECT key FROM heat_param);

-- Hourly outdoor temperatures from Open-Meteo for the past (before the sensor existed, or
-- while it was unavailable), written by setup_heat.py when there is internet.
CREATE TABLE IF NOT EXISTS heat_weather (
    time    timestamptz      PRIMARY KEY,
    temp    double precision NOT NULL,
    fetched timestamptz      NOT NULL DEFAULT now()
);

-- Gas market price per hour (EnergyZero: the day-ahead gas price, EUR/m³, without taxes or
-- markup), kept forever. Filled by heat_capture from the REST sensor, and by setup_heat.py.
CREATE TABLE IF NOT EXISTS heat_gas_market (
    time    timestamptz      PRIMARY KEY,   -- start of the hour
    price   double precision NOT NULL,
    fetched timestamptz      NOT NULL DEFAULT now()
);

-- Hourly temperatures of the reference year; the data is in heat_reference.sql.
CREATE TABLE IF NOT EXISTS heat_reference (
    hour integer PRIMARY KEY,           -- 0 .. 8759, hours since 1 January 00:00 UTC
    temp double precision NOT NULL      -- °C
);

-- Results too heavy for a SQL sensor that polls every 30 s (heat_results, hourly), and
-- bookkeeping of the jobs.
CREATE TABLE IF NOT EXISTS heat_result (
    key     text PRIMARY KEY,
    value   double precision,
    detail  text,
    updated timestamptz NOT NULL DEFAULT now()
);
DELETE FROM heat_result WHERE key IN ('year_cost_hyb', 'year_hyb_kwh', 'year_hyb_gas',
    'year_payback_wp', 'year_payback_hyb', 'dhw_measured');

-- --- Capture from LTSS ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION heat_capture() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v double precision;
BEGIN
    IF NEW.entity_id LIKE 'input\_number.heat\_%' THEN
        BEGIN
            v := ltss_numeric(NEW.state);
            IF v IS NOT NULL AND v IS DISTINCT FROM (
                    SELECT value FROM heat_setting
                    WHERE entity_id = NEW.entity_id AND time <= NEW.time
                    ORDER BY time DESC LIMIT 1) THEN
                INSERT INTO heat_setting (entity_id, time, value)
                VALUES (NEW.entity_id, NEW.time, v)
                ON CONFLICT DO NOTHING;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'heat_capture (%): %', NEW.entity_id, SQLERRM;
        END;
    ELSIF NEW.attributes ? 'Prices' AND NEW.entity_id = heat_entity_id('gas_market') THEN
        BEGIN
            INSERT INTO heat_gas_market (time, price)
            SELECT (p ->> 'readingDate')::timestamptz, (p ->> 'price')::double precision
            FROM jsonb_array_elements(NEW.attributes -> 'Prices') AS p
            WHERE ltss_numeric(p ->> 'price') IS NOT NULL
            ON CONFLICT (time) DO UPDATE SET price = EXCLUDED.price, fetched = now();
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'heat_capture (%): %', NEW.entity_id, SQLERRM;
        END;
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS heat_capture ON ltss;
CREATE TRIGGER heat_capture AFTER INSERT ON ltss
    FOR EACH ROW EXECUTE FUNCTION heat_capture();

-- --- Settings ---------------------------------------------------------------------------
-- Current value of a parameter: the helper, or the default when it was never stored.
CREATE OR REPLACE FUNCTION heat_param_now(p_key text) RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT coalesce(
        (SELECT value FROM heat_setting WHERE entity_id = 'input_number.heat_' || p_key
         ORDER BY time DESC LIMIT 1),
        (SELECT default_val FROM heat_param WHERE key = p_key))
$$;

-- True when the helper was changed by hand after it was created with its default.
CREATE OR REPLACE FUNCTION heat_param_set(p_key text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT count(*) > 1 FROM heat_setting WHERE entity_id = 'input_number.heat_' || p_key
$$;

-- Validity ranges of the prices. The first stored value is the default written when the
-- helper was created; the first change replaces it for all history, later changes hold
-- from the moment they are made. Without any stored value: the default, always.
CREATE OR REPLACE FUNCTION heat_price_ranges()
RETURNS TABLE (key text, valid_from timestamptz, valid_to timestamptz, value double precision)
LANGUAGE sql STABLE AS $$
    WITH s AS (
        SELECT substr(entity_id, length('input_number.heat_') + 1) AS key, time, value,
               row_number() OVER (PARTITION BY entity_id ORDER BY time) AS rn,
               count(*) OVER (PARTITION BY entity_id) AS cnt
        FROM heat_setting
    ),
    kept AS (
        SELECT key, time, value, row_number() OVER (PARTITION BY key ORDER BY time) AS rn
        FROM s WHERE NOT (rn = 1 AND cnt > 1)
    ),
    ranged AS (
        SELECT key, CASE WHEN rn = 1 THEN '-infinity'::timestamptz ELSE time END AS valid_from, value
        FROM kept
    )
    SELECT r.key, r.valid_from,
           lead(r.valid_from, 1, 'infinity'::timestamptz) OVER (PARTITION BY r.key ORDER BY r.valid_from),
           r.value
    FROM ranged r JOIN heat_param p ON p.key = r.key AND p.kind = 'price'
    UNION ALL
    SELECT p.key, '-infinity'::timestamptz, 'infinity'::timestamptz, p.default_val
    FROM heat_param p
    WHERE p.kind = 'price' AND NOT EXISTS (SELECT 1 FROM kept k WHERE k.key = p.key)
$$;

CREATE OR REPLACE FUNCTION heat_price_now(p_key text) RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT value FROM heat_price_ranges() WHERE key = p_key AND now() >= valid_from AND now() < valid_to
$$;

-- --- Physics ----------------------------------------------------------------------------
-- Supply temperature of the heat pump's heating curve: supply_min at the heating limit,
-- supply_design at -10 °C, linear in between and flat outside.
CREATE OR REPLACE FUNCTION heat_supply(p_out double precision, p_limit double precision,
                                       p_min double precision, p_design double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
    SELECT least(greatest(p_min + (p_design - p_min) * (p_limit - p_out) / nullif(p_limit + 10, 0),
                          p_min), greatest(p_design, p_min))
$$;

-- COP of an air/water heat pump: a fraction (p_eta) of the Carnot COP between the outdoor
-- air and the supply water, minus the defrost loss below 6 °C; between 1 and p_max. The
-- temperature lift is at least 5 K, so a warm day does not give an infinite COP.
DROP FUNCTION IF EXISTS heat_cop(double precision, double precision, double precision, double precision);
CREATE OR REPLACE FUNCTION heat_cop(p_eta double precision, p_supply double precision,
                                    p_out double precision, p_defrost double precision,
                                    p_max double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
    SELECT greatest(1, least(p_max,
        p_eta * (p_supply + 273.15) / greatest(p_supply - p_out, 5)
        * CASE WHEN p_out < 6 THEN 1 - p_defrost / 100 ELSE 1 END))
$$;

-- Fraction of the Carnot COP: from the manufacturer's COP at A7/W35 when given.
CREATE OR REPLACE FUNCTION heat_eta() RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN heat_param_now('cop_a7w35') > 0
                THEN heat_param_now('cop_a7w35') / (308.15 / 28)
                ELSE heat_param_now('carnot_eff') / 100 END
$$;

-- The usual Dutch low tariff when the P1 meter's tariff is not known (anymore): weekends,
-- and on weekdays from 23:00 to 07:00.
CREATE OR REPLACE FUNCTION heat_tariff_rule(p_t timestamptz) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN extract(isodow FROM p_t AT TIME ZONE 'Europe/Amsterdam') IN (6, 7)
                  OR extract(hour FROM p_t AT TIME ZONE 'Europe/Amsterdam') < 7
                  OR extract(hour FROM p_t AT TIME ZONE 'Europe/Amsterdam') >= 23
                THEN 'low' ELSE 'normal' END
$$;

-- Hour of the reference year (heat_reference) for a moment: same day of the year and hour (UTC).
CREATE OR REPLACE FUNCTION heat_ref_hour(p_t timestamptz) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
    SELECT (((extract(doy FROM p_t AT TIME ZONE 'UTC')::integer - 1) * 24
             + extract(hour FROM p_t AT TIME ZONE 'UTC')::integer) % 8760)
$$;

-- Capacity of the heat pump when hp_capacity is 0: what is needed at -10 °C (heat_results),
-- at least 4 kW, the smallest common size. 4 kW as long as there is no fit.
CREATE OR REPLACE FUNCTION heat_auto_capacity() RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT greatest(4, coalesce((SELECT value FROM heat_result WHERE key = 'required_kw'), 0))
$$;

-- --- Series per minute ------------------------------------------------------------------
-- Gas per minute (m³) in [p_from, p_to), split into hot water and heating.
--
-- The meter reports every 5 minutes what was used in the 5 minutes before; HA receives it
-- within seconds, so a step in minute r is spread evenly over the minutes r-5 .. r-1.
--
-- Each report is classified (class):
--   tapwater    average power over the 5 minutes >= p_min_kw, in a run of such reports of at
--               most p_max_minutes (a shower, a bath, the kitchen tap at length);
--   los         a report with no other use 10 minutes before and after (a short tap, or the
--               boiler keeping its heat exchanger warm);
--   verwarming  everything else (including a long run at high power: warming up the house).
-- tapwater and los count as hot water. The newest report counts as heating until 10 minutes
-- later, when it is known whether another one followed.
DROP FUNCTION IF EXISTS heat_gas(timestamptz, timestamptz);
CREATE OR REPLACE FUNCTION heat_gas(p_from timestamptz, p_to timestamptz,
                                    p_min_kw double precision, p_max_minutes double precision,
                                    p_kwh_m3 double precision)
RETURNS TABLE (minute timestamptz, gas double precision, gas_dhw double precision, class text)
LANGUAGE sql STABLE AS $$
    WITH c AS (
        SELECT minute, delta
        FROM solar_counter(heat_entity_id('gas'), p_from - INTERVAL '30 minutes', p_to + INTERVAL '30 minutes')
    ),
    ev AS (
        SELECT minute AS r, delta,
               lag(minute) OVER w AS prev_r, lead(minute) OVER w AS next_r,
               delta * p_kwh_m3 * 12 >= p_min_kw AS burst,
               lag(delta) OVER w * p_kwh_m3 * 12 >= p_min_kw AS prev_burst
        FROM c WHERE delta > 0
        WINDOW w AS (ORDER BY minute)
    ),
    runs AS (
        SELECT ev.*,
               sum(CASE WHEN burst AND prev_burst AND r - prev_r <= INTERVAL '7 minutes' THEN 0 ELSE 1 END)
                   OVER (ORDER BY r) AS grp
        FROM ev
    ),
    cls AS (
        SELECT r, delta,
               CASE WHEN burst AND 5 * count(*) OVER (PARTITION BY grp) <= p_max_minutes THEN 'tapwater'
                    WHEN (prev_r IS NULL OR r - prev_r > INTERVAL '10 minutes')
                         AND ((next_r IS NULL AND r < now() - INTERVAL '10 minutes')
                              OR next_r - r > INTERVAL '10 minutes') THEN 'los'
                    ELSE 'verwarming' END AS class
        FROM runs
    ),
    s AS (
        SELECT c.minute, c.delta,
               CASE WHEN cls.class IN ('tapwater', 'los') THEN c.delta ELSE 0 END AS delta_dhw,
               cls.class
        FROM c LEFT JOIN cls ON cls.r = c.minute
    ),
    spread AS (
        SELECT minute,
               sum(delta) OVER f / 5 AS gas,
               sum(delta_dhw) OVER f / 5 AS gas_dhw,
               -- the report that covers this minute: the first one 1 .. 5 minutes later
               (array_remove(array_agg(class) OVER f, NULL))[1] AS class
        FROM s
        WINDOW f AS (ORDER BY minute ROWS BETWEEN 1 FOLLOWING AND 5 FOLLOWING)
    )
    SELECT minute, gas, gas_dhw, CASE WHEN gas > 0 THEN class END
    FROM spread WHERE minute >= p_from AND minute < p_to
$$;

-- Outdoor temperature per minute in [p_from, p_to), with its source:
--   sensor          the outdoor temperature sensor (weather entity), interpolated linearly
--                   between states at most 3 hours apart, or held for 3 hours after the last;
--   open-meteo      hourly history from Open-Meteo (heat_weather) where the sensor has
--                   nothing within 90 minutes, same rules;
--   referentiejaar  otherwise the same hour in the reference year (heat_reference).
CREATE OR REPLACE FUNCTION heat_temperature(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (minute timestamptz, temp double precision, source text)
LANGUAGE sql STABLE AS $$
    WITH sen AS (
        SELECT bucket AS ts, value_avg AS v, 'sensor'::text AS src
        FROM ltss_1m
        WHERE entity_id = heat_entity_id('outdoor_temp') AND value_avg IS NOT NULL
          AND bucket >= p_from - INTERVAL '3 hours' AND bucket < p_to + INTERVAL '3 hours'
    ),
    arc AS (
        SELECT w.time AS ts, w.temp AS v, 'open-meteo'::text AS src
        FROM heat_weather w
        WHERE w.time >= p_from - INTERVAL '3 hours' AND w.time < p_to + INTERVAL '3 hours'
          AND NOT EXISTS (SELECT 1 FROM sen
                          WHERE sen.ts BETWEEN w.time - INTERVAL '90 minutes' AND w.time + INTERVAL '90 minutes')
    ),
    smp AS (SELECT * FROM sen UNION ALL SELECT * FROM arc),
    m AS (
        SELECT generate_series(p_from - INTERVAL '3 hours', p_to + INTERVAL '3 hours', INTERVAL '1 minute') AS t
    ),
    raw AS (
        SELECT m.t, s.v, s.src, CASE WHEN s.v IS NOT NULL THEN m.t END AS ts
        FROM m LEFT JOIN smp s ON s.ts = m.t
    ),
    marks AS (
        SELECT t, v, src, ts,
               count(ts) OVER (ORDER BY t) AS g_prev,
               count(ts) OVER (ORDER BY t DESC) AS g_next
        FROM raw
    ),
    around AS (
        SELECT t, v, src,
               first_value(ts)  OVER (PARTITION BY g_prev ORDER BY t) AS t_prev,
               first_value(v)   OVER (PARTITION BY g_prev ORDER BY t) AS v_prev,
               first_value(src) OVER (PARTITION BY g_prev ORDER BY t) AS src_prev,
               first_value(ts)  OVER (PARTITION BY g_next ORDER BY t DESC) AS t_next,
               first_value(v)   OVER (PARTITION BY g_next ORDER BY t DESC) AS v_next
        FROM marks
    ),
    est AS (
        SELECT t,
               CASE WHEN v IS NOT NULL THEN v
                    WHEN t_prev IS NOT NULL AND t_next IS NOT NULL AND t_next - t_prev <= INTERVAL '3 hours'
                    THEN v_prev + (v_next - v_prev)
                         * extract(epoch FROM t - t_prev) / extract(epoch FROM t_next - t_prev)
                    WHEN t_prev IS NOT NULL AND t - t_prev <= INTERVAL '3 hours' THEN v_prev
               END AS temp,
               CASE WHEN v IS NOT NULL THEN src ELSE src_prev END AS src
        FROM around
    )
    SELECT e.t,
           coalesce(e.temp, r.temp),
           CASE WHEN e.temp IS NOT NULL THEN e.src ELSE 'referentiejaar' END
    FROM est e
    LEFT JOIN heat_reference r ON r.hour = heat_ref_hour(e.t)
    WHERE e.t >= p_from AND e.t < p_to
$$;

-- --- Per minute -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heat_1m (
    minute          timestamptz NOT NULL PRIMARY KEY,
    gas             double precision NOT NULL,  -- m³ used (P1)
    gas_space       double precision NOT NULL,  -- m³ of it for heating the house
    gas_dhw         double precision NOT NULL,  -- m³ for hot water
    temp            double precision,           -- outdoor °C
    temp_source     text,                       -- sensor, open-meteo, referentiejaar
    degree_hours    double precision NOT NULL,  -- max(0, heating limit - temp) / 60
    q_space         double precision NOT NULL,  -- kWh useful heat for the house
    q_dhw           double precision NOT NULL,  -- kWh useful heat for hot water
    t_supply        double precision,           -- °C heat pump supply (heating curve)
    cop_space       double precision,
    cop_dhw         double precision,
    f_backup        double precision NOT NULL,  -- share of the space heat from the element
    tariff          text,                       -- 'low' or 'normal'
    p_gas           double precision,           -- EUR/m³, contract
    p_elec          double precision,           -- EUR/kWh, contract, active tariff
    p_elec_source   text,                       -- contract or aanname
    export_kwh      double precision NOT NULL,  -- solar surplus to the grid this minute
    p_export        double precision,           -- EUR/kWh given up per kWh of surplus used
    wp_kwh          double precision NOT NULL,  -- electricity of the heat pump incl. element
    wp_backup_kwh   double precision NOT NULL,  -- of which the element
    wp_solar_kwh    double precision NOT NULL,  -- of which from the solar surplus
    cv_cost         double precision,           -- EUR, gas, contract
    cv_fixed        double precision,           -- EUR, gas connection + boiler maintenance
    wp_cost         double precision,           -- EUR, electricity, contract
    wp_fixed        double precision            -- EUR, heat pump maintenance
);
SELECT create_hypertable('heat_1m', by_range('minute', INTERVAL '7 days'), if_not_exists => TRUE);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_name = 'heat_1m' AND compression_enabled) THEN
        ALTER TABLE heat_1m SET (timescaledb.compress, timescaledb.compress_orderby = 'minute DESC');
    END IF;
END $$;
SELECT add_compression_policy('heat_1m', compress_after => INTERVAL '30 days', if_not_exists => TRUE);
ALTER TABLE heat_1m
    ADD COLUMN IF NOT EXISTS gas_class   text,              -- tapwater, los, verwarming
    ADD COLUMN IF NOT EXISTS p_gas_mkt   double precision,  -- EUR/m³, market, all-in
    ADD COLUMN IF NOT EXISTS p_elec_mkt  double precision,  -- EUR/kWh, market, all-in
    ADD COLUMN IF NOT EXISTS cv_cost_mkt double precision,  -- EUR, gas, market
    ADD COLUMN IF NOT EXISTS wp_cost_mkt double precision;  -- EUR, electricity, market

DROP VIEW IF EXISTS heat_fit_days;
CREATE TABLE IF NOT EXISTS heat_1d (
    day             date PRIMARY KEY,
    minutes         integer NOT NULL,
    day_minutes     integer NOT NULL,           -- length of the day (DST: 1380 or 1500)
    measured_minutes integer NOT NULL,          -- minutes with a measured temperature
    temp_avg        double precision,
    degree_hours    double precision NOT NULL,
    gas             double precision NOT NULL,
    gas_space       double precision NOT NULL,
    gas_dhw         double precision NOT NULL,
    q_space         double precision NOT NULL,
    q_dhw           double precision NOT NULL,
    wp_kwh          double precision NOT NULL,
    wp_backup_kwh   double precision NOT NULL,
    wp_solar_kwh    double precision NOT NULL,
    cv_cost         double precision,
    cv_fixed        double precision,
    wp_cost         double precision,
    wp_fixed        double precision
);
ALTER TABLE heat_1d
    ADD COLUMN IF NOT EXISTS dhw_runs     integer,          -- tap water runs (showers etc.)
    ADD COLUMN IF NOT EXISTS cv_cost_mkt  double precision,
    ADD COLUMN IF NOT EXISTS wp_cost_mkt  double precision,
    ADD COLUMN IF NOT EXISTS mkt_minutes  integer;          -- minutes with both market prices

-- Columns of an earlier version (cooking, hybrid heat pump).
ALTER TABLE heat_1m
    DROP COLUMN IF EXISTS gas_cook, DROP COLUMN IF EXISTS f_hybrid,
    DROP COLUMN IF EXISTS wp_cook_kwh, DROP COLUMN IF EXISTS hyb_kwh,
    DROP COLUMN IF EXISTS hyb_solar_kwh, DROP COLUMN IF EXISTS hyb_gas,
    DROP COLUMN IF EXISTS hyb_cost, DROP COLUMN IF EXISTS hyb_fixed;
ALTER TABLE heat_1d
    DROP COLUMN IF EXISTS gas_cook, DROP COLUMN IF EXISTS hyb_kwh,
    DROP COLUMN IF EXISTS hyb_gas, DROP COLUMN IF EXISTS hyb_cost, DROP COLUMN IF EXISTS hyb_fixed;

-- Days usable for the heat loss fit: complete, with a measured temperature for at least
-- 90 % of the minutes, and in the past.
CREATE OR REPLACE VIEW heat_fit_days AS
    SELECT * FROM heat_1d
    WHERE minutes >= day_minutes AND measured_minutes >= 0.9 * minutes
      AND day < (now() AT TIME ZONE 'Europe/Amsterdam')::date;

-- First minute with gas data.
CREATE OR REPLACE FUNCTION heat_first() RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT min(bucket) FROM ltss_1m WHERE entity_id = heat_entity_id('gas')
$$;

-- (Re)calculate heat_1m for the whole local days that [p_from, p_to) touches, up to now, and
-- those days in heat_1d. Returns the number of minutes written.
CREATE OR REPLACE FUNCTION heat_recompute(p_from timestamptz, p_to timestamptz)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_first   timestamptz := heat_first();
    v_from    timestamptz;
    v_to      timestamptz := date_trunc('minute', least(p_to, now()));
    k_gas     double precision := heat_param_now('gas_kwh_per_m3');
    eff_h     double precision := heat_param_now('boiler_eff_heating') / 100;
    eff_d     double precision := heat_param_now('boiler_eff_dhw') / 100;
    v_dhw_kw  double precision := heat_param_now('dhw_min_kw');
    v_dhw_min double precision := heat_param_now('dhw_max_minutes');
    v_limit   double precision := heat_param_now('heating_limit');
    v_sup_min double precision := heat_param_now('supply_min');
    v_sup_des double precision := heat_param_now('supply_design');
    v_dhw_t   double precision := heat_param_now('dhw_temp');
    v_defrost double precision := heat_param_now('defrost_loss');
    v_eta     double precision := heat_eta();
    v_cop_max double precision := heat_param_now('cop_max');
    v_cap     double precision := heat_param_now('hp_capacity');
    v_rows    integer;
BEGIN
    IF v_first IS NULL OR v_to IS NULL THEN
        RETURN 0;
    END IF;
    v_from := date_trunc('day', greatest(p_from, v_first) AT TIME ZONE 'Europe/Amsterdam')
              AT TIME ZONE 'Europe/Amsterdam';
    IF v_from >= v_to THEN
        RETURN 0;
    END IF;
    IF v_cap IS NULL OR v_cap <= 0 THEN
        v_cap := heat_auto_capacity();
    END IF;

    WITH r AS MATERIALIZED (SELECT * FROM heat_price_ranges()),
    sr AS MATERIALIZED (SELECT * FROM solar_setting_ranges() WHERE key IN ('import_low', 'import_normal')),
    m AS (
        SELECT generate_series(v_from, v_to - INTERVAL '1 minute', INTERVAL '1 minute') AS t
    ),
    base AS (
        SELECT m.t,
               coalesce(g.gas, 0) AS gas,
               coalesce(g.gas_dhw, 0) AS gas_dhw,
               g.class AS gas_class,
               tp.temp, tp.source AS temp_source,
               coalesce(tf.tariff, heat_tariff_rule(m.t)) AS tariff,
               coalesce(s.export_low + s.export_normal, 0) AS export_kwh,
               coalesce(s.price_export_net, 0) AS p_export,
               (SELECT value FROM r WHERE key = 'gas_price' AND m.t >= valid_from AND m.t < valid_to) AS p_gas,
               (SELECT value FROM r WHERE key = 'gas_fixed_month' AND m.t >= valid_from AND m.t < valid_to) AS gas_fixed_month,
               (SELECT value FROM r WHERE key = 'electricity_price' AND m.t >= valid_from AND m.t < valid_to) AS p_elec_default,
               (SELECT value FROM r WHERE key = 'maint_boiler' AND m.t >= valid_from AND m.t < valid_to) AS maint_boiler,
               (SELECT value FROM r WHERE key = 'maint_hp' AND m.t >= valid_from AND m.t < valid_to) AS maint_hp,
               (SELECT value FROM r WHERE key = 'market_gas_markup' AND m.t >= valid_from AND m.t < valid_to) AS mk_gas,
               (SELECT value FROM r WHERE key = 'gas_energy_tax' AND m.t >= valid_from AND m.t < valid_to) AS tax_gas,
               (SELECT value FROM r WHERE key = 'market_elec_markup' AND m.t >= valid_from AND m.t < valid_to) AS mk_elec,
               (SELECT value FROM r WHERE key = 'elec_energy_tax' AND m.t >= valid_from AND m.t < valid_to) AS tax_elec,
               (SELECT value FROM r WHERE key = 'vat' AND m.t >= valid_from AND m.t < valid_to) AS vat,
               (SELECT value FROM sr WHERE key = 'import_low' AND m.t >= valid_from AND m.t < valid_to) AS c_low,
               (SELECT value FROM sr WHERE key = 'import_normal' AND m.t >= valid_from AND m.t < valid_to) AS c_normal,
               -- Market prices: the day-ahead electricity price of this quarter, and the gas
               -- price of this hour (or the last one in the 48 hours before).
               (SELECT CASE WHEN p.end_time > m.t THEN p.price_eur_mwh / 1000 END
                FROM energy_price p WHERE p.area = 'NL' AND p.start <= m.t
                ORDER BY p.start DESC LIMIT 1) AS da_elec,
               (SELECT q.price FROM heat_gas_market q
                WHERE q.time <= m.t AND q.time > m.t - INTERVAL '48 hours'
                ORDER BY q.time DESC LIMIT 1) AS da_gas,
               extract(epoch FROM (date_trunc('month', m.t AT TIME ZONE 'Europe/Amsterdam') + INTERVAL '1 month')
                                - date_trunc('month', m.t AT TIME ZONE 'Europe/Amsterdam')) / 60 AS month_minutes
        FROM m
        LEFT JOIN heat_gas(v_from, v_to, v_dhw_kw, v_dhw_min, k_gas) g ON g.minute = m.t
        LEFT JOIN heat_temperature(v_from, v_to) tp ON tp.minute = m.t
        LEFT JOIN solar_tariff(solar_entity_id('tariff'), v_from, v_to) tf ON tf.minute = m.t
        LEFT JOIN solar_1m s ON s.minute = m.t
    ),
    c1 AS (
        SELECT b.*,
               b.gas - b.gas_dhw AS gas_space,
               heat_supply(b.temp, v_limit, v_sup_min, v_sup_des) AS t_supply,
               CASE WHEN b.tariff = 'low' AND b.c_low > 0 THEN b.c_low
                    WHEN b.tariff <> 'low' AND b.c_normal > 0 THEN b.c_normal
                    ELSE b.p_elec_default END AS p_elec,
               CASE WHEN (b.tariff = 'low' AND b.c_low > 0) OR (b.tariff <> 'low' AND b.c_normal > 0)
                    THEN 'contract' ELSE 'aanname' END AS p_elec_source,
               (b.da_gas + b.mk_gas + b.tax_gas) * (1 + b.vat / 100) AS p_gas_mkt,
               (b.da_elec + b.mk_elec + b.tax_elec) * (1 + b.vat / 100) AS p_elec_mkt
        FROM base b
    ),
    c2 AS (
        SELECT c1.*,
               gas_space * k_gas * eff_h AS q_space,
               gas_dhw * k_gas * eff_d AS q_dhw,
               heat_cop(v_eta, t_supply, temp, v_defrost, v_cop_max) AS cop_space,
               heat_cop(v_eta, v_dhw_t, temp, v_defrost, v_cop_max) AS cop_dhw
        FROM c1
    ),
    -- The capacity of a heat pump is per hour: a boiler heats in short bursts, a heat pump
    -- runs longer at a lower power, so only the heat per hour has to fit. Hot water comes
    -- from a storage tank and does not count.
    c3 AS (
        SELECT c2.*,
               sum(q_space) OVER (PARTITION BY date_trunc('hour', t)) AS q_space_hour
        FROM c2
    ),
    c4 AS (
        SELECT c3.*,
               CASE WHEN q_space_hour > v_cap THEN (q_space_hour - v_cap) / q_space_hour ELSE 0 END AS f_backup
        FROM c3
    ),
    c5 AS (
        SELECT c4.*,
               q_space / cop_space * (1 - f_backup) + q_space * f_backup + q_dhw / cop_dhw AS wp_kwh,
               q_space * f_backup AS wp_backup_kwh,
               gas_fixed_month / month_minutes AS gas_fixed_min
        FROM c4
    ),
    c6 AS (
        SELECT c5.*, least(wp_kwh, export_kwh) AS wp_solar_kwh FROM c5
    )
    INSERT INTO heat_1m AS h (
        minute, gas, gas_space, gas_dhw, gas_class, temp, temp_source, degree_hours,
        q_space, q_dhw, t_supply, cop_space, cop_dhw, f_backup, tariff, p_gas, p_elec,
        p_elec_source, p_gas_mkt, p_elec_mkt, export_kwh, p_export, wp_kwh, wp_backup_kwh,
        wp_solar_kwh, cv_cost, cv_cost_mkt, cv_fixed, wp_cost, wp_cost_mkt, wp_fixed)
    SELECT t, gas, gas_space, gas_dhw, gas_class, temp, temp_source,
           greatest(v_limit - temp, 0) / 60,
           q_space, q_dhw, t_supply, cop_space, cop_dhw, f_backup, tariff, p_gas, p_elec,
           p_elec_source, p_gas_mkt, p_elec_mkt, export_kwh, p_export, wp_kwh, wp_backup_kwh,
           wp_solar_kwh,
           gas * p_gas,
           gas * p_gas_mkt,
           gas_fixed_min + maint_boiler / 525960,
           wp_solar_kwh * p_export + (wp_kwh - wp_solar_kwh) * p_elec,
           wp_solar_kwh * p_export + (wp_kwh - wp_solar_kwh) * p_elec_mkt,
           maint_hp / 525960
    FROM c6
    ON CONFLICT (minute) DO UPDATE SET
        gas = EXCLUDED.gas, gas_space = EXCLUDED.gas_space, gas_dhw = EXCLUDED.gas_dhw,
        gas_class = EXCLUDED.gas_class, temp = EXCLUDED.temp, temp_source = EXCLUDED.temp_source,
        degree_hours = EXCLUDED.degree_hours, q_space = EXCLUDED.q_space, q_dhw = EXCLUDED.q_dhw,
        t_supply = EXCLUDED.t_supply, cop_space = EXCLUDED.cop_space, cop_dhw = EXCLUDED.cop_dhw,
        f_backup = EXCLUDED.f_backup,
        -- Raw tariff states are gone after 30 days: keep the stored one then.
        tariff = CASE WHEN h.tariff IS NOT NULL AND h.minute < now() - INTERVAL '29 days'
                      THEN h.tariff ELSE EXCLUDED.tariff END,
        p_gas = EXCLUDED.p_gas, p_elec = EXCLUDED.p_elec, p_elec_source = EXCLUDED.p_elec_source,
        p_gas_mkt = EXCLUDED.p_gas_mkt, p_elec_mkt = EXCLUDED.p_elec_mkt,
        export_kwh = EXCLUDED.export_kwh, p_export = EXCLUDED.p_export,
        wp_kwh = EXCLUDED.wp_kwh, wp_backup_kwh = EXCLUDED.wp_backup_kwh,
        wp_solar_kwh = EXCLUDED.wp_solar_kwh,
        cv_cost = EXCLUDED.cv_cost, cv_cost_mkt = EXCLUDED.cv_cost_mkt, cv_fixed = EXCLUDED.cv_fixed,
        wp_cost = EXCLUDED.wp_cost, wp_cost_mkt = EXCLUDED.wp_cost_mkt, wp_fixed = EXCLUDED.wp_fixed;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    PERFORM heat_recompute_days((v_from AT TIME ZONE 'Europe/Amsterdam')::date,
                                ((v_to - INTERVAL '1 minute') AT TIME ZONE 'Europe/Amsterdam')::date);
    RETURN v_rows;
END $$;

CREATE OR REPLACE FUNCTION heat_recompute_days(p_first date, p_last date)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM heat_1d WHERE day BETWEEN p_first AND p_last;
    INSERT INTO heat_1d (day, minutes, day_minutes, measured_minutes, temp_avg, degree_hours,
                         gas, gas_space, gas_dhw, q_space, q_dhw, wp_kwh, wp_backup_kwh,
                         wp_solar_kwh, cv_cost, cv_fixed, wp_cost, wp_fixed, dhw_runs,
                         cv_cost_mkt, wp_cost_mkt, mkt_minutes)
    SELECT d.day, count(*),
           extract(epoch FROM ((d.day + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam')
                            - (d.day::timestamp AT TIME ZONE 'Europe/Amsterdam'))::integer / 60,
           count(*) FILTER (WHERE temp_source IN ('sensor', 'open-meteo')),
           avg(temp), sum(degree_hours),
           sum(gas), sum(gas_space), sum(gas_dhw), sum(q_space), sum(q_dhw),
           sum(wp_kwh), sum(wp_backup_kwh), sum(wp_solar_kwh),
           sum(cv_cost), sum(cv_fixed), sum(wp_cost), sum(wp_fixed),
           -- a run starts at a tap water minute after a minute without
           count(*) FILTER (WHERE gas_class = 'tapwater' AND prev_class IS DISTINCT FROM 'tapwater'),
           sum(cv_cost_mkt), sum(wp_cost_mkt),
           count(*) FILTER (WHERE p_gas_mkt IS NOT NULL AND p_elec_mkt IS NOT NULL)
    FROM (
        SELECT h.*, (minute AT TIME ZONE 'Europe/Amsterdam')::date AS day,
               lag(gas_class) OVER (ORDER BY minute) AS prev_class
        FROM heat_1m h
        WHERE minute >= (p_first::timestamp AT TIME ZONE 'Europe/Amsterdam')
          AND minute < ((p_last + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam')
    ) d
    GROUP BY d.day;
END $$;

-- --- Totals -----------------------------------------------------------------------------
-- Running totals since the first gas data, for the SQL sensors (packages/heat.yaml). HA turns
-- them into long-term statistics (sums per day, week, month, year). A changed model
-- parameter recomputes all history, so a total can then jump. The market totals only count
-- minutes with a market price; the fixed costs are the same as on the contract.
DROP FUNCTION IF EXISTS heat_totals();
CREATE OR REPLACE FUNCTION heat_totals()
RETURNS TABLE (cv double precision, wp double precision,
               cv_mkt double precision, wp_mkt double precision,
               cv_var double precision, wp_var double precision,
               gas double precision, gas_dhw double precision, heat double precision,
               wp_kwh double precision, days double precision)
LANGUAGE sql STABLE AS $$
    SELECT sum(cv_cost + cv_fixed), sum(wp_cost + wp_fixed),
           sum(coalesce(cv_cost_mkt, 0) + cv_fixed), sum(coalesce(wp_cost_mkt, 0) + wp_fixed),
           sum(cv_cost), sum(wp_cost),
           sum(gas), sum(gas_dhw), sum(q_space + q_dhw), sum(wp_kwh),
           sum(minutes) / 1440.0
    FROM heat_1d
$$;

-- --- Heat loss and forecast -------------------------------------------------------------
-- Fills heat_result:
--   fit          heat loss coefficient H (W/K) = heat for the house / degree hours, over
--                heat_fit_days, with R² of the daily values (line through zero). While that
--                is unreliable (< 30 days or R² < 0.5) and the gas use of last year is known,
--                H is calibrated on it instead;
--   required_kw  H x (heating limit + 10): the heat needed per hour at -10 °C;
--   dhw_per_day  measured hot water gas per day, over complete days;
--   year_*       one year with the temperatures of the reference year, H, the measured hot
--                water per day and the prices of now: the contract, and the average market
--                price of the last 30 days. Solar surplus not counted;
--   forecast     the same as a markdown table.
CREATE OR REPLACE FUNCTION heat_results() RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    k_gas     double precision := heat_param_now('gas_kwh_per_m3');
    eff_h     double precision := heat_param_now('boiler_eff_heating') / 100;
    eff_d     double precision := heat_param_now('boiler_eff_dhw') / 100;
    v_limit   double precision := heat_param_now('heating_limit');
    v_sup_min double precision := heat_param_now('supply_min');
    v_sup_des double precision := heat_param_now('supply_design');
    v_dhw_t   double precision := heat_param_now('dhw_temp');
    v_defrost double precision := heat_param_now('defrost_loss');
    v_eta     double precision := heat_eta();
    v_cop_max double precision := heat_param_now('cop_max');
    v_cap     double precision := heat_param_now('hp_capacity');
    p_gas     double precision := heat_price_now('gas_price');
    p_fixed   double precision := 12 * heat_price_now('gas_fixed_month');
    m_boiler  double precision := heat_price_now('maint_boiler');
    m_hp      double precision := heat_price_now('maint_hp');
    c_low     double precision := solar_setting_now('import_low');
    c_normal  double precision := solar_setting_now('import_normal');
    v_low     double precision;
    p_elec    double precision;
    p_gas_m   double precision;
    p_elec_m  double precision;
    v_h       double precision;   -- kW/K, used
    v_h_fit   double precision;   -- kW/K, measured
    v_h_src   text := 'meting';
    v_last_y  double precision := heat_param_now('gas_last_year');
    v_r2      double precision;
    v_n       integer;
    v_req     double precision;
    v_req_old double precision := (SELECT value FROM heat_result WHERE key = 'required_kw');
    v_dhw     double precision;   -- m³ hot water gas per day
    v_dhw_n   integer;
    y         record;
    v_cv      double precision;
    v_wp      double precision;
    v_cv_m    double precision;
    v_wp_m    double precision;
BEGIN
    -- Measured hot water (needed for the calibration).
    SELECT avg(gas_dhw), count(*) INTO v_dhw, v_dhw_n FROM heat_1d WHERE minutes >= day_minutes;

    -- Heat loss coefficient: measured, or calibrated on last year's gas while uncertain.
    SELECT sum(q_space) / nullif(sum(degree_hours), 0), count(*)
    INTO v_h_fit, v_n
    FROM heat_fit_days WHERE degree_hours > 0;
    SELECT 1 - sum((q_space - v_h_fit * degree_hours) ^ 2) / nullif(sum((q_space - a.q) ^ 2), 0)
    INTO v_r2
    FROM heat_fit_days, (SELECT avg(q_space) AS q FROM heat_fit_days WHERE degree_hours > 0) a
    WHERE degree_hours > 0;
    v_h := v_h_fit;
    IF (v_h_fit IS NULL OR v_n < 30 OR coalesce(v_r2, 0) < 0.5) AND v_last_y > 0 THEN
        SELECT greatest(v_last_y - coalesce(v_dhw, 0) * 365, 0) * k_gas * eff_h
               / nullif(sum(greatest(v_limit - temp, 0)), 0)
        INTO v_h FROM heat_reference;
        v_h_src := 'jaarafrekening';
    END IF;
    v_req := v_h * (v_limit + 10);
    INSERT INTO heat_result (key, value, detail, updated) VALUES
        ('fit', 1000 * v_h,
         CASE WHEN v_h IS NULL THEN 'Nog geen volledige dag met gemeten buitentemperatuur.'
              WHEN v_h_src = 'jaarafrekening' THEN format(
                  'H = %s W/K, geijkt op het gasverbruik van vorig jaar (%s m³ min %s m³ warm water). '
                  'Gemeten tot nu toe: %s W/K uit %s dagen, R² = %s; die gaat voor zodra hij betrouwbaar is '
                  '(30 dagen en R² boven 0,5).',
                  round((1000 * v_h)::numeric), round(v_last_y::numeric), round((coalesce(v_dhw, 0) * 365)::numeric),
                  coalesce(round((1000 * v_h_fit)::numeric)::text, '–'), v_n,
                  coalesce(round(v_r2::numeric, 2)::text, '–'))
              ELSE format('H = %s W/K gemeten uit %s dagen; R² = %s. Warmte voor het huis per dag = H x graaduren (stookgrens %s °C).',
                          round((1000 * v_h)::numeric), v_n, coalesce(round(v_r2::numeric, 2)::text, '–'), v_limit) END,
         now()),
        ('required_kw', v_req,
         format('H x (%s + 10) K. Zonder marge voor warm water en opwarmen na een nachtverlaging.', v_limit), now())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, detail = EXCLUDED.detail, updated = EXCLUDED.updated;

    INSERT INTO heat_result (key, value, detail, updated) VALUES
        ('dhw_per_day', v_dhw,
         format('Gemiddeld gas voor warm water per dag, gemeten over %s volledige dagen (%s tapbeurten per dag).',
                v_dhw_n, coalesce(round((SELECT avg(dhw_runs) FROM heat_1d WHERE minutes >= day_minutes)::numeric, 1)::text, '–')),
         now())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, detail = EXCLUDED.detail, updated = EXCLUDED.updated;
    v_dhw := coalesce(v_dhw, 0);

    -- Prices for the forecast.
    SELECT sum(wp_kwh) FILTER (WHERE tariff = 'low') / nullif(sum(wp_kwh), 0),
           sum(wp_kwh * p_elec_mkt) / nullif(sum(wp_kwh) FILTER (WHERE p_elec_mkt IS NOT NULL), 0),
           avg(p_gas_mkt)
    INTO v_low, p_elec_m, p_gas_m
    FROM heat_1m WHERE minute > now() - INTERVAL '30 days';
    v_low := coalesce(v_low, 0.5);
    p_elec := CASE WHEN c_low > 0 AND c_normal > 0 THEN v_low * c_low + (1 - v_low) * c_normal
                   WHEN c_normal > 0 THEN c_normal
                   ELSE heat_price_now('electricity_price') END;

    IF v_cap IS NULL OR v_cap <= 0 THEN
        v_cap := greatest(4, coalesce(v_req, 0));
    END IF;

    IF v_h IS NULL THEN
        DELETE FROM heat_result WHERE key LIKE 'year\_%' OR key = 'forecast';
    ELSE
        SELECT sum(gas) AS gas, sum(q) AS q, sum(wp) AS wp, sum(wp_backup) AS wp_backup
        INTO y
        FROM (
            SELECT q_space / (k_gas * eff_h) + v_dhw / 24 AS gas,
                   q_space + q_dhw AS q,
                   q_space / cop_s * (1 - f_b) + q_space * f_b + q_dhw / cop_d AS wp,
                   q_space * f_b AS wp_backup
            FROM (
                SELECT g.*,
                       CASE WHEN q_space > v_cap THEN (q_space - v_cap) / q_space ELSE 0 END AS f_b
                FROM (
                    SELECT v_h * greatest(v_limit - temp, 0) AS q_space,
                           v_dhw / 24 * k_gas * eff_d AS q_dhw,
                           heat_cop(v_eta, heat_supply(temp, v_limit, v_sup_min, v_sup_des), temp, v_defrost, v_cop_max) AS cop_s,
                           heat_cop(v_eta, v_dhw_t, temp, v_defrost, v_cop_max) AS cop_d
                    FROM heat_reference
                ) g
            ) h
        ) hours;

        v_cv := y.gas * p_gas + p_fixed + m_boiler;
        v_wp := y.wp * p_elec + m_hp;
        v_cv_m := y.gas * p_gas_m + p_fixed + m_boiler;
        v_wp_m := y.wp * p_elec_m + m_hp;

        INSERT INTO heat_result (key, value, detail, updated) VALUES
            ('year_gas_m3', y.gas, 'Gasverbruik per jaar met de CV-ketel, volgens het model.', now()),
            ('year_heat_kwh', y.q, 'Nuttige warmte per jaar (huis + warm water).', now()),
            ('year_cost_cv', v_cv, 'Contractprijs.', now()),
            ('year_cost_wp', v_wp, 'Contractprijs.', now()),
            ('year_cost_cv_mkt', v_cv_m, 'Marktprijs (gemiddelde laatste 30 dagen).', now()),
            ('year_cost_wp_mkt', v_wp_m, 'Marktprijs (gemiddelde laatste 30 dagen).', now()),
            ('year_wp_kwh', y.wp, NULL, now()),
            ('year_scop', y.q / nullif(y.wp - y.wp_backup, 0), 'SCOP: warmte / stroom van de warmtepomp zelf (zonder element).', now()),
            ('forecast', v_cv - v_wp, format(
E'| | CV-ketel | Warmtepomp |\n|---|---:|---:|\n'
'| Gas (m³/jaar) | %s | 0 |\n'
'| Stroom (kWh/jaar) | 0 | %s |\n'
'| Energie, contractprijs (€/jaar) | %s | %s |\n'
'| Energie, marktprijs (€/jaar) | %s | %s |\n'
'| Vaste kosten gasaansluiting (€/jaar) | %s | 0 |\n'
'| Onderhoud (€/jaar) | %s | %s |\n'
'| **Totaal, contractprijs (€/jaar)** | **%s** | **%s** |\n'
'| **Totaal, marktprijs (€/jaar)** | **%s** | **%s** |\n\n'
'Verschil per jaar: %s met de contractprijs, %s met de marktprijs (positief = warmtepomp goedkoper).\n\n'
'Rekenbasis: warmteverlies %s W/K (%s), warm water %s m³ gas per dag (gemeten), buitentemperaturen van '
'referentiejaar 2019. Contract: gas € %s/m³, stroom € %s/kWh. Markt (gemiddelde laatste 30 dagen): '
'gas € %s/m³, stroom € %s/kWh. Element: %s kWh/jaar (vermogen %s kW). SCOP: %s. '
'Zonnestroom is in de prognose niet meegerekend.%s%s',
                round(y.gas::numeric), round(y.wp::numeric),
                round((y.gas * p_gas)::numeric), round((y.wp * p_elec)::numeric),
                coalesce(round((y.gas * p_gas_m)::numeric)::text, '–'),
                coalesce(round((y.wp * p_elec_m)::numeric)::text, '–'),
                round(p_fixed::numeric), round(m_boiler::numeric), round(m_hp::numeric),
                round(v_cv::numeric), round(v_wp::numeric),
                coalesce(round(v_cv_m::numeric)::text, '–'), coalesce(round(v_wp_m::numeric)::text, '–'),
                to_char(v_cv - v_wp, 'FMSG99990'), coalesce(to_char(v_cv_m - v_wp_m, 'FMSG99990'), '–'),
                round((1000 * v_h)::numeric), v_h_src, round(v_dhw::numeric, 2),
                round(p_gas::numeric, 3), round(p_elec::numeric, 3),
                coalesce(round(p_gas_m::numeric, 3)::text, '–'), coalesce(round(p_elec_m::numeric, 3)::text, '–'),
                round(y.wp_backup::numeric), round(v_cap::numeric, 1),
                coalesce(round((y.q / nullif(y.wp - y.wp_backup, 0))::numeric, 2)::text, '–'),
                CASE WHEN v_h_src = 'meting' AND (v_n < 30 OR coalesce(v_r2, 0) < 0.5) THEN format(
                    E'\n\n**Nog onzeker:** het warmteverlies komt uit %s dagen (R² %s). Betrouwbaar wordt het na '
                    'een maand stookseizoen met koude en zachte dagen (R² boven 0,5).',
                    v_n, coalesce(round(v_r2::numeric, 2)::text, '–')) ELSE '' END,
                CASE WHEN v_h_src = 'jaarafrekening' THEN format(
                    E'

**Geijkt:** het warmteverlies volgt nu uit de jaarafrekening (%s m³), dus het gasverbruik '
                    'van het model is daaraan gelijk. De meting (%s dagen, R² %s) neemt het over zodra hij betrouwbaar is.',
                    round(v_last_y::numeric), v_n, coalesce(round(v_r2::numeric, 2)::text, '–'))
                     WHEN heat_param_now('gas_last_year') > 0 THEN format(
                    E'\n\n**Controle:** jaarafrekening %s m³, model %s m³ (%s %%).',
                    round(heat_param_now('gas_last_year')::numeric), round(y.gas::numeric),
                    to_char(100 * (y.gas / heat_param_now('gas_last_year') - 1), 'FMSG990')) ELSE '' END),
             now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, detail = EXCLUDED.detail, updated = EXCLUDED.updated;
    END IF;

    -- In automatic mode the capacity follows the fit; recompute history when it moved > 10 %.
    IF heat_param_now('hp_capacity') <= 0
       AND abs(greatest(4, coalesce(v_req, 0)) - greatest(4, coalesce(v_req_old, 0)))
           > 0.1 * greatest(4, coalesce(v_req_old, 0)) THEN
        PERFORM heat_recompute(heat_first(), now());
    END IF;
END $$;

-- --- Explanation ------------------------------------------------------------------------
-- The calculation of one minute, step by step with its numbers (markdown), for the
-- dashboard: the most recent minute with gas use in the last two days, else the latest.
CREATE OR REPLACE FUNCTION heat_explain()
RETURNS TABLE (shown timestamptz, md text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    h       heat_1m;
    dd      heat_1d;
    k_gas   double precision := heat_param_now('gas_kwh_per_m3');
    eff_h   double precision := heat_param_now('boiler_eff_heating');
    eff_d   double precision := heat_param_now('boiler_eff_dhw');
    v_limit double precision := heat_param_now('heating_limit');
    v_cap   double precision;
    v_taps  text;
    fmt     text := 'FM999990.000';
    eur     text := 'FM99990.00000';
BEGIN
    SELECT * INTO h FROM heat_1m
    WHERE heat_1m.minute > now() - INTERVAL '2 days' AND gas > 0
    ORDER BY heat_1m.minute DESC LIMIT 1;
    IF h.minute IS NULL THEN
        SELECT * INTO h FROM heat_1m ORDER BY heat_1m.minute DESC LIMIT 1;
    END IF;
    IF h.minute IS NULL THEN
        RETURN;
    END IF;
    SELECT * INTO dd FROM heat_1d WHERE day = (h.minute AT TIME ZONE 'Europe/Amsterdam')::date;
    v_cap := CASE WHEN heat_param_now('hp_capacity') > 0 THEN heat_param_now('hp_capacity')
                  ELSE heat_auto_capacity() END;
    -- Tap water runs of that day, for checking the recognition.
    SELECT string_agg(format('%s (%s m³)', to_char(t0 AT TIME ZONE 'Europe/Amsterdam', 'HH24:MI'),
                             to_char(m3, 'FM0.000')), ', ' ORDER BY t0)
    INTO v_taps
    FROM (
        SELECT min(minute) AS t0, sum(gas) AS m3
        FROM (SELECT minute, gas, sum(CASE WHEN gas_class = 'tapwater' AND prev IS DISTINCT FROM 'tapwater' THEN 1 ELSE 0 END)
                                      OVER (ORDER BY minute) AS run, gas_class
              FROM (SELECT minute, gas, gas_class, lag(gas_class) OVER (ORDER BY minute) AS prev
                    FROM heat_1m WHERE (minute AT TIME ZONE 'Europe/Amsterdam')::date = dd.day) a) b
        WHERE gas_class = 'tapwater'
        GROUP BY run
    ) runs;

    shown := h.minute;
    md := format(
E'### Minuut %s\n\n'
'**1. Gas.** De meter meldt elke 5 minuten de stand; het verschil wordt gelijk verdeeld over de 5 minuten ervoor. '
'Deze minuut: **%s m³** (%s kW gemiddeld over de melding).\n\n'
'**2. Waarvoor.** Deze melding is herkend als **%s**. Warm water: minstens %s kW, hooguit %s minuten achter elkaar, '
'of een losse melding zonder verbruik 10 minuten ervoor en erna. De rest is verwarming. '
'Deze minuut: verwarming %s m³, warm water %s m³. Tapbeurten op %s: %s.\n\n'
'**3. Nuttige warmte.** gas × %s kWh/m³ × rendement: verwarming %s × %s × %s %% = **%s kWh**, '
'warm water %s × %s × %s %% = **%s kWh**.\n\n'
'**4. Buiten.** %s °C (bron: %s). Stooklijn: %s °C bij de stookgrens van %s °C, oplopend naar %s °C bij -10 °C '
'→ aanvoer **%s °C**.\n\n'
'**5. COP.** %s × (aanvoer + 273,15) / (aanvoer − buiten)%s, hooguit %s = **%s** voor verwarming, **%s** voor warm water (%s °C).\n\n'
'**6. Warmtepomp.** %s / %s + %s / %s%s = **%s kWh** stroom. '
'Vermogen %s kW voor verwarming (warm water komt uit een boilervat).\n\n'
'**7. Prijzen.** Contract: gas € %s/m³, stroom € %s/kWh (%s tarief, %s). '
'Markt: gas € %s/m³, stroom € %s/kWh (marktprijs + opslag + energiebelasting, plus btw). '
'Zonnestroom-overschot deze minuut: %s kWh (gebruik ervan kost € %s/kWh gemiste vergoeding).\n\n'
'**8. Kosten deze minuut** (vaste kosten: gasaansluiting per maand en onderhoud per jaar, per minuut):\n\n'
'| | Contract | Markt | Vast |\n|---|---:|---:|---:|\n'
'| CV-ketel | € %s | € %s | € %s |\n| Warmtepomp | € %s | € %s | € %s |\n\n'
'**Op %s:** gas %s m³ (warm water %s m³), warmte %s kWh, gem. %s °C. '
'CV € %s (markt € %s), warmtepomp € %s (markt € %s), inclusief vaste kosten.',
        to_char(h.minute AT TIME ZONE 'Europe/Amsterdam', 'YYYY-MM-DD HH24:MI'),
        to_char(h.gas, fmt), round((h.gas * 5 * k_gas * 12)::numeric, 1),
        coalesce(h.gas_class, '–'), heat_param_now('dhw_min_kw'), heat_param_now('dhw_max_minutes'),
        to_char(h.gas_space, fmt), to_char(h.gas_dhw, fmt),
        to_char(dd.day, 'YYYY-MM-DD'), coalesce(v_taps, 'geen'),
        k_gas, to_char(h.gas_space, fmt), k_gas, eff_h, to_char(h.q_space, fmt),
        to_char(h.gas_dhw, fmt), k_gas, eff_d, to_char(h.q_dhw, fmt),
        round(h.temp::numeric, 1), h.temp_source, heat_param_now('supply_min'), v_limit,
        heat_param_now('supply_design'), round(h.t_supply::numeric, 1),
        round(heat_eta()::numeric, 3),
        CASE WHEN h.temp < 6 THEN format(' × (1 − %s %% ontdooien)', heat_param_now('defrost_loss')) ELSE '' END,
        heat_param_now('cop_max'),
        round(h.cop_space::numeric, 2), round(h.cop_dhw::numeric, 2), heat_param_now('dhw_temp'),
        to_char(h.q_space, fmt), round(h.cop_space::numeric, 2), to_char(h.q_dhw, fmt), round(h.cop_dhw::numeric, 2),
        CASE WHEN h.f_backup > 0 THEN format(' (verwarming %s %% via element, COP 1)', round((100 * h.f_backup)::numeric)) ELSE '' END,
        to_char(h.wp_kwh, fmt),
        round(v_cap::numeric, 1)::text || CASE WHEN heat_param_now('hp_capacity') > 0 THEN '' ELSE ' (automatisch)' END,
        to_char(h.p_gas, 'FM0.0000'), to_char(h.p_elec, 'FM0.0000'),
        CASE WHEN h.tariff = 'low' THEN 'dal' ELSE 'normaal' END,
        CASE WHEN h.p_elec_source = 'contract' THEN 'contract' ELSE 'helper: contract bij Zonnepanelen niet ingevuld' END,
        coalesce(to_char(h.p_gas_mkt, 'FM0.0000'), 'onbekend'), coalesce(to_char(h.p_elec_mkt, 'FM0.0000'), 'onbekend'),
        to_char(h.export_kwh, fmt), to_char(h.p_export, 'FM0.0000'),
        to_char(h.cv_cost, eur), coalesce(to_char(h.cv_cost_mkt, eur), '–'), to_char(h.cv_fixed, eur),
        to_char(h.wp_cost, eur), coalesce(to_char(h.wp_cost_mkt, eur), '–'), to_char(h.wp_fixed, eur),
        to_char(dd.day, 'YYYY-MM-DD'), to_char(dd.gas, 'FM9990.000'), to_char(dd.gas_dhw, 'FM9990.000'),
        to_char(dd.q_space + dd.q_dhw, 'FM9990.0'), round(dd.temp_avg::numeric, 1),
        to_char(dd.cv_cost + dd.cv_fixed, 'FM9990.00'), coalesce(to_char(dd.cv_cost_mkt + dd.cv_fixed, 'FM9990.00'), '–'),
        to_char(dd.wp_cost + dd.wp_fixed, 'FM9990.00'), coalesce(to_char(dd.wp_cost_mkt + dd.wp_fixed, 'FM9990.00'), '–'));
    RETURN NEXT;
END $$;

-- All assumptions with their value and source, and the quality of the data (markdown).
CREATE OR REPLACE FUNCTION heat_assumptions()
RETURNS TABLE (defaults integer, md text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_rows  text;
    v_days  double precision;
    v_src   text;
    v_fit   text := (SELECT detail FROM heat_result WHERE key = 'fit');
    v_dhw   record;
    v_mkt   text;
    v_elec  text;
BEGIN
    SELECT count(*) FILTER (WHERE NOT heat_param_set(key)),
           string_agg(format('| %s | %s %s | %s | %s | %s |', label,
                             CASE WHEN step >= 1 THEN round(heat_param_now(key)::numeric)::text
                                  ELSE rtrim(rtrim(round(heat_param_now(key)::numeric, 5)::text, '0'), '.') END,
                             unit,
                             CASE WHEN heat_param_set(key) THEN 'ingevuld' ELSE '**standaard**' END,
                             explain, refine), E'\n' ORDER BY sort)
    INTO defaults, v_rows
    FROM heat_param;

    SELECT sum(minutes) / 1440.0,
           format('%s %% van de minuten', round(100.0 * sum(mkt_minutes) / nullif(sum(minutes), 0)))
    INTO v_days, v_mkt FROM heat_1d;
    SELECT string_agg(format('%s %s %%', temp_source, round(100.0 * n / total)), ', ' ORDER BY n DESC)
    INTO v_src
    FROM (SELECT temp_source, count(*) AS n, sum(count(*)) OVER () AS total FROM heat_1m
          WHERE minute > now() - INTERVAL '30 days' GROUP BY 1) s;
    SELECT value, detail INTO v_dhw FROM heat_result WHERE key = 'dhw_per_day';
    v_elec := CASE WHEN solar_setting_now('import_normal') > 0
                   THEN 'Inkoopprijs normaal/dal van het dashboard Zonnepanelen; daarvoor de helper Stroomprijs contract'
                   ELSE '**helper** (Inkoopprijs normaal/dal op het dashboard Zonnepanelen staan op 0)' END;

    md := format(
E'### Datakwaliteit\n\n'
'- Gasdata: %s dagen (P1-meter, elke 5 minuten).\n'
'- Warm water (gemeten): %s\n'
'- Buitentemperatuur (laatste 30 dagen): %s.\n'
'- Marktprijzen bekend: %s (stroom: Nord Pool per kwartier; gas: EnergyZero per uur).\n'
'- Warmteverlies: %s\n'
'- Stroomprijs contract: %s.\n\n'
'### Aannames\n\n'
'**Standaard** betekent dat de waarde nog niet is aangepast. Wijzigen kan onder *Instellingen*. Een modelwaarde '
'geldt na wijzigen voor alle historie (alles wordt binnen 5 minuten opnieuw berekend); een prijs geldt vanaf het '
'moment van wijzigen, behalve de eerste wijziging: die vervangt de standaardwaarde ook voor het verleden.\n\n'
'| Aanname | Waarde | Bron | Wat het is | Verfijnen |\n|---|---|---|---|---|\n%s\n\n'
'### Wat het model (nog) niet weet\n\n'
'- **Warm water tijdens het verwarmen.** Een combiketel geeft tapwater voorrang; een douchebeurt midden in een '
'verwarmingsperiode telt helemaal als warm water, ook het stukje verwarming in hetzelfde interval van 5 minuten. '
'Een OpenTherm-koppeling met de ketel (tapwater of verwarming per minuut) zou dat exact maken.\n'
'- **De aanvoertemperatuur die het huis echt nodig heeft.** Bepaalt de COP het sterkst. Te meten met een '
'OpenTherm-thermostaat of een temperatuursensor op de aanvoerleiding.\n'
'- **Wind en zon.** Het warmteverlies is één getal (W/K) tegen de buitentemperatuur; wind en zoninstraling zitten '
'er gemiddeld in.\n'
'- **Thermostaatgedrag.** Een warmtepomp werkt beter zonder nachtverlaging; het model neemt de warmtevraag van nu over.\n'
'- **Vermogen bij vorst.** De capaciteit is één getal; in werkelijkheid levert een lucht/water-warmtepomp bij -10 °C minder.',
        round(coalesce(v_days, 0)::numeric, 1),
        CASE WHEN v_dhw.value IS NULL THEN 'nog geen volledige dag.'
             ELSE format('%s m³ per dag. %s', round(v_dhw.value::numeric, 2), v_dhw.detail) END,
        coalesce(v_src, '–'),
        coalesce(v_mkt, '–'),
        coalesce(v_fit, '–'),
        v_elec,
        v_rows);
    RETURN NEXT;
END $$;

-- --- Jobs -------------------------------------------------------------------------------
-- Every 5 minutes, after a change of a setting (heat_* helpers, or the electricity tariffs
-- of the solar panels): all history. Otherwise the minutes since the last run, always from
-- the local midnight before the last 6 hours (late gas reports, ltss_1m refresh, solar_1m,
-- new market prices).
CREATE OR REPLACE PROCEDURE heat_job(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
DECLARE
    v_seen double precision := (SELECT value FROM heat_result WHERE key = 'settings_seen');
    v_new  double precision := extract(epoch FROM greatest(
               (SELECT max(time) FROM heat_setting),
               (SELECT max(time) FROM solar_setting
                WHERE entity_id IN ('input_number.solar_import_low', 'input_number.solar_import_normal',
                                    'input_number.solar_feed_in', 'input_number.solar_feed_in_cost'))));
    v_last timestamptz := (SELECT max(minute) FROM heat_1m);
    v_day  timestamptz := date_trunc('day', (now() - INTERVAL '6 hours') AT TIME ZONE 'Europe/Amsterdam')
                          AT TIME ZONE 'Europe/Amsterdam';
BEGIN
    IF v_last IS NULL OR v_new IS DISTINCT FROM v_seen THEN
        PERFORM heat_recompute(heat_first(), now());
        INSERT INTO heat_result (key, value, detail, updated)
        VALUES ('settings_seen', v_new, 'Laatste instelling verwerkt (epoch); intern.', now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated = EXCLUDED.updated;
        PERFORM heat_results();
    ELSE
        PERFORM heat_recompute(least(v_day, v_last + INTERVAL '1 minute'), now());
    END IF;
END $$;

CREATE OR REPLACE PROCEDURE heat_results_job(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM heat_results();
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'heat_job') THEN
        PERFORM add_job('heat_job', INTERVAL '5 minutes');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'heat_results_job') THEN
        PERFORM add_job('heat_results_job', INTERVAL '1 hour');
    END IF;
END $$;
