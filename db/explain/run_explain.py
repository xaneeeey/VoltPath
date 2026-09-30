"""EXPLAIN (ANALYZE, BUFFERS) before/after for the slowest queries -> db/explain/REPORT.md.

"Before" plans are captured by dropping the tuned index *inside a transaction* and rolling back
(PostgreSQL DDL is transactional), so the database is never actually modified.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from voltpath.config import settings  # noqa: E402

OUT = Path(__file__).parent / "REPORT.md"


def plan(cur, sql, params=()):
    rows = cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, params).fetchall()
    text = "\n".join(r[0] for r in rows)
    ms = float(re.search(r"Execution Time: ([\d.]+) ms", text).group(1))
    return ms, text


CASES = [
    {
        "name": "Open-alert feed (newest first, per tenant)",
        "drop": "DROP INDEX alert_open_feed",
        "before_sql": None,
        "sql": "SELECT id, vin, type_code, message, created_at FROM alert WHERE tenant_id = 1 AND status = 'OPEN' "
               "ORDER BY id DESC LIMIT 50",
        "fix": "Partial index `(tenant_id, id DESC) WHERE status='OPEN'` - an ordered index scan that stops after 50 rows.",
    },
    {
        "name": "Abnormal battery degradation list",
        "drop": "DROP INDEX soh_forecast_anomalies",
        "sql": "SELECT vin, soh_now, slope_per_year, anomaly_score FROM soh_forecast WHERE tenant_id = 1 "
               "AND anomaly_score > 3.5 ORDER BY anomaly_score DESC LIMIT 20",
        "fix": "Partial index on `(tenant_id, anomaly_score DESC) WHERE anomaly_score > 3.5` (~2.5 % of rows).",
    },
    {
        "name": "30-day fleet energy trend over battery_daily (12M rows)",
        "drop": "DROP INDEX battery_daily_tenant_day_cover; DROP INDEX battery_daily_tenant_day",
        "sql": "SELECT day, sum(km), sum(energy_kwh), sum(charge_cost), avg(soh_pct) FROM battery_daily "
               "WHERE tenant_id = 1 AND day > (SELECT max(day) FROM battery_daily) - 30 GROUP BY day ORDER BY day",
        "fix": "Covering index `(tenant_id, day) INCLUDE (km, energy_kwh, charge_cost, soh_pct)`: index-only scan "
               "per hypertable chunk, no heap fetches (plus chunk exclusion on `day`).",
    },
    {
        "name": "Vehicle list page 1000 - OFFSET vs keyset pagination",
        "drop": None,
        "before_sql": "SELECT vin, registration, model_code FROM vehicle WHERE tenant_id = 1 ORDER BY vin "
                      "OFFSET 49950 LIMIT 50",
        "sql": "SELECT vin, registration, model_code FROM vehicle WHERE tenant_id = 1 AND vin > %s ORDER BY vin LIMIT 50",
        "params_from": "SELECT vin FROM vehicle WHERE tenant_id = 1 ORDER BY vin OFFSET 49949 LIMIT 1",
        "fix": "Keyset (`vin > :cursor`) on the `(tenant_id, vin)` index: cost is independent of page depth; "
               "OFFSET must walk and discard 49,950 rows.",
    },
    {
        "name": "Fleet-wide average SoC per 15 min over the last 6 h - raw hypertable vs continuous aggregate",
        "drop": None,
        "before_sql": "SELECT time_bucket('15 minutes', ts) b, avg(soc_pct), count(*) FROM telemetry "
                      "WHERE tenant_id = 1 AND ts > now() - interval '6 hours' GROUP BY b ORDER BY b",
        "sql": "SELECT bucket, avg(avg_soc), sum(samples) FROM telemetry_15m WHERE tenant_id = 1 "
               "AND bucket > now() - interval '6 hours' GROUP BY bucket ORDER BY bucket",
        "fix": "TimescaleDB continuous aggregate `telemetry_15m` (materialised, refreshed every minute) - "
               "reads pre-aggregated buckets instead of every raw sample.",
    },
]


def n_plus_one(cur) -> tuple[float, float, int]:
    """ORM-style N+1 (1 query for fleets + 1 COUNT per fleet) vs a single JOIN/GROUP BY."""
    t0 = time.perf_counter()
    fleets = cur.execute("SELECT id FROM fleet WHERE tenant_id = 1").fetchall()
    for (fid,) in fleets:
        cur.execute("SELECT count(*) FROM vehicle WHERE fleet_id = %s", (fid,)).fetchone()
    n1 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    cur.execute("SELECT f.id, count(v.vin) FROM fleet f LEFT JOIN vehicle v ON v.fleet_id = f.id "
                "WHERE f.tenant_id = 1 GROUP BY f.id").fetchall()
    one = (time.perf_counter() - t0) * 1000
    return n1, one, len(fleets) + 1


def main():
    lines = ["# SQL optimisation - EXPLAIN ANALYZE before / after", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} on the seeded 100K-vehicle dataset "
             "(`make explain`). Each query ran warm; times are PostgreSQL execution time.", "",
             "| Query | Before (ms) | After (ms) | Speed-up | Fix |", "|---|---:|---:|---:|---|"]
    details = []
    with psycopg.connect(settings().database_url) as c, c.cursor() as cur:
        cur.execute("ANALYZE alert; ANALYZE soh_forecast; ANALYZE vehicle")
        for case in CASES:
            params = ()
            if case.get("params_from"):
                params = (cur.execute(case["params_from"]).fetchone()[0],)
            plan(cur, case["sql"], params)  # warm
            after_ms, after_plan = plan(cur, case["sql"], params)
            if case["drop"]:
                with c.transaction(force_rollback=True):
                    cur.execute(case["drop"])
                    plan(cur, case["sql"], params)
                    before_ms, before_plan = plan(cur, case["sql"], params)
            else:
                plan(cur, case["before_sql"])
                before_ms, before_plan = plan(cur, case["before_sql"])
            speed = before_ms / after_ms if after_ms else float("inf")
            lines.append(f"| {case['name']} | {before_ms:.2f} | {after_ms:.2f} | {speed:.1f}x | {case['fix']} |")
            details += [f"## {case['name']}", "", "**Before**", "```", before_plan, "```", "**After**", "```",
                        after_plan, "```", ""]
            print(f"{case['name']}: {before_ms:.2f} ms -> {after_ms:.2f} ms ({speed:.1f}x)")
        n1, one, q = n_plus_one(cur)
        lines.append(f"| Fleets with vehicle counts - N+1 ({q} queries) vs one GROUP BY | {n1:.2f} (wall) | "
                     f"{one:.2f} (wall) | {n1 / one:.1f}x | The API issues one JOIN/GROUP BY (`GET /v1/fleets`); "
                     "no ORM lazy loading anywhere. |")
        print(f"N+1: {n1:.2f} ms -> {one:.2f} ms")
    OUT.write_text("\n".join(lines + [""] + details))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
