"""Batch layer jobs.

history      : 120 days of per-vehicle daily battery/energy history (12M rows) -> Parquet lake +
               battery_daily hypertable. Generated once from the simulator's hidden ground truth.
rollup       : live lake Parquet (today) -> per-vehicle daily rows (DuckDB over Arrow datasets).
forecast     : DuckDB regression over the lake history -> soh_forecast (slope, days to 80 %,
               robust cohort z-score) + 16-d degradation embeddings (pgvector) + RAPID_SOH_DROP alerts.
energy       : fleet_energy_daily rollup for the analytics API.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import psycopg

from voltcore.energy import MODELS_BY_CODE, speed_factor, temp_factor
from voltpath.config import settings
from voltpath.fleetgen import TODAY, generate
from voltpath.infra import s3fs

log = logging.getLogger("batch")
HISTORY_DAYS = 120
HIST_SCHEMA = pa.schema([("day", pa.date32()), ("vin", pa.string()), ("tenant_id", pa.int32()),
                         ("soh_pct", pa.float32()), ("km", pa.float32()), ("energy_kwh", pa.float32()),
                         ("charge_kwh", pa.float32()), ("dc_fast_kwh", pa.float32()), ("max_temp_c", pa.float32()),
                         ("charge_cost", pa.float32())])


def _duck() -> duckdb.DuckDBPyConnection:
    """Bounded DuckDB: spills to disk instead of growing past the container's memory budget."""
    con = _duck()
    con.execute("SET memory_limit = '1500MB'")
    con.execute("SET threads = 4")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    return con


def _pg():
    return psycopg.connect(settings().database_url)


# ---------------------------------------------------------------------------- history
def history(force: bool = False) -> int:
    s = settings()
    with _pg() as c:
        if not force and c.execute("SELECT EXISTS (SELECT 1 FROM battery_daily)").fetchone()[0]:
            log.info("history already present - skipping")
            return 0
    w = generate(s.seed, s.n_vehicles)
    rng = np.random.default_rng(s.seed + 7)
    n = len(w.vin)
    vins = np.array(w.vin)
    base_whkm = np.array([MODELS_BY_CODE[m].base_wh_per_km for m in w.model_code])
    fs = s3fs()
    t0 = time.time()
    total = 0
    tables = []
    for d in range(HISTORY_DAYS, 0, -1):
        day = TODAY - timedelta(days=d)
        years_back = d / 365.25
        soh = w.soh0 + w.degr_per_year * years_back + rng.normal(0, 0.25, n)
        weekday = 0.75 if day.weekday() >= 5 else 1.0
        active = rng.random(n) > 0.03
        km = np.where(active, w.daily_km * weekday * rng.gamma(9, 1 / 9, n), 0.0)
        season = 26 + 3 * np.sin((day.timetuple().tm_yday - 100) / 365 * 2 * np.pi)
        whkm = base_whkm * speed_factor(28) * temp_factor(season) * rng.normal(1.0, 0.05, n)
        energy = km * whkm / 1000
        charge = energy / 0.92 * rng.normal(1.0, 0.08, n).clip(0.7, 1.3)
        dc_share = np.clip(w.dc_share + rng.normal(0, 0.08, n), 0, 1)
        dc = charge * dc_share
        max_temp = season + 6 + 14 * dc_share + rng.normal(0, 1.5, n)
        max_temp = np.where(w.defect & (rng.random(n) < 0.08), rng.uniform(56, 63, n), max_temp)
        cost = (charge - dc) * rng.uniform(6.5, 9.0, n) + dc * rng.uniform(17, 22, n)
        tables.append(pa.table({
            "day": pa.array([day] * n, pa.date32()), "vin": vins, "tenant_id": w.tenant_id,
            "soh_pct": np.clip(soh, 55, 100).astype(np.float32), "km": km.astype(np.float32),
            "energy_kwh": energy.astype(np.float32), "charge_kwh": charge.astype(np.float32),
            "dc_fast_kwh": dc.astype(np.float32), "max_temp_c": max_temp.astype(np.float32),
            "charge_cost": cost.astype(np.float32)}, schema=HIST_SCHEMA))
        if len(tables) == 30 or d == 1:
            batch = pa.concat_tables(tables)
            month = tables[0]["day"][0].as_py().strftime("%Y-%m-%d")
            pq.write_table(batch, f"{s.lake_bucket}/history/battery_daily/start={month}.parquet", filesystem=fs,
                           compression="zstd")
            _copy_battery_daily(batch)
            total += batch.num_rows
            log.info("history: %d rows so far (%.0fs)", total, time.time() - t0)
            tables = []
    return total


def _copy_battery_daily(t: pa.Table) -> None:
    cols = HIST_SCHEMA.names
    with _pg() as c, c.cursor() as cur:
        with cur.copy(f"COPY battery_daily ({', '.join(cols)}) FROM STDIN") as cp:
            arrays = [t[col].to_pylist() for col in cols]
            for row in zip(*arrays, strict=True):
                cp.write_row(row)
        c.commit()


# ---------------------------------------------------------------------------- lake rollup
def rollup(day: date | None = None) -> int:
    """Today's full-fidelity lake data -> battery_daily rows (upsert)."""
    s = settings()
    day = day or datetime.now(UTC).date()
    path = f"{s.lake_bucket}/telemetry/dt={day.isoformat()}"
    fs = s3fs()
    if fs.get_file_info(path).type.name == "NotFound":
        log.info("rollup: no lake data for %s", day)
        return 0
    dataset = ds.dataset(path, filesystem=fs, format="parquet", partitioning="hive")
    con = _duck()
    con.register("t", dataset)
    rows = con.execute("""
        WITH d AS (
            SELECT DISTINCT ON (vin, seq) vin, ts, soh_pct, odo_km, power_kw, batt_temp_c FROM t
        ), w AS (
            SELECT *, least(epoch(lead(ts) OVER (PARTITION BY vin ORDER BY ts)) - epoch(ts), 60) AS dt FROM d
        )
        SELECT vin, last(soh_pct ORDER BY ts) AS soh, max(odo_km) - min(odo_km) AS km,
               sum(CASE WHEN power_kw < 0 THEN -power_kw * coalesce(dt, 1) ELSE 0 END) / 3600 AS energy,
               sum(CASE WHEN power_kw > 0 THEN power_kw * coalesce(dt, 1) ELSE 0 END) / 3600 AS charge,
               sum(CASE WHEN power_kw > 25 THEN power_kw * coalesce(dt, 1) ELSE 0 END) / 3600 AS dc,
               max(batt_temp_c) AS max_temp
        FROM w GROUP BY vin
    """).fetchall()
    w = generate(s.seed, s.n_vehicles)
    tenant = dict(zip(w.vin, w.tenant_id.tolist(), strict=True))
    with _pg() as c, c.cursor() as cur:
        cur.execute("CREATE TEMP TABLE bd (LIKE battery_daily) ON COMMIT DROP")
        with cur.copy("COPY bd (day, vin, tenant_id, soh_pct, km, energy_kwh, charge_kwh, dc_fast_kwh, max_temp_c, "
                      "charge_cost) FROM STDIN") as cp:
            for vin, soh, km, e, ch, dc, mt in rows:
                cp.write_row((day, vin, tenant.get(vin, 1), soh, km or 0, e or 0, ch or 0, dc or 0, mt or 0,
                              (ch - dc) * 7.5 + dc * 19.5 if ch else 0))
        cur.execute("INSERT INTO battery_daily SELECT * FROM bd ON CONFLICT (vin, day) DO UPDATE SET "
                    "soh_pct = EXCLUDED.soh_pct, km = EXCLUDED.km, energy_kwh = EXCLUDED.energy_kwh, "
                    "charge_kwh = EXCLUDED.charge_kwh, dc_fast_kwh = EXCLUDED.dc_fast_kwh, "
                    "max_temp_c = EXCLUDED.max_temp_c, charge_cost = EXCLUDED.charge_cost")
        c.commit()
    log.info("rollup %s: %d vehicles from the lake", day, len(rows))
    return len(rows)


# ---------------------------------------------------------------------------- forecasting
def forecast() -> int:
    """Per-vehicle SoH regression + cohort anomaly score + degradation embeddings."""
    s = settings()
    fs = s3fs()
    dataset = ds.dataset(f"{s.lake_bucket}/history/battery_daily", filesystem=fs, format="parquet")
    con = _duck()
    con.register("h", dataset)
    w = generate(s.seed, s.n_vehicles)
    con.register("models", pa.table({"vin": w.vin, "model_code": w.model_code}))
    t0 = time.time()
    res = con.execute("""
        WITH per AS (
            SELECT h.vin, any_value(h.tenant_id) AS tenant_id, any_value(m.model_code) AS model_code,
                   regr_slope(h.soh_pct, epoch(h.day) / 86400.0) * 365.25 AS slope_year,
                   regr_intercept(h.soh_pct, epoch(h.day) / 86400.0) + regr_slope(h.soh_pct, epoch(h.day) / 86400.0)
                       * max(epoch(h.day) / 86400.0) AS soh_now,
                   sum(h.dc_fast_kwh) / nullif(sum(h.charge_kwh), 0) AS dc_share,
                   avg(h.km) AS km_day, avg(h.max_temp_c) AS temp,
                   sum(h.energy_kwh) / nullif(sum(h.km), 0) * 1000 AS whkm,
                   list(h.soh_pct ORDER BY h.day) AS curve
            FROM h JOIN models m USING (vin) GROUP BY h.vin
        ), coh AS (
            SELECT model_code, median(slope_year) AS med FROM per GROUP BY model_code
        ), mad AS (
            SELECT p.model_code, median(abs(p.slope_year - c.med)) AS mad
            FROM per p JOIN coh c USING (model_code) GROUP BY p.model_code
        )
        SELECT p.vin, p.tenant_id, p.soh_now, -p.slope_year AS loss_year,
               (c.med - p.slope_year) / nullif(1.4826 * m.mad, 0) AS z,
               p.dc_share, p.km_day, p.temp, p.whkm, p.curve
        FROM per p JOIN coh c USING (model_code) JOIN mad m USING (model_code)
    """).fetchall()
    log.info("forecast: regression over %d vehicles in %.1fs", len(res), time.time() - t0)
    with _pg() as c, c.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sf (LIKE soh_forecast INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE be (LIKE battery_embedding INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy("COPY sf (vin, tenant_id, soh_now, slope_per_year, days_to_80, anomaly_score, dc_fast_share) "
                      "FROM STDIN") as cp:
            for vin, tid, soh_now, loss, z, dc, *_ in res:
                days = int((soh_now - 80) / loss * 365.25) if loss and loss > 0.05 and soh_now > 80 else (
                    0 if soh_now <= 80 else None)
                if days is not None and days > 3650:
                    days = None
                cp.write_row((vin, tid, round(soh_now, 2), round(loss, 3), days, round(z or 0, 2), round(dc or 0, 3)))
        with cur.copy("COPY be (vin, tenant_id, embedding) FROM STDIN") as cp:
            for vin, tid, _soh, _loss, _z, dc, km, temp, whkm, curve in res:
                cp.write_row((vin, tid, _embedding(curve, dc, km, temp, whkm)))
        cur.execute("INSERT INTO soh_forecast SELECT * FROM sf ON CONFLICT (vin) DO UPDATE SET "
                    "computed_at = now(), soh_now = EXCLUDED.soh_now, slope_per_year = EXCLUDED.slope_per_year, "
                    "days_to_80 = EXCLUDED.days_to_80, anomaly_score = EXCLUDED.anomaly_score, "
                    "dc_fast_share = EXCLUDED.dc_fast_share")
        cur.execute("INSERT INTO battery_embedding SELECT * FROM be ON CONFLICT (vin) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, updated_at = now()")
        # RAPID_SOH_DROP alerts for strong anomalies without an open alert already
        cur.execute("""
            INSERT INTO alert(tenant_id, vin, type_code, message, event_ts, details)
            SELECT f.tenant_id, f.vin, 'RAPID_SOH_DROP',
                   format('Battery losing %s %%/year (cohort z-score %s); SoH now %s %%',
                          round(f.slope_per_year::numeric, 1), round(f.anomaly_score::numeric, 1),
                          round(f.soh_now::numeric, 1)),
                   now(), jsonb_build_object('slope_per_year', f.slope_per_year, 'z', f.anomaly_score,
                                             'days_to_80', f.days_to_80)
            FROM soh_forecast f
            WHERE f.anomaly_score > 4 AND f.slope_per_year > 6
              AND NOT EXISTS (SELECT 1 FROM alert a WHERE a.vin = f.vin AND a.type_code = 'RAPID_SOH_DROP'
                              AND a.status = 'OPEN')
        """)
        n_alerts = cur.rowcount
        c.commit()
    log.info("forecast written (%d vehicles, %d new RAPID_SOH_DROP alerts)", len(res), n_alerts)
    return len(res)


def forecast_is_fresh(max_age_h: float = 6) -> bool:
    with _pg() as c:
        row = c.execute("SELECT max(computed_at) > now() - make_interval(hours => %s) FROM soh_forecast",
                        (max_age_h,)).fetchone()
    return bool(row and row[0])


def _embedding(curve, dc, km, temp, whkm) -> str:
    """16-d: 12-point normalised SoH trajectory (shape of degradation) + 4 usage features."""
    c = np.asarray(curve, dtype=np.float64)
    pts = np.interp(np.linspace(0, len(c) - 1, 12), np.arange(len(c)), c)
    kernel = np.ones(7) / 7
    smooth = np.convolve(np.pad(c, 3, mode="edge"), kernel, "valid")
    pts = np.interp(np.linspace(0, len(smooth) - 1, 12), np.arange(len(smooth)), smooth)
    traj = (pts - pts[0]) / 2.0   # percentage points lost since start, scaled
    usage = np.array([(dc or 0) * 2, (km or 0) / 150, ((temp or 30) - 30) / 10, ((whkm or 150) - 150) / 60])
    v = np.concatenate([traj, usage])
    n = np.linalg.norm(v)
    v = v / n if n else v
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def energy_rollup() -> int:
    with _pg() as c:
        c.execute("TRUNCATE fleet_energy_daily")
        cur = c.execute("""
            INSERT INTO fleet_energy_daily(day, tenant_id, fleet_id, vehicles, km, energy_kwh, charge_cost)
            SELECT b.day, v.tenant_id, v.fleet_id, count(*), sum(b.km), sum(b.energy_kwh), sum(b.charge_cost)
            FROM battery_daily b JOIN vehicle v ON v.vin = b.vin
            GROUP BY b.day, v.tenant_id, v.fleet_id
        """)
        c.commit()
        return cur.rowcount
