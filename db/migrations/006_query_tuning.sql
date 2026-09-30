-- 006_query_tuning.sql - indexes chosen from EXPLAIN ANALYZE of the slowest API queries
-- (evidence: db/explain/REPORT.md, regenerate with `make explain`).

-- Open-alert feed per tenant, newest first (the Alerts panel + WebSocket backfill).
-- Partial: resolved/acked alerts (the vast majority over time) are not indexed at all.
CREATE INDEX IF NOT EXISTS alert_open_feed ON alert (tenant_id, id DESC) WHERE status = 'OPEN';

-- "Abnormal degradation" list: only a few % of packs qualify, so a partial index stays tiny.
CREATE INDEX IF NOT EXISTS soh_forecast_anomalies ON soh_forecast (tenant_id, anomaly_score DESC)
    WHERE anomaly_score > 3.5;

-- Current driver of a vehicle (vehicle detail): only open assignments matter.
CREATE INDEX IF NOT EXISTS driver_assignment_current ON driver_assignment (vin) WHERE to_ts IS NULL;

-- Per-tenant fleet energy trend over battery_daily (12M rows): covering index for the rollup.
CREATE INDEX IF NOT EXISTS battery_daily_tenant_day_cover ON battery_daily (tenant_id, day)
    INCLUDE (km, energy_kwh, charge_cost, soh_pct);
