# VoltPath: EV charging and battery-health intelligence

> *When and where should each vehicle charge at the lowest cost, and whose battery is dying early?*

VoltPath is a connected-vehicle platform for EV fleets. It ingests telemetry from **100,000 simulated vehicles** pushed by three OEM clouds in three different dialects. It detects stranded-vehicle risk in real time with road-network search, and schedules charging with a dynamic programme over time-of-use tariffs. It predicts range with a model that beats the dashboard estimate by 69 %, and finds abnormally degrading batteries with 97 % precision and 97 % recall. Everything is exposed through a secure multi-tenant API, a car-screen-style web UI, and an AI copilot that can only *propose* actions.

Built solo in 48 h for the Motorq-referenced Connected Vehicle Intelligence hackathon. Motorq is used only as an industry reference; all data is synthetic, and all OEM, operator and people names are fictional.

## Quick start

```bash
docker compose up -d --build        # or: make up
```

This one command:
1. Starts TimescaleDB (with pgvector), Redpanda (the Kafka API), Redis, SeaweedFS (S3), Prometheus and Grafana.
2. Runs migrations and seeds **100,000 vehicles**, 5,128 charging connectors, 60K synthetic drivers and 3 tenants.
3. Generates **120 days of battery history** (12M rows) and trains the range model.
4. Starts the simulator streaming live telemetry through the ingest gateway and stream processors.

| What | Where |
|---|---|
| Web UI (dark by default, toggle top-right) | http://localhost:8080 |
| API + OpenAPI docs | http://localhost:8000/docs |
| Grafana SLO dashboard | http://localhost:3000 (anonymous viewer) |
| Redpanda console (topics, lag) | http://localhost:8081 |

**Demo accounts.** The password for all seeded test users is `SEED_PASSWORD` in `.env.example`.

| User | Sees |
|---|---|
| `fleetmanager@acme.test` | everything for Acme Logistics (55K vehicles), can book charging |
| `analyst@acme.test` | analytics; locations masked to ~5 km |
| `viewer@acme.test` | read-only, masked |
| `admin@acme.test` | audit trail, right-to-erasure |
| `fleetmanager@zenith.test` | a different tenant (30K vehicles), fully isolated |

**Copilot.** Set `ANTHROPIC_API_KEY` in `.env` to use Claude (`claude-opus-5`, tool use). Without a key, a deterministic rule-based copilot answers the same questions with the same tools and the same audit trail.

**Tuning.** `SIM_RATE` (default 20,000 ev/s, which keeps a laptop responsive) sets the simulator's aggregate rate. `100000` means every vehicle reports at 1 Hz.

## What's inside

```
src/voltcore/        pure domain library: VIN/DTC parsing, OEM adapters, geohash, Bloom + Count-Min,
                     A*/Dijkstra road graph, DP charging planner, HMM trip segmentation, EV energy model
src/voltpath/
  simulator/         100K-vehicle vectorised simulator (OSM road network, physics, chaos: dupes,
                     out-of-order, outages, bursts, corrupt records)
  ingest/            OEM webhook gateway: HMAC auth -> normalise -> validate -> dedup -> Kafka (ack-before-202)
  processor/         stream processor (state, dedup, rules, alerts, Timescale, Redis) + Parquet lake writer
  batch/             DuckDB over the lake: history, SoH regression, cohort anomalies, embeddings, ML training
  api/               FastAPI: OAuth2/JWT RS256, RBAC, Postgres RLS, rate limiting, audit, WebSocket
  agent/             Charging Copilot (Claude tool use, guardrails, two-phase actions)
web/                 React + Vite + Tailwind + MapLibre (OpenFreeMap tiles), car-screen UI
db/migrations/       3NF core, Timescale hypertables + continuous aggregate, pgvector, RLS, tuning indexes
infra/               compose, Dockerfiles, Helm chart, Terraform (AWS), CloudNativePG, Prometheus/Grafana
tests/               unit · contract · integration (Testcontainers) · BDD · load · chaos · security
docs/                SOLUTION.md, architecture, ER (3NF), ADRs, STRIDE, algorithms, SQL plans
```

## Evidence

| Claim | Result | Reproduce |
|---|---|---|
| Ingest throughput, one container (4 workers) | **~56K ev/s** accepted, 0 errors, **202 only after broker ack** | `make load` |
| Burst absorption | 1.87M-event backlog drained in **72 s** with no loss | `make load` + Grafana |
| Ingest → live state latency | **96 % < 1 s, 100 % < 2 s** at 20K ev/s | Grafana "Latency SLOs" |
| Broker + processor killed mid-stream | **15,000 / 15,000** events stored exactly once | `make chaos` |
| Range model vs dashboard estimate | MAE **6.8 km vs 22.0 km** (−69 %) | `make train` |
| Battery anomaly detection | precision **0.973**, recall **0.972** | `docs/algorithms.md` |
| A* vs Dijkstra on the 45K-node Bengaluru graph | **8× fewer** node expansions | `make bench` |
| Charging DP vs charging now | **37–54 %** cheaper | `make bench` |
| SQL tuning | up to **2,287×** (partial index), **499×** (keyset), **64×** (continuous aggregate) | `make explain` → `db/explain/REPORT.md` |
| Tests | 80+ unit (core lib 99 % lines), 8 contract, 23 integration on real containers, 6 BDD scenarios | `make test` |

The capacity figures above were measured on one laptop (10 cores) running the entire stack plus the load generator. See [docs/SOLUTION.md](docs/SOLUTION.md) §7 for the scale-out math to a sustained 100K ev/s.

## Docs
- [Solution document](docs/SOLUTION.md): problem, design, trade-offs, results
- [Architecture](docs/architecture.md): diagrams, data lifecycle, CAP/PACELC
- [ER model (3NF)](docs/er.md) · [ADRs](docs/adr/) · [STRIDE threat model](docs/stride.md)
- [Algorithms + complexity](docs/algorithms.md) · [SQL before/after plans](db/explain/REPORT.md)

## Attribution
- Road network © OpenStreetMap contributors (ODbL), via Overpass.
- Map tiles by [OpenFreeMap](https://openfreemap.org) / OpenMapTiles.

Open-source components are listed in `pyproject.toml` and `web/package.json`.
