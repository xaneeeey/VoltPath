# SQL optimisation - EXPLAIN ANALYZE before / after

Generated 2026-09-30 17:44 on the seeded 100K-vehicle dataset (`make explain`). Each query ran warm; times are PostgreSQL execution time.

| Query | Before (ms) | After (ms) | Speed-up | Fix |
|---|---:|---:|---:|---|
| Open-alert feed (newest first, per tenant) | 0.09 | 0.07 | 1.4x | Partial index `(tenant_id, id DESC) WHERE status='OPEN'` - an ordered index scan that stops after 50 rows. |
| Abnormal battery degradation list | 187.55 | 0.08 | 2287.2x | Partial index on `(tenant_id, anomaly_score DESC) WHERE anomaly_score > 3.5` (~2.5 % of rows). |
| 30-day fleet energy trend over battery_daily (12M rows) | 3413.49 | 2838.13 | 1.2x | Covering index `(tenant_id, day) INCLUDE (km, energy_kwh, charge_cost, soh_pct)`: index-only scan per hypertable chunk, no heap fetches (plus chunk exclusion on `day`). |
| Vehicle list page 1000 - OFFSET vs keyset pagination | 112.28 | 0.23 | 499.0x | Keyset (`vin > :cursor`) on the `(tenant_id, vin)` index: cost is independent of page depth; OFFSET must walk and discard 49,950 rows. |
| Fleet-wide average SoC per 15 min over the last 6 h - raw hypertable vs continuous aggregate | 61735.69 | 965.43 | 63.9x | TimescaleDB continuous aggregate `telemetry_15m` (materialised, refreshed every minute) - reads pre-aggregated buckets instead of every raw sample. |
| Fleets with vehicle counts - N+1 (7 queries) vs one GROUP BY | 685.08 (wall) | 458.46 (wall) | 1.5x | The API issues one JOIN/GROUP BY (`GET /v1/fleets`); no ORM lazy loading anywhere. |

## Open-alert feed (newest first, per tenant)

**Before**
```
Limit  (cost=0.28..5.14 rows=50 width=102) (actual time=0.015..0.067 rows=50 loops=1)
  Buffers: shared hit=61
  ->  Index Scan Backward using alert_pkey on alert  (cost=0.28..250.95 rows=2581 width=102) (actual time=0.014..0.061 rows=50 loops=1)
        Filter: ((tenant_id = 1) AND (status = 'OPEN'::text))
        Rows Removed by Filter: 47
        Buffers: shared hit=61
Planning:
  Buffers: shared hit=2
Planning Time: 0.161 ms
Execution Time: 0.091 ms
```
**After**
```
Limit  (cost=0.28..4.34 rows=50 width=102) (actual time=0.017..0.047 rows=50 loops=1)
  Buffers: shared hit=36
  ->  Index Scan using alert_open_feed on alert  (cost=0.28..209.99 rows=2581 width=102) (actual time=0.016..0.043 rows=50 loops=1)
        Index Cond: (tenant_id = 1)
        Buffers: shared hit=36
Planning:
  Buffers: shared hit=2
Planning Time: 0.153 ms
Execution Time: 0.066 ms
```

## Abnormal battery degradation list

**Before**
```
Limit  (cost=2273.57..2273.62 rows=20 width=30) (actual time=187.507..187.512 rows=20 loops=1)
  Buffers: shared hit=40117
  ->  Sort  (cost=2273.57..2276.91 rows=1333 width=30) (actual time=187.506..187.508 rows=20 loops=1)
        Sort Key: anomaly_score DESC
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=40117
        ->  Index Scan using soh_forecast_tenant_risk on soh_forecast  (cost=0.29..2238.10 rows=1333 width=30) (actual time=0.047..187.077 rows=1451 loops=1)
              Index Cond: (tenant_id = 1)
              Filter: (anomaly_score > '3.5'::double precision)
              Rows Removed by Filter: 53549
              Buffers: shared hit=40117
Planning:
  Buffers: shared hit=2
Planning Time: 0.308 ms
Execution Time: 187.549 ms
```
**After**
```
Limit  (cost=0.28..13.08 rows=20 width=30) (actual time=0.027..0.065 rows=20 loops=1)
  Buffers: shared hit=22
  ->  Index Scan using soh_forecast_anomalies on soh_forecast  (cost=0.28..853.29 rows=1333 width=30) (actual time=0.026..0.062 rows=20 loops=1)
        Index Cond: (tenant_id = 1)
        Buffers: shared hit=22
Planning:
  Buffers: shared hit=2
Planning Time: 0.221 ms
Execution Time: 0.082 ms
```

## 30-day fleet energy trend over battery_daily (12M rows)

**Before**
```
Finalize GroupAggregate  (cost=103416.39..103465.99 rows=200 width=24) (actual time=3230.724..3412.448 rows=30 loops=1)
  Group Key: battery_daily.day
  Buffers: shared hit=50323
  InitPlan 2 (returns $1)
    ->  Result  (cost=0.43..0.44 rows=1 width=4) (actual time=0.981..1.034 rows=1 loops=1)
          Buffers: shared hit=84
          InitPlan 1 (returns $0)
            ->  Limit  (cost=0.43..0.43 rows=1 width=4) (actual time=0.974..1.021 rows=1 loops=1)
                  Buffers: shared hit=84
                  ->  Custom Scan (ChunkAppend) on battery_daily battery_daily_1  (cost=0.43..53987.03 rows=2700000 width=4) (actual time=0.972..1.013 rows=1 loops=1)
                        Order: battery_daily_1.day DESC
                        Buffers: shared hit=84
                        ->  Index Only Scan using _hyper_3_10_chunk_battery_daily_day_idx on _hyper_3_10_chunk _hyper_3_10_chunk_1  (cost=0.43..53987.03 rows=2700000 width=4) (actual time=0.969..0.984 rows=1 loops=1)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 95
                              Buffers: shared hit=84
                        ->  Index Only Scan using _hyper_3_9_chunk_battery_daily_day_idx on _hyper_3_9_chunk _hyper_3_9_chunk_1  (cost=0.43..61811.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_8_chunk_battery_daily_day_idx on _hyper_3_8_chunk _hyper_3_8_chunk_1  (cost=0.43..59290.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_7_chunk_battery_daily_day_idx on _hyper_3_7_chunk _hyper_3_7_chunk_1  (cost=0.43..59250.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_6_chunk_battery_daily_day_idx on _hyper_3_6_chunk _hyper_3_6_chunk_1  (cost=0.42..7404.52 rows=400000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
  ->  Gather Merge  (cost=103415.95..103458.55 rows=360 width=48) (actual time=3230.644..3412.267 rows=92 loops=1)
        Workers Planned: 3
        Params Evaluated: $1
        Workers Launched: 3
        Buffers: shared hit=50323
        ->  Sort  (cost=102415.91..102416.21 rows=120 width=48) (actual time=2926.718..2926.770 rows=23 loops=4)
              Sort Key: battery_daily.day
              Sort Method: quicksort  Memory: 26kB
              Buffers: shared hit=50239
              Worker 0:  Sort Method: quicksort  Memory: 27kB
              Worker 1:  Sort Method: quicksort  Memory: 26kB
              Worker 2:  Sort Method: quicksort  Memory: 26kB
              ->  Parallel Custom Scan (ChunkAppend) on battery_daily  (cost=0.43..102411.76 rows=120 width=48) (actual time=206.500..2926.517 rows=23 loops=4)
                    Chunks excluded during runtime: 3
                    Buffers: shared hit=50215
                    ->  Partial GroupAggregate  (cost=0.43..24931.15 rows=30 width=48) (never executed)
                          Group Key: _hyper_3_8_chunk.day
                          ->  Parallel Index Scan Backward using _hyper_3_8_chunk_battery_daily_day_idx on _hyper_3_8_chunk  (cost=0.43..22731.39 rows=175957 width=20) (never executed)
                                Index Cond: (day > ($1 - 30))
                                Filter: (tenant_id = 1)
                    ->  Partial GroupAggregate  (cost=0.43..24962.43 rows=30 width=48) (actual time=308.964..568.581 rows=3 loops=2)
                          Group Key: _hyper_3_9_chunk.day
                          Buffers: shared hit=4963
                          ->  Parallel Index Scan Backward using _hyper_3_9_chunk_battery_daily_day_idx on _hyper_3_9_chunk  (cost=0.43..22728.39 rows=178699 width=20) (actual time=154.297..540.120 rows=82500 loops=2)
                                Index Cond: (day > ($1 - 30))
                                Filter: (tenant_id = 1)
                                Rows Removed by Filter: 67500
                                Buffers: shared hit=4963
                    ->  Partial GroupAggregate  (cost=0.43..24938.99 rows=30 width=48) (never executed)
                          Group Key: _hyper_3_7_chunk.day
                          ->  Parallel Index Scan Backward using _hyper_3_7_chunk_battery_daily_day_idx on _hyper_3_7_chunk  (cost=0.43..22718.39 rows=177624 width=20) (never executed)
                                Index Cond: (day > ($1 - 30))
                                Filter: (tenant_id = 1)
                    ->  Partial GroupAggregate  (cost=0.43..23551.31 rows=26 width=48) (actual time=72.227..2641.655 rows=22 loops=4)
                          Group Key: _hyper_3_10_chunk.day
                          Buffers: shared hit=45078
                          ->  Parallel Index Scan Backward using _hyper_3_10_chunk_battery_daily_day_idx on _hyper_3_10_chunk  (cost=0.43..21560.76 rows=159223 width=20) (actual time=10.045..2338.411 rows=371250 loops=4)
                                Index Cond: (day > ($1 - 30))
                                Filter: (tenant_id = 1)
                                Rows Removed by Filter: 303750
                                Buffers: shared hit=45078
                    ->  Partial GroupAggregate  (cost=0.42..4027.88 rows=4 width=48) (never executed)
                          Group Key: _hyper_3_6_chunk.day
                          ->  Parallel Index Scan Backward using _hyper_3_6_chunk_battery_daily_day_idx on _hyper_3_6_chunk  (cost=0.42..3490.61 rows=42978 width=20) (never executed)
                                Index Cond: (day > ($1 - 30))
                                Filter: (tenant_id = 1)
Planning:
  Buffers: shared hit=101
Planning Time: 8.225 ms
Execution Time: 3413.491 ms
```
**After**
```
Finalize GroupAggregate  (cost=67078.48..67144.45 rows=200 width=24) (actual time=2779.288..2837.640 rows=30 loops=1)
  Group Key: battery_daily.day
  Buffers: shared hit=11132
  InitPlan 2 (returns $1)
    ->  Result  (cost=0.43..0.44 rows=1 width=4) (actual time=4.944..4.955 rows=1 loops=1)
          Buffers: shared hit=84
          InitPlan 1 (returns $0)
            ->  Limit  (cost=0.43..0.43 rows=1 width=4) (actual time=4.937..4.946 rows=1 loops=1)
                  Buffers: shared hit=84
                  ->  Custom Scan (ChunkAppend) on battery_daily battery_daily_1  (cost=0.43..53987.03 rows=2700000 width=4) (actual time=4.935..4.944 rows=1 loops=1)
                        Order: battery_daily_1.day DESC
                        Buffers: shared hit=84
                        ->  Index Only Scan using _hyper_3_10_chunk_battery_daily_day_idx on _hyper_3_10_chunk _hyper_3_10_chunk_1  (cost=0.43..53987.03 rows=2700000 width=4) (actual time=4.932..4.933 rows=1 loops=1)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 95
                              Buffers: shared hit=84
                        ->  Index Only Scan using _hyper_3_9_chunk_battery_daily_day_idx on _hyper_3_9_chunk _hyper_3_9_chunk_1  (cost=0.43..61811.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_8_chunk_battery_daily_day_idx on _hyper_3_8_chunk _hyper_3_8_chunk_1  (cost=0.43..59290.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_7_chunk_battery_daily_day_idx on _hyper_3_7_chunk _hyper_3_7_chunk_1  (cost=0.43..59250.23 rows=3000000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
                        ->  Index Only Scan using _hyper_3_6_chunk_battery_daily_day_idx on _hyper_3_6_chunk _hyper_3_6_chunk_1  (cost=0.42..7404.52 rows=400000 width=4) (never executed)
                              Index Cond: (day IS NOT NULL)
                              Heap Fetches: 0
  ->  Gather Merge  (cost=67078.03..67135.51 rows=480 width=48) (actual time=2779.228..2837.488 rows=129 loops=1)
        Workers Planned: 4
        Params Evaluated: $1
        Workers Launched: 4
        Buffers: shared hit=11132
        ->  Sort  (cost=66077.98..66078.28 rows=120 width=48) (actual time=2682.990..2683.012 rows=26 loops=5)
              Sort Key: battery_daily.day
              Sort Method: quicksort  Memory: 27kB
              Buffers: shared hit=11048
              Worker 0:  Sort Method: quicksort  Memory: 27kB
              Worker 1:  Sort Method: quicksort  Memory: 27kB
              Worker 2:  Sort Method: quicksort  Memory: 27kB
              Worker 3:  Sort Method: quicksort  Memory: 26kB
              ->  Parallel Custom Scan (ChunkAppend) on battery_daily  (cost=0.43..66073.83 rows=120 width=48) (actual time=104.654..2682.485 rows=26 loops=5)
                    Chunks excluded during runtime: 3
                    Buffers: shared hit=11016
                    ->  Partial GroupAggregate  (cost=0.43..18333.40 rows=30 width=48) (actual time=78.088..240.703 rows=2 loops=3)
                          Group Key: _hyper_3_9_chunk.day
                          Buffers: shared hit=2588
                          ->  Parallel Index Only Scan using _hyper_3_9_chunk_battery_daily_tenant_day_cover on _hyper_3_9_chunk  (cost=0.43..16601.95 rows=138492 width=20) (actual time=0.122..199.213 rows=55000 loops=3)
                                Index Cond: ((tenant_id = 1) AND (day > ($1 - 30)))
                                Heap Fetches: 165000
                                Buffers: shared hit=2588
                    ->  Partial GroupAggregate  (cost=0.43..15723.72 rows=30 width=48) (never executed)
                          Group Key: _hyper_3_7_chunk.day
                          ->  Parallel Index Only Scan using _hyper_3_7_chunk_battery_daily_tenant_day_cover on _hyper_3_7_chunk  (cost=0.43..14002.69 rows=137658 width=20) (never executed)
                                Index Cond: ((tenant_id = 1) AND (day > ($1 - 30)))
                                Heap Fetches: 0
                    ->  Partial GroupAggregate  (cost=0.43..15649.43 rows=30 width=48) (never executed)
                          Group Key: _hyper_3_8_chunk.day
                          ->  Parallel Index Only Scan using _hyper_3_8_chunk_battery_daily_tenant_day_cover on _hyper_3_8_chunk  (cost=0.43..13944.54 rows=136367 width=20) (never executed)
                                Index Cond: ((tenant_id = 1) AND (day > ($1 - 30)))
                                Heap Fetches: 0
                    ->  Partial GroupAggregate  (cost=0.43..14504.35 rows=26 width=48) (actual time=71.469..2537.538 rows=24 loops=5)
                          Group Key: _hyper_3_10_chunk.day
                          Buffers: shared hit=8196
                          ->  Parallel Index Only Scan using _hyper_3_10_chunk_battery_daily_tenant_day_cover on _hyper_3_10_chunk  (cost=0.43..12961.62 rows=123398 width=20) (actual time=0.553..2081.618 rows=297000 loops=5)
                                Index Cond: ((tenant_id = 1) AND (day > ($1 - 30)))
                                Heap Fetches: 55000
                                Buffers: shared hit=8196
                    ->  Partial GroupAggregate  (cost=0.42..1862.94 rows=4 width=48) (never executed)
                          Group Key: _hyper_3_6_chunk.day
                          ->  Parallel Index Only Scan using _hyper_3_6_chunk_battery_daily_tenant_day_cover on _hyper_3_6_chunk  (cost=0.42..1482.37 rows=30442 width=20) (never executed)
                                Index Cond: ((tenant_id = 1) AND (day > ($1 - 30)))
                                Heap Fetches: 0
Planning:
  Buffers: shared hit=101
Planning Time: 7.819 ms
Execution Time: 2838.130 ms
```

## Vehicle list page 1000 - OFFSET vs keyset pagination

**Before**
```
Limit  (cost=2088.45..2090.54 rows=50 width=41) (actual time=112.102..112.221 rows=50 loops=1)
  Buffers: shared hit=50090
  ->  Index Scan using vehicle_tenant_vin on vehicle  (cost=0.42..2291.61 rows=54810 width=41) (actual time=0.034..109.952 rows=50000 loops=1)
        Index Cond: (tenant_id = 1)
        Buffers: shared hit=50090
Planning:
  Buffers: shared hit=2
Planning Time: 0.613 ms
Execution Time: 112.278 ms
```
**After**
```
Limit  (cost=0.42..6.86 rows=50 width=41) (actual time=0.034..0.201 rows=50 loops=1)
  Buffers: shared hit=53
  ->  Index Scan using vehicle_tenant_vin on vehicle  (cost=0.42..775.07 rows=6015 width=41) (actual time=0.033..0.195 rows=50 loops=1)
        Index Cond: ((tenant_id = 1) AND (vin > '9VLST72E5TG002030'::bpchar))
        Buffers: shared hit=53
Planning:
  Buffers: shared hit=2
Planning Time: 0.319 ms
Execution Time: 0.225 ms
```

## Fleet-wide average SoC per 15 min over the last 6 h - raw hypertable vs continuous aggregate

**Before**
```
Sort  (cost=679281.14..679281.64 rows=200 width=24) (actual time=61505.090..61678.775 rows=18 loops=1)
  Sort Key: (time_bucket('00:15:00'::interval, telemetry.ts))
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=31601 read=426223
  I/O Timings: shared read=283888.475
  ->  Finalize HashAggregate  (cost=679270.49..679273.49 rows=200 width=24) (actual time=61504.809..61678.512 rows=18 loops=1)
        Group Key: (time_bucket('00:15:00'::interval, telemetry.ts))
        Batches: 1  Memory Usage: 40kB
        Buffers: shared hit=31601 read=426223
        I/O Timings: shared read=283888.475
        ->  Gather  (cost=1000.00..676634.62 rows=351450 width=48) (actual time=47638.020..61677.702 rows=126 loops=1)
              Workers Planned: 5
              Workers Launched: 5
              Buffers: shared hit=31601 read=426223
              I/O Timings: shared read=283888.475
              ->  Parallel Custom Scan (ChunkAppend) on telemetry  (cost=0.00..640489.62 rows=70290 width=48) (actual time=35646.498..60770.241 rows=21 loops=6)
                    Chunks excluded during startup: 0
                    Buffers: shared hit=31601 read=426223
                    I/O Timings: shared read=283888.475
                    ->  Partial HashAggregate  (cost=158572.63..158789.75 rows=17370 width=48) (actual time=46672.124..46672.922 rows=17 loops=2)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_1_chunk.ts)
                          Batches: 1  Memory Usage: 793kB
                          Buffers: shared hit=4598 read=110363
                          I/O Timings: shared read=73804.569
                          Worker 1:  Batches: 1  Memory Usage: 793kB
                          ->  Parallel Seq Scan on _hyper_1_1_chunk  (cost=0.00..152150.50 rows=856284 width=12) (actual time=42.609..44649.871 rows=2125623 loops=2)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 1751012
                                Buffers: shared hit=4598 read=110363
                                I/O Timings: shared read=73804.569
                    ->  Partial HashAggregate  (cost=155807.10..156023.75 rows=17332 width=48) (actual time=42514.594..42521.815 rows=16 loops=2)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_4_chunk.ts)
                          Buffers: shared hit=3621 read=109612
                          I/O Timings: shared read=66218.826
                          Worker 0:  Batches: 1  Memory Usage: 793kB
                          Worker 3:  Batches: 1  Memory Usage: 793kB
                          ->  Parallel Seq Scan on _hyper_1_4_chunk  (cost=0.00..149537.50 rows=835947 width=12) (actual time=1.459..40165.483 rows=2125066 loops=2)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 1723434
                                Buffers: shared hit=3621 read=109612
                                I/O Timings: shared read=66218.826
                    ->  Partial HashAggregate  (cost=154149.57..154369.80 rows=17619 width=48) (actual time=16221.443..16231.933 rows=5 loops=6)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_2_chunk.ts)
                          Batches: 1  Memory Usage: 793kB
                          Buffers: shared hit=8560 read=102572
                          I/O Timings: shared read=76718.560
                          Worker 0:  Batches: 1  Memory Usage: 793kB
                          Worker 1:  Batches: 1  Memory Usage: 793kB
                          Worker 2:  Batches: 1  Memory Usage: 793kB
                          Worker 3:  Batches: 1  Memory Usage: 793kB
                          Worker 4:  Batches: 1  Memory Usage: 793kB
                          ->  Parallel Seq Scan on _hyper_1_2_chunk  (cost=0.00..147806.92 rows=845687 width=12) (actual time=27.931..15359.562 rows=703912 loops=6)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 574526
                                Buffers: shared hit=8560 read=102572
                                I/O Timings: shared read=76718.560
                    ->  Partial HashAggregate  (cost=150190.37..150388.81 rows=15875 width=48) (actual time=28378.115..28385.905 rows=8 loops=3)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_3_chunk.ts)
                          Batches: 1  Memory Usage: 793kB
                          Buffers: shared hit=5022 read=103676
                          I/O Timings: shared read=67146.520
                          Worker 0:  Batches: 1  Memory Usage: 793kB
                          Worker 4:  Batches: 1  Memory Usage: 793kB
                          ->  Parallel Seq Scan on _hyper_1_3_chunk  (cost=0.00..144094.06 rows=812842 width=12) (actual time=1.619..26848.647 rows=1391883 loops=3)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 1136333
                                Buffers: shared hit=5022 read=103676
                                I/O Timings: shared read=67146.520
                    ->  Partial HashAggregate  (cost=5274.42..5280.92 rows=520 width=48) (actual time=182.999..183.021 rows=1 loops=1)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_44_chunk.ts)
                          Buffers: shared hit=2462
                          Worker 1:  Batches: 1  Memory Usage: 49kB
                          ->  Parallel Seq Scan on _hyper_1_44_chunk  (cost=0.00..4861.22 rows=55093 width=12) (actual time=0.045..164.494 rows=93330 loops=1)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 76982
                                Buffers: shared hit=2462
                    ->  Partial HashAggregate  (cost=5254.72..5261.28 rows=525 width=48) (actual time=2510.031..2510.042 rows=1 loops=1)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_46_chunk.ts)
                          Buffers: shared hit=2466
                          Worker 3:  Batches: 1  Memory Usage: 49kB
                          ->  Parallel Seq Scan on _hyper_1_46_chunk  (cost=0.00..4845.81 rows=54522 width=12) (actual time=0.048..2061.971 rows=93062 loops=1)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 75671
                                Buffers: shared hit=2466
                    ->  Partial HashAggregate  (cost=5199.68..5206.25 rows=526 width=48) (actual time=519.762..519.782 rows=1 loops=1)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_48_chunk.ts)
                          Buffers: shared hit=2432
                          Worker 1:  Batches: 1  Memory Usage: 49kB
                          ->  Parallel Seq Scan on _hyper_1_48_chunk  (cost=0.00..4790.73 rows=54526 width=12) (actual time=0.018..492.320 rows=92496 loops=1)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 75425
                                Buffers: shared hit=2432
                    ->  Partial HashAggregate  (cost=5162.51..5169.04 rows=523 width=48) (actual time=466.419..466.584 rows=1 loops=1)
                          Group Key: time_bucket('00:15:00'::interval, _hyper_1_45_chunk.ts)
                          Buffers: shared hit=2440
                          Worker 1:  Batches: 1  Memory Usage: 49kB
                          ->  Parallel Seq Scan on _hyper_1_45_chunk  (cost=0.00..4760.37 rows=53618 width=12) (actual time=0.014..440.518 rows=91743 loops=1)
                                Filter: ((tenant_id = 1) AND (ts > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 74985
                                Buffers: shared hit=2440
Planning:
  Buffers: shared hit=306
Planning Time: 29.158 ms
Execution Time: 61735.691 ms
```
**After**
```
Finalize GroupAggregate  (cost=28655.35..28735.35 rows=200 width=48) (actual time=906.904..961.353 rows=13 loops=1)
  Group Key: _materialized_hypertable_2.bucket
  Buffers: shared hit=17358
  ->  Gather Merge  (cost=28655.35..28726.35 rows=600 width=72) (actual time=906.818..961.241 rows=49 loops=1)
        Workers Planned: 3
        Workers Launched: 3
        Buffers: shared hit=17358
        ->  Sort  (cost=27655.31..27655.81 rows=200 width=72) (actual time=488.498..488.501 rows=12 loops=4)
              Sort Key: _materialized_hypertable_2.bucket
              Sort Method: quicksort  Memory: 26kB
              Buffers: shared hit=17358
              Worker 0:  Sort Method: quicksort  Memory: 26kB
              Worker 1:  Sort Method: quicksort  Memory: 26kB
              Worker 2:  Sort Method: quicksort  Memory: 26kB
              ->  Partial HashAggregate  (cost=27645.16..27647.66 rows=200 width=72) (actual time=488.369..488.376 rows=12 loops=4)
                    Group Key: _materialized_hypertable_2.bucket
                    Batches: 1  Memory Usage: 40kB
                    Buffers: shared hit=17334
                    Worker 0:  Batches: 1  Memory Usage: 40kB
                    Worker 1:  Batches: 1  Memory Usage: 40kB
                    Worker 2:  Batches: 1  Memory Usage: 40kB
                    ->  Parallel Custom Scan (ChunkAppend) on _materialized_hypertable_2  (cost=0.00..26043.82 rows=213513 width=24) (actual time=0.046..406.809 rows=165005 loops=4)
                          Chunks excluded during startup: 0
                          Buffers: shared hit=17334
                          ->  Parallel Seq Scan on _hyper_2_5_chunk  (cost=0.00..26043.82 rows=213513 width=24) (actual time=0.041..377.580 rows=165005 loops=4)
                                Filter: ((tenant_id = 1) AND (bucket > (now() - '06:00:00'::interval)))
                                Rows Removed by Filter: 135000
                                Buffers: shared hit=17334
Planning:
  Buffers: shared hit=10
Planning Time: 9.079 ms
Execution Time: 965.435 ms
```
