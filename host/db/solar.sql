-- Solar panels: prices, settings and the calculation layer (docs/zonnepanelen.md).
--
-- Applied by install.sh on every run, after timescale.sql, so every statement must be
-- idempotent. Jobs are added only when missing; changing a schedule here does not alter
-- an existing job (alter_job by hand). Tables are never dropped; new columns need an
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS below the CREATE TABLE.
--
-- Data flow:
--   ltss (trigger solar_capture) -> energy_price (day-ahead prices, Nord Pool)
--                                -> solar_setting (helper values, changes only)
--   ltss_1m + ltss + energy_price + solar_setting -> solar_1m (per minute) -> solar_1d
--   solar_1m -> solar_result (battery simulation and surplus table, hourly)
--
-- Units: energy in kWh, money in EUR, prices in EUR/kWh unless the name says otherwise.
-- Days are local days (Europe/Amsterdam).

-- --- Configuration ----------------------------------------------------------------------
-- Entity id per role, written by host/ha/setup_solar.py:
--   import_low, import_normal   P1 counters, electricity used tariff 1 (low) and 2 (normal)
--   export_low, export_normal   P1 counters, electricity delivered tariff 1 and 2
--   tariff                      P1 active tariff ('low' or 'normal')
CREATE TABLE IF NOT EXISTS solar_entity (
    role      text PRIMARY KEY,
    entity_id text NOT NULL
);

CREATE OR REPLACE FUNCTION solar_entity_id(p_role text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT entity_id FROM solar_entity WHERE role = p_role
$$;

-- History of the helpers input_number.solar_* and input_datetime.solar_* (changes only).
-- input_datetime values are stored as epoch seconds (their 'timestamp' attribute).
CREATE TABLE IF NOT EXISTS solar_setting (
    entity_id text             NOT NULL,
    time      timestamptz      NOT NULL,
    value     double precision NOT NULL,
    PRIMARY KEY (entity_id, time)
);

-- Day-ahead prices as Nord Pool publishes them: EUR/MWh, without taxes or supplier markup.
CREATE TABLE IF NOT EXISTS energy_price (
    area          text             NOT NULL,
    start         timestamptz      NOT NULL,
    end_time      timestamptz      NOT NULL,
    price_eur_mwh double precision NOT NULL,
    fetched       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (area, start)
);

-- --- Capture from LTSS ------------------------------------------------------------------
-- LTSS keeps raw states for 30 days only. This trigger copies what must be kept forever:
-- the prices of sensor.solar_day_ahead_prices (attribute martha_day_ahead, see
-- packages/solar_prices.yaml) and every change of a solar helper. It never fails the
-- insert of LTSS: an error becomes a warning in the database log.
CREATE OR REPLACE FUNCTION solar_capture() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v double precision;
BEGIN
    BEGIN
        IF NEW.attributes ? 'martha_day_ahead' AND jsonb_typeof(NEW.attributes -> 'prices') = 'array' THEN
            INSERT INTO energy_price (area, start, end_time, price_eur_mwh)
            SELECT NEW.attributes ->> 'area', (p ->> 'start')::timestamptz,
                   (p ->> 'end')::timestamptz, (p ->> 'price')::double precision
            FROM jsonb_array_elements(NEW.attributes -> 'prices') AS p
            WHERE ltss_numeric(p ->> 'price') IS NOT NULL
            ON CONFLICT (area, start) DO UPDATE
                SET end_time = EXCLUDED.end_time, price_eur_mwh = EXCLUDED.price_eur_mwh,
                    fetched = now();
        ELSIF NEW.entity_id LIKE 'input\_number.solar\_%' OR NEW.entity_id LIKE 'input\_datetime.solar\_%' THEN
            v := CASE WHEN NEW.entity_id LIKE 'input\_datetime.%'
                      THEN ltss_numeric(NEW.attributes ->> 'timestamp')
                      ELSE ltss_numeric(NEW.state) END;
            IF v IS NOT NULL AND v IS DISTINCT FROM (
                    SELECT value FROM solar_setting
                    WHERE entity_id = NEW.entity_id AND time <= NEW.time
                    ORDER BY time DESC LIMIT 1) THEN
                INSERT INTO solar_setting (entity_id, time, value)
                VALUES (NEW.entity_id, NEW.time, v)
                ON CONFLICT DO NOTHING;
            END IF;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'solar_capture (%): %', NEW.entity_id, SQLERRM;
    END;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS solar_capture ON ltss;
CREATE TRIGGER solar_capture AFTER INSERT ON ltss
    FOR EACH ROW EXECUTE FUNCTION solar_capture();

-- --- Settings ---------------------------------------------------------------------------
-- Start of the investment (input_datetime.solar_start, a date: local midnight).
CREATE OR REPLACE FUNCTION solar_start() RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT to_timestamp(value) FROM solar_setting
    WHERE entity_id = 'input_datetime.solar_start'
    ORDER BY time DESC LIMIT 1
$$;

-- Validity ranges of input_number.solar_<key>. A value holds from the moment it was set.
-- Exception: the first change at or after the start applies from the start, so tariffs
-- can be filled in a few days late. Later changes hold from the moment they are made.
CREATE OR REPLACE FUNCTION solar_setting_ranges()
RETURNS TABLE (key text, valid_from timestamptz, valid_to timestamptz, value double precision)
LANGUAGE sql STABLE AS $$
    WITH st AS (SELECT solar_start() AS start),
    s AS (
        SELECT entity_id, time, value,
               time = min(time) FILTER (WHERE time >= st.start) OVER (PARTITION BY entity_id)
                   AS first_after_start,
               st.start
        FROM solar_setting, st
        WHERE entity_id LIKE 'input\_number.solar\_%'
    ),
    e AS (
        SELECT entity_id, value, time,
               CASE WHEN first_after_start THEN start ELSE time END AS eff
        FROM s
    )
    SELECT substr(entity_id, length('input_number.solar_') + 1),
           CASE WHEN row_number() OVER w = 1 THEN '-infinity'::timestamptz ELSE eff END,
           lead(eff, 1, 'infinity'::timestamptz) OVER w,
           value
    FROM e
    WINDOW w AS (PARTITION BY entity_id ORDER BY eff, time)
$$;

-- Current value of input_number.solar_<key> (NULL if it was never stored).
CREATE OR REPLACE FUNCTION solar_setting_now(p_key text) RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT value FROM solar_setting_ranges()
    WHERE key = p_key AND now() >= valid_from AND now() < valid_to
$$;

-- --- Series per minute ------------------------------------------------------------------
-- Use per minute of a counter (kWh) in [p_from, p_to): the reading at the end of the
-- minute minus the one a minute earlier, with the last known reading carried forward
-- (HA only writes on change). A drop (meter replaced) counts as 0. Minutes before the
-- first reading count as 0 too.
CREATE OR REPLACE FUNCTION solar_counter(p_entity text, p_from timestamptz, p_to timestamptz)
RETURNS TABLE (minute timestamptz, delta double precision)
LANGUAGE sql STABLE AS $$
    WITH m AS (
        SELECT generate_series(p_from - INTERVAL '1 minute', p_to - INTERVAL '1 minute',
                               INTERVAL '1 minute') AS t
    ),
    c AS (
        SELECT bucket, value_last FROM ltss_1m
        WHERE entity_id = p_entity
          AND bucket >= p_from - INTERVAL '1 minute' AND bucket < p_to
    ),
    raw AS (
        SELECT m.t,
               CASE WHEN m.t = p_from - INTERVAL '1 minute' AND c.value_last IS NULL
                    THEN (SELECT value_last FROM ltss_1m
                          WHERE entity_id = p_entity AND bucket < p_from - INTERVAL '1 minute'
                          ORDER BY bucket DESC LIMIT 1)
                    ELSE c.value_last END AS v
        FROM m LEFT JOIN c ON c.bucket = m.t
    ),
    grouped AS (SELECT t, v, count(v) OVER (ORDER BY t) AS grp FROM raw),
    filled AS (SELECT t, first_value(v) OVER (PARTITION BY grp ORDER BY t) AS v FROM grouped),
    used AS (SELECT t, greatest(v - lag(v) OVER (ORDER BY t), 0) AS delta FROM filled)
    SELECT t, coalesce(delta, 0) FROM used WHERE t >= p_from
$$;

-- Active tariff per minute ('low' or 'normal'), from the raw states (30 days in ltss).
CREATE OR REPLACE FUNCTION solar_tariff(p_entity text, p_from timestamptz, p_to timestamptz)
RETURNS TABLE (minute timestamptz, tariff text)
LANGUAGE sql STABLE AS $$
    WITH m AS (
        SELECT generate_series(p_from, p_to - INTERVAL '1 minute', INTERVAL '1 minute') AS t
    ),
    c AS (
        SELECT time_bucket(INTERVAL '1 minute', time) AS bucket, last(state, time) AS state
        FROM ltss
        WHERE entity_id = p_entity AND time >= p_from AND time < p_to
          AND state IN ('low', 'normal')
        GROUP BY 1
    ),
    raw AS (
        SELECT m.t,
               CASE WHEN m.t = p_from AND c.state IS NULL
                    THEN (SELECT state FROM ltss
                          WHERE entity_id = p_entity AND time < p_from
                            AND state IN ('low', 'normal')
                          ORDER BY time DESC LIMIT 1)
                    ELSE c.state END AS s
        FROM m LEFT JOIN c ON c.bucket = m.t
    ),
    grouped AS (SELECT t, s, count(s) OVER (ORDER BY t) AS grp FROM raw)
    SELECT t, first_value(s) OVER (PARTITION BY grp ORDER BY t) FROM grouped
$$;

-- --- Per minute -------------------------------------------------------------------------
-- pv, self_use and use_total stay NULL until the inverter is read (phase 5); saving_self
-- likewise. Prices are those that held in that minute (solar_setting_ranges).
CREATE TABLE IF NOT EXISTS solar_1m (
    minute             timestamptz NOT NULL PRIMARY KEY,
    import_low         double precision NOT NULL,  -- kWh from the grid, tariff 1
    import_normal      double precision NOT NULL,  -- kWh from the grid, tariff 2
    export_low         double precision NOT NULL,  -- kWh to the grid, tariff 1
    export_normal      double precision NOT NULL,  -- kWh to the grid, tariff 2
    tariff             text,                       -- active tariff of the P1 meter
    pv                 double precision,           -- kWh generated
    self_use           double precision,           -- pv - export, >= 0
    use_total          double precision,           -- import + self_use
    day_ahead          double precision,           -- market price, EUR/kWh
    price_import       double precision,           -- contract, EUR/kWh, active tariff
    price_export_net   double precision,           -- feed-in compensation - feed-in cost
    cost_import        double precision,           -- EUR, contract
    revenue_export     double precision,           -- EUR, feed-in compensation
    cost_export        double precision,           -- EUR, feed-in cost per kWh
    saving_self        double precision,           -- EUR, self_use * price_import
    dyn_cost_import    double precision,           -- EUR, as if on a dynamic contract
    dyn_revenue_export double precision            -- EUR, as if on a dynamic contract
);
SELECT create_hypertable('solar_1m', by_range('minute', INTERVAL '7 days'), if_not_exists => TRUE);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_name = 'solar_1m' AND compression_enabled) THEN
        ALTER TABLE solar_1m SET (timescaledb.compress, timescaledb.compress_orderby = 'minute DESC');
    END IF;
END $$;
SELECT add_compression_policy('solar_1m', compress_after => INTERVAL '30 days', if_not_exists => TRUE);

-- --- Per day ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS solar_1d (
    day                date PRIMARY KEY,
    minutes            integer NOT NULL,
    pv_minutes         integer NOT NULL,          -- minutes with a known pv value
    import_low         double precision NOT NULL,
    import_normal      double precision NOT NULL,
    export_low         double precision NOT NULL,
    export_normal      double precision NOT NULL,
    pv                 double precision,
    self_use           double precision,
    use_total          double precision,
    cost_import        double precision,
    revenue_export     double precision,
    cost_export        double precision,
    cost_export_fixed  double precision,          -- feed-in cost per month, spread over its days
    saving_self        double precision,
    export_negative    double precision NOT NULL, -- kWh exported while day_ahead < 0
    revenue_negative   double precision,          -- EUR received for those kWh (contract)
    cmp_minutes        integer NOT NULL,          -- minutes with a day-ahead price
    cmp_cost_fixed     double precision,          -- net cost on the contract, those minutes
    cmp_cost_dynamic   double precision           -- net cost on a dynamic contract, same minutes
);

-- (Re)calculate solar_1m for the minutes in [p_from, p_to), never before the start, and
-- the days they touch in solar_1d. Returns the number of minutes written.
CREATE OR REPLACE FUNCTION solar_recompute(p_from timestamptz, p_to timestamptz)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_start timestamptz := solar_start();
    v_from  timestamptz := date_trunc('minute', greatest(p_from, v_start));
    v_to    timestamptz := date_trunc('minute', least(p_to, now()));
    n       integer;
BEGIN
    IF v_start IS NULL OR v_from IS NULL OR v_to IS NULL OR v_from >= v_to THEN
        RETURN 0;
    END IF;

    WITH r AS MATERIALIZED (SELECT * FROM solar_setting_ranges()),
    m AS (
        SELECT generate_series(v_from, v_to - INTERVAL '1 minute', INTERVAL '1 minute') AS t
    ),
    base AS (
        SELECT m.t,
               coalesce(il.delta, 0) AS import_low,
               coalesce(inr.delta, 0) AS import_normal,
               coalesce(el.delta, 0) AS export_low,
               coalesce(enr.delta, 0) AS export_normal,
               tf.tariff,
               (SELECT CASE WHEN p.end_time > m.t THEN p.price_eur_mwh / 1000 END
                FROM energy_price p
                WHERE p.area = 'NL' AND p.start <= m.t
                ORDER BY p.start DESC LIMIT 1) AS day_ahead,
               (SELECT value FROM r WHERE key = 'import_low' AND m.t >= valid_from AND m.t < valid_to) AS p_low,
               (SELECT value FROM r WHERE key = 'import_normal' AND m.t >= valid_from AND m.t < valid_to) AS p_normal,
               (SELECT value FROM r WHERE key = 'feed_in' AND m.t >= valid_from AND m.t < valid_to) AS p_feed_in,
               (SELECT value FROM r WHERE key = 'feed_in_cost' AND m.t >= valid_from AND m.t < valid_to) AS p_feed_in_cost,
               (SELECT value FROM r WHERE key = 'dyn_import_markup' AND m.t >= valid_from AND m.t < valid_to) AS dyn_in,
               (SELECT value FROM r WHERE key = 'dyn_feed_in_markup' AND m.t >= valid_from AND m.t < valid_to) AS dyn_out,
               (SELECT value FROM r WHERE key = 'energy_tax' AND m.t >= valid_from AND m.t < valid_to) AS tax,
               (SELECT value FROM r WHERE key = 'vat' AND m.t >= valid_from AND m.t < valid_to) AS vat
        FROM m
        LEFT JOIN solar_counter(solar_entity_id('import_low'), v_from, v_to) il ON il.minute = m.t
        LEFT JOIN solar_counter(solar_entity_id('import_normal'), v_from, v_to) inr ON inr.minute = m.t
        LEFT JOIN solar_counter(solar_entity_id('export_low'), v_from, v_to) el ON el.minute = m.t
        LEFT JOIN solar_counter(solar_entity_id('export_normal'), v_from, v_to) enr ON enr.minute = m.t
        LEFT JOIN solar_tariff(solar_entity_id('tariff'), v_from, v_to) tf ON tf.minute = m.t
    ),
    calc AS (
        SELECT b.*,
               b.import_low + b.import_normal AS import_kwh,
               b.export_low + b.export_normal AS export_kwh,
               CASE WHEN b.tariff = 'low' THEN b.p_low ELSE b.p_normal END AS price_import,
               coalesce(b.p_feed_in, 0) - coalesce(b.p_feed_in_cost, 0) AS price_export_net,
               1 + coalesce(b.vat, 21) / 100 AS vat_factor
        FROM base b
    )
    INSERT INTO solar_1m AS s (
        minute, import_low, import_normal, export_low, export_normal, tariff,
        pv, self_use, use_total, day_ahead, price_import, price_export_net,
        cost_import, revenue_export, cost_export, saving_self,
        dyn_cost_import, dyn_revenue_export)
    SELECT t, import_low, import_normal, export_low, export_normal, tariff,
           NULL, NULL, NULL, day_ahead, price_import, price_export_net,
           import_low * p_low + import_normal * p_normal,
           export_kwh * p_feed_in,
           export_kwh * p_feed_in_cost,
           NULL,
           import_kwh * (day_ahead + coalesce(dyn_in, 0) + coalesce(tax, 0)) * vat_factor,
           export_kwh * (day_ahead - coalesce(dyn_out, 0)) * vat_factor
    FROM calc
    ON CONFLICT (minute) DO UPDATE SET
        import_low = EXCLUDED.import_low, import_normal = EXCLUDED.import_normal,
        export_low = EXCLUDED.export_low, export_normal = EXCLUDED.export_normal,
        -- Raw tariff states are gone after 30 days: keep the stored one then.
        tariff = coalesce(EXCLUDED.tariff, s.tariff),
        day_ahead = EXCLUDED.day_ahead,
        price_import = CASE WHEN EXCLUDED.tariff IS NULL AND s.tariff IS NOT NULL
                            THEN s.price_import ELSE EXCLUDED.price_import END,
        price_export_net = EXCLUDED.price_export_net,
        cost_import = EXCLUDED.cost_import, revenue_export = EXCLUDED.revenue_export,
        cost_export = EXCLUDED.cost_export,
        dyn_cost_import = EXCLUDED.dyn_cost_import,
        dyn_revenue_export = EXCLUDED.dyn_revenue_export;
    GET DIAGNOSTICS n = ROW_COUNT;

    PERFORM solar_recompute_days((v_from AT TIME ZONE 'Europe/Amsterdam')::date,
                                 ((v_to - INTERVAL '1 minute') AT TIME ZONE 'Europe/Amsterdam')::date);
    RETURN n;
END $$;

-- Rebuild solar_1d for the local days p_first .. p_last from solar_1m.
CREATE OR REPLACE FUNCTION solar_recompute_days(p_first date, p_last date)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM solar_1d WHERE day BETWEEN p_first AND p_last;
    WITH r AS MATERIALIZED (SELECT * FROM solar_setting_ranges())
    INSERT INTO solar_1d
    SELECT d.day, d.minutes, d.pv_minutes, d.import_low, d.import_normal, d.export_low,
           d.export_normal, d.pv, d.self_use, d.use_total, d.cost_import, d.revenue_export,
           d.cost_export,
           (SELECT value FROM r
            WHERE key = 'feed_in_cost_month'
              AND (d.day::timestamp AT TIME ZONE 'Europe/Amsterdam') >= valid_from
              AND (d.day::timestamp AT TIME ZONE 'Europe/Amsterdam') < valid_to)
             / extract(day FROM date_trunc('month', d.day) + INTERVAL '1 month' - INTERVAL '1 day'),
           d.saving_self, d.export_negative, d.revenue_negative, d.cmp_minutes,
           d.cmp_cost_fixed, d.cmp_cost_dynamic
    FROM (
        SELECT (minute AT TIME ZONE 'Europe/Amsterdam')::date AS day,
               count(*) AS minutes,
               count(pv) AS pv_minutes,
               sum(import_low) AS import_low, sum(import_normal) AS import_normal,
               sum(export_low) AS export_low, sum(export_normal) AS export_normal,
               sum(pv) AS pv, sum(self_use) AS self_use, sum(use_total) AS use_total,
               sum(cost_import) AS cost_import, sum(revenue_export) AS revenue_export,
               sum(cost_export) AS cost_export, sum(saving_self) AS saving_self,
               coalesce(sum(export_low + export_normal) FILTER (WHERE day_ahead < 0), 0) AS export_negative,
               sum(revenue_export - cost_export) FILTER (WHERE day_ahead < 0) AS revenue_negative,
               count(day_ahead) AS cmp_minutes,
               sum(cost_import - revenue_export + cost_export) FILTER (WHERE day_ahead IS NOT NULL) AS cmp_cost_fixed,
               sum(dyn_cost_import - dyn_revenue_export) FILTER (WHERE day_ahead IS NOT NULL) AS cmp_cost_dynamic
        FROM solar_1m
        WHERE minute >= (p_first::timestamp AT TIME ZONE 'Europe/Amsterdam')
          AND minute < ((p_last + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam')
        GROUP BY 1
    ) d;
END $$;

-- --- Totals (doel 1, 2, 3) ----------------------------------------------------------------
-- Running totals since the start, for the SQL sensors (packages/solar.yaml). HA turns
-- them into long-term statistics, which give the sums per day, week, month and year.
CREATE OR REPLACE FUNCTION solar_totals()
RETURNS TABLE (
    self_use double precision, saving_self double precision,
    export double precision, revenue_export double precision, cost_export double precision,
    export_negative double precision, revenue_negative double precision,
    benefit double precision, cost_import double precision,
    cmp_cost_fixed double precision, cmp_cost_dynamic double precision)
LANGUAGE sql STABLE AS $$
    SELECT sum(self_use), sum(saving_self),
           sum(export_low + export_normal), sum(revenue_export),
           sum(cost_export) + coalesce(sum(cost_export_fixed), 0),
           sum(export_negative), coalesce(sum(revenue_negative), 0),
           coalesce(sum(saving_self), 0) + sum(revenue_export)
               - sum(cost_export) - coalesce(sum(cost_export_fixed), 0),
           sum(cost_import),
           sum(cmp_cost_fixed), sum(cmp_cost_dynamic)
    FROM solar_1d
    WHERE day >= (solar_start() AT TIME ZONE 'Europe/Amsterdam')::date
$$;

-- Self-use and self-sufficiency over the last 365 days, NULL without pv data.
CREATE OR REPLACE FUNCTION solar_self_use_ratio()
RETURNS TABLE (self_use_pct double precision, self_sufficiency_pct double precision, pv_days integer)
LANGUAGE sql STABLE AS $$
    SELECT 100 * sum(self_use) / nullif(sum(pv), 0),
           100 * sum(self_use) / nullif(sum(use_total), 0),
           count(*)::integer
    FROM solar_1d
    WHERE pv_minutes > 0 AND day > current_date - 365
$$;

-- --- Forecast (doel 5) ------------------------------------------------------------------
-- Cash flow per year, with year 0 the investment. Inputs are the current helper values:
-- the yield is measured once there is a year of pv data, otherwise the expected yield
-- (from the quote); the self-use share is measured after 30 days of pv data, otherwise
-- assumed. Prices grow per year; the panels degrade per year; the inverter is replaced
-- every inverter lifetime before the end.
CREATE OR REPLACE FUNCTION solar_forecast()
RETURNS TABLE (year integer, production double precision, cashflow double precision,
               cumulative double precision, discounted double precision)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_inv      double precision := coalesce(solar_setting_now('investment'), 0)
                                   + coalesce(solar_setting_now('extra_costs'), 0);
    v_yield    double precision;
    v_self     double precision;
    v_p_import double precision := coalesce(solar_setting_now('import_normal'), 0);
    v_p_export double precision := coalesce(solar_setting_now('feed_in'), 0)
                                   - coalesce(solar_setting_now('feed_in_cost'), 0);
    v_fixed    double precision := 12 * coalesce(solar_setting_now('feed_in_cost_month'), 0);
    v_g_import double precision := coalesce(solar_setting_now('growth_import'), 0) / 100;
    v_g_export double precision := coalesce(solar_setting_now('growth_feed_in'), 0) / 100;
    v_deg      double precision := coalesce(solar_setting_now('degradation'), 0) / 100;
    v_life     integer := coalesce(solar_setting_now('lifetime_panels'), 25)::integer;
    v_life_inv integer := coalesce(solar_setting_now('lifetime_inverter'), 0)::integer;
    v_repl     double precision := coalesce(solar_setting_now('inverter_replacement'), 0);
    v_rate     double precision := coalesce(solar_setting_now('discount_rate'), 0) / 100;
    v_cum      double precision;
    v_prod     double precision;
    v_cf       double precision;
    v_days     integer;
    v_ratio    double precision;
BEGIN
    SELECT sum(pv), count(*) INTO v_yield, v_days
    FROM solar_1d WHERE pv_minutes > 0 AND day > current_date - 365;
    IF v_days < 365 THEN
        v_yield := coalesce(solar_setting_now('expected_yield'), 0);
    END IF;
    SELECT r.self_use_pct, r.pv_days INTO v_ratio, v_days FROM solar_self_use_ratio() r;
    v_self := CASE WHEN v_days >= 30 AND v_ratio IS NOT NULL THEN v_ratio
                   ELSE coalesce(solar_setting_now('self_use_assumed'), 0) END / 100;

    year := 0; production := 0; cashflow := -v_inv; cumulative := -v_inv; discounted := -v_inv;
    v_cum := -v_inv;
    RETURN NEXT;
    FOR y IN 1 .. v_life LOOP
        v_prod := v_yield * power(1 - v_deg, y - 1);
        v_cf := v_prod * v_self * v_p_import * power(1 + v_g_import, y - 1)
              + v_prod * (1 - v_self) * v_p_export * power(1 + v_g_export, y - 1)
              - v_fixed;
        IF v_life_inv > 0 AND y % v_life_inv = 0 AND y < v_life THEN
            v_cf := v_cf - v_repl;
        END IF;
        v_cum := v_cum + v_cf;
        year := y; production := v_prod; cashflow := v_cf; cumulative := v_cum;
        discounted := v_cf / power(1 + v_rate, y);
        RETURN NEXT;
    END LOOP;
END $$;

-- Net present value at p_rate (fraction per year; default: the discount rate helper).
CREATE OR REPLACE FUNCTION solar_npv(p_rate double precision DEFAULT NULL)
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT sum(cashflow / power(1 + coalesce(p_rate, coalesce(solar_setting_now('discount_rate'), 0) / 100), year))
    FROM solar_forecast()
$$;

-- Internal rate of return (fraction per year) by bisection; NULL if there is no root
-- between -90% and +100%.
CREATE OR REPLACE FUNCTION solar_irr()
RETURNS double precision
LANGUAGE plpgsql STABLE AS $$
DECLARE
    lo double precision := -0.9;
    hi double precision := 1.0;
    mid double precision;
    f_lo double precision;
    f_mid double precision;
    flows double precision[];
BEGIN
    SELECT array_agg(cashflow ORDER BY year) INTO flows FROM solar_forecast();
    f_lo := solar_irr_npv(flows, lo);
    IF f_lo * solar_irr_npv(flows, hi) > 0 THEN
        RETURN NULL;
    END IF;
    FOR i IN 1 .. 100 LOOP
        mid := (lo + hi) / 2;
        f_mid := solar_irr_npv(flows, mid);
        IF abs(f_mid) < 1e-6 THEN EXIT; END IF;
        IF f_lo * f_mid < 0 THEN hi := mid; ELSE lo := mid; f_lo := f_mid; END IF;
    END LOOP;
    RETURN mid;
END $$;

CREATE OR REPLACE FUNCTION solar_irr_npv(flows double precision[], rate double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
    SELECT sum(f / power(1 + rate, i - 1)) FROM unnest(flows) WITH ORDINALITY AS u(f, i)
$$;

-- Payback time in years from the forecast (interpolated within the year), NULL if the
-- investment is not earned back within the lifetime of the panels.
CREATE OR REPLACE FUNCTION solar_payback_years()
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT (f.year - 1) + (-prev.cumulative) / nullif(f.cashflow, 0)
    FROM solar_forecast() f
    JOIN solar_forecast() prev ON prev.year = f.year - 1
    WHERE prev.cumulative < 0 AND f.cumulative >= 0
    ORDER BY f.year LIMIT 1
$$;

-- --- Investment so far (doel 3) ---------------------------------------------------------
-- invested: investment + extra costs. earned: net benefit since the start (solar_totals).
-- break_even: the day the cumulative benefit reached the investment; before that, an
-- estimate from the forecast (start + payback years). estimated says which one it is.
CREATE OR REPLACE FUNCTION solar_payback()
RETURNS TABLE (invested double precision, earned double precision, ratio_pct double precision,
               break_even date, estimated boolean)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_years double precision;
BEGIN
    invested := coalesce(solar_setting_now('investment'), 0) + coalesce(solar_setting_now('extra_costs'), 0);
    SELECT t.benefit INTO earned FROM solar_totals() t;
    earned := coalesce(earned, 0);
    ratio_pct := 100 * earned / nullif(invested, 0);
    SELECT c.day INTO break_even
    FROM (SELECT day, sum(coalesce(saving_self, 0) + coalesce(revenue_export, 0)
                          - coalesce(cost_export, 0) - coalesce(cost_export_fixed, 0))
                      OVER (ORDER BY day) AS cum
          FROM solar_1d
          WHERE day >= (solar_start() AT TIME ZONE 'Europe/Amsterdam')::date) c
    WHERE invested > 0 AND c.cum >= invested
    ORDER BY c.day LIMIT 1;
    estimated := break_even IS NULL;
    IF estimated THEN
        v_years := solar_payback_years();
        IF v_years IS NOT NULL THEN
            break_even := ((solar_start() AT TIME ZONE 'Europe/Amsterdam')
                           + make_interval(days => round(v_years * 365.25)::integer))::date;
        END IF;
    END IF;
    RETURN NEXT;
END $$;

-- --- Battery simulation and surplus (doel 4) --------------------------------------------
-- A battery that charges from what would go to the grid and discharges instead of buying,
-- minute by minute over [p_from, p_to). Efficiency is the round trip, split evenly over
-- charging and discharging. saving = avoided purchase - feed-in compensation given up.
CREATE OR REPLACE FUNCTION solar_simulate_battery(
    p_capacity double precision, p_power double precision, p_efficiency double precision,
    p_from timestamptz, p_to timestamptz)
RETURNS TABLE (saving double precision, charged double precision, discharged double precision,
               cycles double precision, days double precision)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    eta   double precision := sqrt(greatest(least(p_efficiency, 1), 0.01));
    step  double precision := p_power / 60;   -- kWh per minute
    soc   double precision := 0;
    c     double precision;
    d     double precision;
    rec   record;
BEGIN
    saving := 0; charged := 0; discharged := 0;
    SELECT count(*) / 1440.0 INTO days FROM solar_1m WHERE minute >= p_from AND minute < p_to;
    FOR rec IN
        SELECT export_low + export_normal AS export_kwh, import_low + import_normal AS import_kwh,
               coalesce(price_import, 0) AS p_in, coalesce(price_export_net, 0) AS p_out
        FROM solar_1m WHERE minute >= p_from AND minute < p_to ORDER BY minute
    LOOP
        c := least(rec.export_kwh, step, (p_capacity - soc) / eta);
        soc := soc + c * eta;
        d := least(rec.import_kwh, step, soc * eta);
        soc := soc - d / eta;
        charged := charged + c;
        discharged := discharged + d;
        saving := saving + d * rec.p_in - c * rec.p_out;
    END LOOP;
    cycles := charged / nullif(p_capacity, 0);
    RETURN NEXT;
END $$;

-- Results that are too heavy to compute on every poll of a SQL sensor (every 30 s).
CREATE TABLE IF NOT EXISTS solar_result (
    key     text PRIMARY KEY,
    value   double precision,
    detail  text,
    updated timestamptz NOT NULL DEFAULT now()
);

-- Fills solar_result: the battery simulation over the last 365 days (per year), and the
-- average surplus and purchase per hour of the day over the last 30 days (markdown; kWh
-- per hour, averaged over the days with data).
CREATE OR REPLACE FUNCTION solar_results() RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_from timestamptz := greatest(now() - INTERVAL '365 days', solar_start());
    b      record;
    v_table text;
BEGIN
    IF v_from IS NULL THEN
        RETURN;
    END IF;
    SELECT * INTO b FROM solar_simulate_battery(
        coalesce(solar_setting_now('battery_capacity'), 0),
        coalesce(solar_setting_now('battery_power'), 0),
        coalesce(solar_setting_now('battery_efficiency'), 90) / 100,
        v_from, now());
    INSERT INTO solar_result (key, value, detail, updated)
    VALUES ('battery_saving_per_year',
            CASE WHEN b.days > 0 THEN b.saving * 365 / b.days END,
            format('Over %s dagen: besparing %s EUR, geladen %s kWh, ontladen %s kWh, %s cycli.',
                   round(b.days::numeric, 1), round(b.saving::numeric, 2),
                   round(b.charged::numeric, 1), round(b.discharged::numeric, 1),
                   round(coalesce(b.cycles, 0)::numeric, 1)),
            now())
    ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, detail = EXCLUDED.detail, updated = EXCLUDED.updated;

    SELECT string_agg(format('| %s:00 | %s | %s | %s |', lpad(h::text, 2, '0'),
                             to_char(coalesce(e, 0), 'FM0.000'), to_char(coalesce(i, 0), 'FM0.000'),
                             coalesce(to_char(p, 'FM0.000'), '–')),
                      E'\n' ORDER BY h)
    INTO v_table
    FROM (
        SELECT extract(hour FROM minute AT TIME ZONE 'Europe/Amsterdam')::integer AS h,
               sum(export_low + export_normal) / (count(*) / 60.0) AS e,
               sum(import_low + import_normal) / (count(*) / 60.0) AS i,
               avg(day_ahead) AS p
        FROM solar_1m
        WHERE minute >= greatest(now() - INTERVAL '30 days', solar_start())
        GROUP BY 1
    ) hours;
    INSERT INTO solar_result (key, value, detail, updated)
    VALUES ('surplus_per_hour', NULL,
            E'| Uur | Overschot (kWh) | Inkoop (kWh) | Day-ahead (€/kWh) |\n|---|---|---|---|\n'
                || coalesce(v_table, ''),
            now())
    ON CONFLICT (key) DO UPDATE
        SET detail = EXCLUDED.detail, updated = EXCLUDED.updated;
END $$;

-- --- Day-ahead prices still missing -------------------------------------------------------
-- Local days in the last 31 days (and tomorrow, once Nord Pool has published it around
-- 13:00) without a full set of prices. The automation in packages/solar_prices.yaml fetches
-- them; Nord Pool answers with 15-minute prices.
CREATE OR REPLACE FUNCTION solar_price_missing_dates(p_area text)
RETURNS SETOF date
LANGUAGE sql STABLE AS $$
    WITH days AS (
        SELECT d::date AS day
        FROM generate_series((now() AT TIME ZONE 'Europe/Amsterdam')::date - 31,
                             (now() AT TIME ZONE 'Europe/Amsterdam')::date
                             + CASE WHEN (now() AT TIME ZONE 'Europe/Amsterdam')::time >= TIME '13:30'
                                    THEN 1 ELSE 0 END,
                             INTERVAL '1 day') AS d
    )
    SELECT day FROM days
    WHERE (SELECT coalesce(sum(extract(epoch FROM least(p.end_time, de.e) - greatest(p.start, de.s))), 0)
           FROM (SELECT day::timestamp AT TIME ZONE 'Europe/Amsterdam' AS s,
                        (day + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam' AS e) de,
                energy_price p
           WHERE p.area = p_area AND p.start < de.e AND p.end_time > de.s)
          < extract(epoch FROM ((day + 1)::timestamp AT TIME ZONE 'Europe/Amsterdam')
                              - (day::timestamp AT TIME ZONE 'Europe/Amsterdam'))
    ORDER BY day
$$;

-- --- Jobs -------------------------------------------------------------------------------
-- Every 5 minutes: the minutes since the last run, and always the last 6 hours, so late
-- states (LTSS, ltss_1m refresh) and new prices are counted.
CREATE OR REPLACE PROCEDURE solar_job(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
DECLARE
    v_last timestamptz;
BEGIN
    SELECT max(minute) INTO v_last FROM solar_1m;
    PERFORM solar_recompute(CASE WHEN v_last IS NULL THEN solar_start()
                                 ELSE least(now() - INTERVAL '6 hours', v_last + INTERVAL '1 minute') END,
                            now());
END $$;

CREATE OR REPLACE PROCEDURE solar_results_job(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM solar_results();
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'solar_job') THEN
        PERFORM add_job('solar_job', INTERVAL '5 minutes');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'solar_results_job') THEN
        PERFORM add_job('solar_results_job', INTERVAL '1 hour');
    END IF;
END $$;
