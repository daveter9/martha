-- Long-term storage of Home Assistant states in TimescaleDB (ADR-001).
--
-- Applied by install.sh on every run, before Home Assistant starts, so every statement
-- must be idempotent. Policies are created with if_not_exists: changing an interval
-- here does not alter an existing policy (remove it first, or alter it by hand).
-- Changing a continuous aggregate's query means dropping and recreating that view.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- --- Raw states (30 days) -----------------------------------------------------------
-- The table LTSS writes to. Created here, with exactly the columns and index names of
-- LTSS v2 (custom_components/ltss/models.py), so the aggregates below can be defined
-- before LTSS first connects; LTSS then finds the table and leaves it alone.
CREATE TABLE IF NOT EXISTS ltss (
    time       timestamptz  NOT NULL,
    entity_id  varchar(255) NOT NULL,
    state      varchar(255),
    attributes jsonb,
    PRIMARY KEY (time, entity_id)
);
-- One-day chunks, so the retention policy drops whole days. LTSS resets the interval
-- to its chunk_time_interval setting on every start; packages/martha_storage.yaml sets
-- the same value.
SELECT create_hypertable('ltss', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ltss_attributes_idx ON ltss USING gin (attributes);
CREATE INDEX IF NOT EXISTS ltss_entityid_time_composite_idx ON ltss (entity_id, time DESC);

-- LTSS has no ignore_attributes option, so attributes that are only UI decoration are
-- dropped here, before the row is stored.
CREATE OR REPLACE FUNCTION ltss_strip_attributes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.attributes := NEW.attributes - ARRAY[
        'icon', 'entity_picture', 'attribution', 'supported_features',
        'options', 'editable', 'assumed_state', 'restored'
    ];
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ltss_strip_attributes ON ltss;
CREATE TRIGGER ltss_strip_attributes BEFORE INSERT ON ltss
    FOR EACH ROW EXECUTE FUNCTION ltss_strip_attributes();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_name = 'ltss' AND compression_enabled) THEN
        ALTER TABLE ltss SET (timescaledb.compress,
                              timescaledb.compress_segmentby = 'entity_id',
                              timescaledb.compress_orderby = 'time DESC');
    END IF;
END $$;
SELECT add_compression_policy('ltss', compress_after => INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('ltss', drop_after => INTERVAL '30 days', if_not_exists => TRUE);

-- --- Numeric states ---------------------------------------------------------------------
-- HA stores every state as text ('21.5', 'on', 'unavailable'). Returns the number, or
-- NULL for anything else. The digit limits keep the cast within double precision, so a
-- strange state can never make a refresh fail.
CREATE OR REPLACE FUNCTION ltss_numeric(state text) RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE WHEN state ~ '^[-+]?[0-9]{1,18}(\.[0-9]{1,18})?([eE][-+]?[0-9]{1,2})?$'
                THEN state::double precision END
$$;

-- --- Per minute (kept forever) --------------------------------------------------------
-- Only numeric states. Use value_last for counters (state_class total/total_increasing,
-- e.g. kWh) and value_avg for momentary values (state_class measurement, e.g. W, °C).
-- The average is over samples: HA writes only on change, so it is not time-weighted.
CREATE MATERIALIZED VIEW IF NOT EXISTS ltss_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', time) AS bucket,
       entity_id,
       avg(ltss_numeric(state))                     AS value_avg,
       min(ltss_numeric(state))                     AS value_min,
       max(ltss_numeric(state))                     AS value_max,
       last(ltss_numeric(state), time)              AS value_last,
       count(*)                                     AS samples,
       last(attributes ->> 'state_class', time)         AS state_class,
       last(attributes ->> 'unit_of_measurement', time) AS unit
FROM ltss
WHERE ltss_numeric(state) IS NOT NULL
GROUP BY bucket, entity_id
WITH NO DATA;

-- The refresh window stays well within the 30 days of raw data, so dropping raw
-- chunks never erases minutes that were already aggregated.
SELECT add_continuous_aggregate_policy('ltss_1m',
    start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates
                   WHERE view_name = 'ltss_1m' AND compression_enabled) THEN
        ALTER MATERIALIZED VIEW ltss_1m SET (timescaledb.compress = true);
    END IF;
END $$;
SELECT add_compression_policy('ltss_1m', compress_after => INTERVAL '30 days', if_not_exists => TRUE);
-- No retention: every minute is kept forever (choice of the user, 2026-10-08). Earlier
-- installs had a 2-year policy; it is removed here.
SELECT remove_retention_policy('ltss_1m', if_exists => TRUE);

-- --- Per hour (kept forever) ------------------------------------------------------------
-- Built on ltss_1m. value_avg is weighted by samples, so it equals the average over
-- all raw samples in the hour.
CREATE MATERIALIZED VIEW IF NOT EXISTS ltss_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
       entity_id,
       sum(value_avg * samples) / sum(samples) AS value_avg,
       min(value_min)                          AS value_min,
       max(value_max)                          AS value_max,
       last(value_last, bucket)                AS value_last,
       sum(samples)                            AS samples,
       last(state_class, bucket)               AS state_class,
       last(unit, bucket)                      AS unit
FROM ltss_1m
GROUP BY 1, entity_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ltss_1h',
    start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes', if_not_exists => TRUE);
