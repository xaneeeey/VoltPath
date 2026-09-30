# ADR-0002: Polyglot persistence, one store per access pattern

- Status: accepted · Date: 2026-09-30

## Context
Access patterns differ by orders of magnitude:
- ACID master data and billing (small, relational, strongly consistent)
- sub-second "latest state of vehicle X" and geo queries
- months of time-series for charts
- years of raw history for batch analytics
- similarity search over battery-degradation curves

No single database serves all of these well. A single SQL database collapses under write amplification, lock contention, mixed OLTP/OLAP load, and a single primary's write ceiling.

## Decision
| Data | Store | Why | CAP / PACELC |
|---|---|---|---|
| Tenants, users, RBAC, fleets, vehicles, chargers, tariffs, sessions, schedules, alerts, audit | **PostgreSQL 16** (3NF, RLS) | Constraints, FKs, transactions, row-level security | CP; PC/EC: consistency over latency |
| Latest vehicle state, GEO index, KPI partials, rate limits, alert cooldowns, pub/sub | **Redis** (NoSQL key-value) | O(1) reads, GEOSEARCH, sub-ms, TTLs | AP; PA/EL: stale-by-a-second is fine |
| Warm telemetry (30 d, downsampled to 1 point per 30 s per vehicle) and 15-minute rollups | **TimescaleDB** hypertable (time + hash(vin) partitions, native compression, continuous aggregates) | SQL over time-series, 10–20× compression, chunk pruning | CP within Postgres |
| Full-fidelity raw events and 120-day history | **Parquet on S3** (SeaweedFS locally, S3/GCS in cloud), Hive-partitioned | Cheapest per GB, columnar scans with DuckDB/Spark | Eventual (object store) |
| Battery-degradation fingerprints, knowledge-base chunks | **pgvector** (HNSW) | k-NN in the same transaction boundary and RLS as the rest | Same as Postgres |

## Consequences
- Four technologies to operate. We mitigate this by co-locating Timescale and pgvector as Postgres extensions: one backup and one failover story for all relational, time-series and vector data.
- Redis is a cache of derived state, not the source of truth. It can be rebuilt by replaying Kafka, because processor checkpoints live in the same hash.
- Tiering and cost estimate:
  - **Hot:** Redis. About 300 B × 100K vehicles ≈ 30 MB.
  - **Warm:** Timescale, 30 days. 100K vehicles × 2,880 rows/day ≈ 288M rows/day, about 60 B/row compressed ≈ 17 GB/day → roughly 0.5 TB for 30 days.
  - **Cold:** Parquet with zstd, about 90 B/event: 8.6 B events/day ≈ 0.8 TB/day ≈ 285 TB/year. That is about $6.5K/month on S3 Standard-IA or $1K/month on Glacier IR after 90 days. Alternative: downsample the cold tier to 10 s after 90 days for a 10× saving.
