-- 002_timeseries.sql - warm telemetry tier (TimescaleDB).
-- Partitioned by time (6 h chunks) and by hash(vin) (4 space partitions) so parallel writers
-- never contend on one chunk and per-vehicle scans prune to one space partition.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE telemetry (
    ts           TIMESTAMPTZ      NOT NULL,
    vin          CHAR(17)         NOT NULL,
    tenant_id    INT              NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed_kmh    REAL NOT NULL,
    soc_pct      REAL NOT NULL,
    soh_pct      REAL NOT NULL,
    batt_temp_c  REAL NOT NULL,
    odo_km       DOUBLE PRECISION NOT NULL,
    power_kw     REAL NOT NULL,
    evt          TEXT NOT NULL,
    PRIMARY KEY (vin, ts)          -- natural idempotency key: replays upsert-nothing
);
SELECT create_hypertable('telemetry', by_range('ts', INTERVAL '6 hours'));
SELECT add_dimension('telemetry', by_hash('vin', 4));

ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'vin',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('telemetry', INTERVAL '1 day');
SELECT add_retention_policy('telemetry', INTERVAL '30 days');   -- older data lives in the Parquet lake

-- 15-minute rollup, refreshed continuously: powers dashboards without scanning raw rows.
CREATE MATERIALIZED VIEW telemetry_15m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '15 minutes', ts) AS bucket,
       vin,
       tenant_id,
       avg(soc_pct)      AS avg_soc,
       min(soc_pct)      AS min_soc,
       max(soc_pct)      AS max_soc,
       avg(speed_kmh)    AS avg_speed,
       max(batt_temp_c)  AS max_temp,
       last(soh_pct, ts) AS soh,
       max(odo_km) - min(odo_km) AS km,
       count(*)          AS samples
FROM telemetry
GROUP BY bucket, vin, tenant_id
WITH NO DATA;
SELECT add_continuous_aggregate_policy('telemetry_15m',
    start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');

-- Daily battery/energy history per vehicle (produced by the batch layer from the lake).
CREATE TABLE battery_daily (
    day          DATE     NOT NULL,
    vin          CHAR(17) NOT NULL,
    tenant_id    INT      NOT NULL,
    soh_pct      REAL NOT NULL,
    km           REAL NOT NULL,
    energy_kwh   REAL NOT NULL,
    charge_kwh   REAL NOT NULL,
    dc_fast_kwh  REAL NOT NULL,
    max_temp_c   REAL NOT NULL,
    charge_cost  REAL NOT NULL,
    PRIMARY KEY (vin, day)
);
SELECT create_hypertable('battery_daily', by_range('day', INTERVAL '30 days'));
CREATE INDEX battery_daily_tenant_day ON battery_daily(tenant_id, day DESC);

-- Batch outputs.
CREATE TABLE soh_forecast (
    vin              CHAR(17) PRIMARY KEY REFERENCES vehicle(vin) ON DELETE CASCADE,
    tenant_id        INT  NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    soh_now          REAL NOT NULL,
    slope_per_year   REAL NOT NULL,      -- SoH percentage points lost per year
    days_to_80       INT,                -- NULL = not within 10 years
    anomaly_score    REAL NOT NULL,      -- robust z-score of slope within model cohort
    dc_fast_share    REAL NOT NULL
);
CREATE INDEX soh_forecast_tenant_risk ON soh_forecast(tenant_id, days_to_80 NULLS LAST);

CREATE TABLE fleet_energy_daily (
    day          DATE NOT NULL,
    tenant_id    INT  NOT NULL,
    fleet_id     INT  NOT NULL REFERENCES fleet(id) ON DELETE CASCADE,
    vehicles     INT  NOT NULL,
    km           REAL NOT NULL,
    energy_kwh   REAL NOT NULL,
    charge_cost  REAL NOT NULL,
    PRIMARY KEY (fleet_id, day)
);
