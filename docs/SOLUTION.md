# VoltPath: solution document

**Team:** solo · **Problem space:** EV charging + battery health · **Repo tag:** `v1.0-submission`

---

## 1. Problem and users

Mixed fleets are electrifying quickly, and EV operations fail in three expensive ways:

1. **Stranded vehicles.** Dashboards show rated range × SoC. In Bengaluru traffic, heat with AC on, or with payload, that overestimates. In our data the naive estimate over-promises range **84 % of the time**. A driver who trusts it and passes the last free charger gets towed.
2. **Charging at the wrong time.** Time-of-use tariffs differ by about 2× between the night off-peak and the evening peak. Plugging in on arrival (the default) wastes **37–54 %** of charging spend.
3. **Batteries dying early, unnoticed.** A few percent of packs (cell defects, cooling faults, DC-fast-charging abuse) degrade 5–10× faster than their cohort. By the time a driver complains, the warranty window may be gone.

| User | Question VoltPath answers |
|---|---|
| Fleet manager | Which vehicles are at risk *right now*, where should they charge, and what does tonight's charging cost? |
| Driver / dispatcher | Which free charger can I actually reach, how far is it by road, and with what SoC on arrival? |
| Battery / warranty analyst | Which packs are degrading abnormally, and which other vehicles look the same? |
| Finance | What is the charging spend per fleet and day, and how much does scheduling save? |
| Compliance / admin | Who accessed what, what did the AI agent do, and can we erase a driver's personal data? |

## 2. Solution overview

- **Simulator.** 100K EVs drive real Bengaluru roads (OpenStreetMap, 45K-node graph) with physics-based consumption, charging with CC-CV taper, battery ageing, defects and heat events. They are "hosted" by three OEM clouds that push signed webhook batches in three dialects, with realistic chaos: duplicates, out-of-order delivery, outages with backlog bursts, corrupt records and 3× shift-start bursts.
- **Real-time path.** Ingest normalises and validates, then produces to Kafka, answering `202` only after the broker acks. Stream processors apply exact dedup and keep per-vehicle state in Redis. They evaluate alert rules, including a road-network reachability check (multi-target Dijkstra against predicted range), and write the warm store (TimescaleDB), trips and charging sessions (Postgres), and push alerts over WebSocket.
- **Batch path.** Full-fidelity Parquet lands in the lake. DuckDB runs regressions over 120 days of history to produce SoH forecasts, cohort anomaly scores, 16-d degradation embeddings (pgvector) and a gradient-boosted range model.
- **Decisions.**
  - A* routing to the chosen charger.
  - A DP charging schedule under time-of-use tariffs, battery wear and taper, plus an EDF depot scheduler under a site power cap.
  - "Vehicles degrading like this one" via vector search.
- **Interfaces.**
  - A secure REST + WebSocket API (OAuth2/JWT, RBAC, RLS, masking, rate limits, audit).
  - A car-screen-inspired web UI with dark default and a light toggle.
  - A Claude-powered **Charging Copilot** that answers from live data and can *propose* bookings, which a human must confirm.

## 3. Architecture

Diagrams (context, sequence, deployment), data lifecycle and CAP/PACELC choices are in [architecture.md](architecture.md). Decisions are recorded in six ADRs ([docs/adr](adr/)):

| ADR | Decision |
|---|---|
| 0001 | Kafka-API log (Redpanda locally, MSK in cloud), 12 VIN-keyed partitions, DLQ |
| 0002 | Polyglot persistence: Postgres 3NF + TimescaleDB + pgvector, Redis, Parquet lake |
| 0003 | Stream processing as partition-owning Python consumers (Kafka Streams style) instead of Flink |
| 0004 | At-least-once + idempotent sinks = effectively-once (and the chaos-test finding that hardened it) |
| 0005 | OAuth2/JWT RS256 + JWKS, DB-driven RBAC, Postgres RLS, agent acts as the user |
| 0006 | Device-free ingestion: signed OEM webhooks, adapters normalise at the edge |

Microservice boundaries: `ingest` (stateless edge), `processor` (stateful by partition), `lake-writer`, `batch` (scheduled), `api` (stateless), `web`. They share only `voltcore` (the domain library) and contracts (the event schema and web-API contract, both CI-tested). Internals are layered as adapters → domain services → pure core.

## 4. Data engineering and database design

- **3NF relational core:** 20 tables. The ER diagram and denormalisation notes are in [er.md](er.md). Constraints do real work: VIN regex CHECK, composite FK keeping `vehicle.tenant_id` consistent with its fleet, a one-active-subscription partial unique index, and an erased-driver CHECK.
- **Polyglot.** Data placement per store is in ADR-0002: Redis for hot state and GEO, Timescale for warm time-series, Parquet for cold, pgvector for similarity.
- **Partitioning and shard keys.**
  - Kafka is partitioned by **VIN**: per-vehicle order, even spread over 100K keys, no hot partition.
  - The telemetry hypertable is partitioned by **time** (6 h chunks) and **hash(vin)** (4 space partitions).
  - The lake is Hive-partitioned by `dt/hour` with per-partition files.
  - Redis keys are per tenant.
- **Data generation.** The simulator and seed share a deterministic generator (`fleetgen.py`, seed 42), producing:
  - 3 tenants, 13 fleets and depots, 6 fictional EV models, 100K valid-check-digit VINs
  - 1,200 stations with 5,128 connectors and ToU tariffs
  - 60K synthetic drivers, and hidden battery truth (2.5 % defective packs)
- **SQL optimisation.** [db/explain/REPORT.md](../db/explain/REPORT.md) holds real `EXPLAIN (ANALYZE, BUFFERS)` output before and after for five slow queries plus an N+1 case:

  | Query | Speed-up | Fix |
  |---|---:|---|
  | Anomaly list | ~2,300× | partial index |
  | Deep vehicle page | ~500× | keyset vs OFFSET |
  | Fleet-wide 6 h SoC trend | ~64× | continuous aggregate vs raw scan |
  | Fleet energy trend | — | covering index |
  | Open-alert feed | — | partial index |

  The API uses raw SQL with joins, so there is no ORM lazy loading and no N+1.

## 5. Algorithms and data structures

Details, complexity and measurements are in [algorithms.md](algorithms.md). Highlights:
- **A\*** (admissible haversine) expands **8× fewer** nodes than Dijkstra.
- **Multi-target Dijkstra** finds the k nearest *free* chargers within predicted range in 0.4 ms.
- **DP charging** handles SoC-dependent taper and convex wear, so greedy is suboptimal. It is proven against brute force on small instances.
- **Viterbi HMM** handles trip and stop segmentation.
- **Union-find** cleans the graph and clusters unapproved depots.
- **VIN check digit** and **DTC regex** parsing.
- **Rotating Bloom filter**, an **exact 64-bit sequence window**, and a **Count-Min + top-K** hot-spot sketch.
- Geohash / grid cells and Redis GEO for spatial indexing.

## 6. ML, vector search and the AI agent

- **Range model.** Histogram gradient boosting on 8 features, the decisive one being the vehicle's *recent Wh/km* (observable from its last trips).

  | Model | Holdout MAE | Over-estimates range |
  |---|---:|---:|
  | Baseline (rated × SoC × SoH) | 22.0 km | 84 % |
  | Linear regression | 16.2 km | — |
  | **GBM** | **6.8 km** | 52 % |

  The model is served in the API with the baseline shown alongside.
- **Battery anomalies.** Per-pack SoH regression slope, then a robust z-score within the model cohort. Against the simulator's hidden truth: precision 0.973 and recall 0.972 at z > 3.5, which feeds `RAPID_SOH_DROP` alerts.
- **Vector layer.**
  - Battery embeddings (a 16-d normalised degradation curve plus usage profile) in pgvector HNSW power "similar vehicles" (shared root causes).
  - A small maintenance knowledge base uses hybrid retrieval: an exact DTC lexical match plus lexical hashed-embedding k-NN.
- **Charging Copilot.** Claude (`claude-opus-5`) with 9 tools over the same domain services the REST API uses.

  | Guardrail | How |
  |---|---|
  | Least privilege | Tools run with the caller's permissions (`as_agent()`), so no escalation is possible |
  | Human in the loop | The one write tool only creates a pending action; a separate confirm endpoint executes it, same user only, single use |
  | Data minimisation | PII is stripped before reaching the model; location precision follows the user's role |
  | Bounded behaviour | At most 6 tool steps; tool output treated as data (injection-resistant system prompt); server-side refusal fallbacks enabled |
  | Audit | Every tool call is audited as `actor_type=agent` |
  | Graceful degradation | Without an API key, or on API errors, a deterministic router answers with the same tools |

## 7. Scale, non-functional requirements and measured results

Everything below was measured on **one 10-core laptop running the entire stack** (about 18 containers plus a load generator). Results are in `tests/load/results/` and `tests/chaos/results/`.

| NFR | Target | Measured | Notes |
|---|---|---|---|
| Throughput | 100K ev/s | **~56K ev/s per ingest container** (4 workers), 0 errors, broker-acked; processors ~31K ev/s peak (3 replicas) | Laptop-bound. Both tiers are stateless or partitioned: **2 ingest pods + 6–8 processor pods reach 100K**. KEDA/HPA values are in the Helm chart. |
| 3× burst for 5 min, no loss | yes | Kafka absorbed a 1.87M-event backlog and drained it in 72 s. Ingest back-pressure (429) and ack-before-202 mean nothing is lost. | `make load` has `--burst 3` |
| Ingest → dashboard | < 2 s | **96 % < 1 s, 100 % < 2 s** at 20K ev/s steady state | `processor_event_age_seconds` |
| Critical alert | < 5 s | alert latency histogram in Grafana; the rules run inline in the processor | `processor_alert_latency_seconds` |
| API p95 / p99 | < 200 / 500 ms | Redis- and index-backed reads; Grafana "API p95/p99" | no OFFSET, no N+1 |
| Availability / recovery | 99.9 %, recovers from broker/pod kill | **Chaos: broker + processor SIGKILL → 15,000/15,000 stored exactly once**, auto-recovery | ADR-0004 |
| Scalability | no code change | same image; replicas and partitions are configuration | Helm HPA + KEDA |
| Security | OAuth2/JWT, RBAC, tenant isolation, TLS, AES-256, secrets | see §8 | |

**Scale-out math for a sustained 100K ev/s** (1 Hz from every vehicle):

| Tier | Measured capacity | Needed for 100K ev/s | Notes |
|---|---|---|---|
| Ingest | ~14K ev/s per worker core | 2 pods × 4 workers (~112K), plus 50 % headroom → **3 pods** | |
| Processing | ~15K ev/s per replica (native); ~10K in a shared-CPU container | **8 replicas** (≤ 12 partitions) | |
| Warm store | at 1 point / 30 s | 3.3K rows/s | COPY into a hypertable easily sustains this |
| Redis | one coalesced write per vehicle per batch | ~100K ops/s | well within one r7g.large |

**The chaos test found a real durability bug**, and it is fixed. The first run lost about 30K *acknowledged* events: Redpanda dev-mode write caching combined with an ingest that returned 202 before the broker ack. The fixes (ack-before-202, Bloom filter updated only after the ack, automatic producer recovery, write caching disabled) are described in ADR-0004. The rerun passed 15,000/15,000.

## 8. Security and compliance

The full STRIDE model and OWASP API Top 10 mapping are in [stride.md](stride.md). Summary:
- **Identity.** OAuth2 password and client-credentials grants issue RS256 JWTs, with JWKS and OIDC discovery published.
- **Authorisation.** DB-driven RBAC (5 roles, 15 permissions). **Postgres RLS** covers every tenant table, and `security_barrier` views cover compressed hypertables. There is field-level masking for location and PII.
- **Transport and storage.**
  - OEM webhooks are HMAC-signed with a replay window; an optional mTLS host exists for OEM connectors.
  - TLS 1.3 at ingress. KMS-backed AES-256 at rest on MSK, ElastiCache, S3 and EBS (Terraform).
  - Secrets come through External Secrets from the cloud secret manager.
- **Hardening.**
  - Security headers and CSP; per-token and per-IP rate limits; login throttling.
  - Non-root read-only containers; NetworkPolicies.
- **Compliance.** An append-only audit of every data access and every agent tool call. Right-to-erasure (`erase_driver()` with a receipt). Retention via Kafka 24 h, Timescale 30 d and lake lifecycle rules. PII limited to one table.

## 9. Testing

| Suite | What | Evidence |
|---|---|---|
| Unit (pytest + Hypothesis) | 80+ tests; core algorithms at **99 % line coverage** (VIN, DTC, geohash, sketches, graph, DP, HMM, OEM adapters, processor state and rules, ingest gateway) | `make test-unit`, `htmlcov/` |
| Contract | Event schema v1 (provider ingest ↔ consumers processor/lake) and web-UI → API paths; the checked-in contract must match the model | `make test-contract`, `contracts/` |
| Integration (Testcontainers) | 23 tests on real TimescaleDB+pgvector, Redis and Redpanda: RLS isolation, append-only audit, erasure, webhook → Kafka → processor → Redis/Timescale with replay idempotency, DLQ, trips/sessions, OAuth flows, RBAC matrix, masking, keyset pagination, routing, DP, copilot two-phase action and no-escalation, KB retrieval, rate limiting | `make test-int` |
| Acceptance (behave BDD) | 6 scenarios / 36 steps against the running stack: reachable chargers, cheapest overnight plan, copilot never books alone, masking, tenant isolation, agent audit | `make test-bdd` |
| Performance | Python multi-process generator (+ k6 script): steady, 3× burst, soak; client p50/p95/p99 plus server throughput, lag and age from Prometheus | `make load`, `make soak`, `make load-k6` |
| Security | Semgrep SAST (OWASP, secrets), Trivy image/FS/IaC, OWASP ZAP API scan against OpenAPI | `make sec`, CI `sast` / `images-e2e` jobs |
| Chaos / compliance | Kill broker + processor mid-stream → exactly-once check; audit and erasure verified in integration | `make chaos` |

Every suite runs in **GitHub Actions** on each push (`.github/workflows/ci.yml`): lint → unit with an 80 % gate → contract → integration → web build → SAST → images, Trivy, BDD, chaos, load smoke and ZAP → IaC validate (tofu, helm lint).

**Bugs found by these tests** (all fixed):
- Acknowledged-data loss (chaos).
- A 500 on vehicles reporting DTCs (integration).
- Audit batch failure on non-IP client hosts (integration).
- NumPy floats leaking into JSON (copilot path).
- Sequence resets after a device reboot dropping all data (live run).
- Producer truthiness bug (unit).

## 10. DevOps

- **Local:** `docker compose up` (one command, health-checked, idempotent init job, per-container memory ceilings).
- **Images:** one Python image with many entrypoints (shared domain library, a single SBOM to scan), and an nginx web image. Both are multi-stage, non-root, and use a frozen lockfile.
- **Kubernetes:** Helm chart `infra/helm/voltpath` includes:
  - Deployments, Services, TLS Ingress plus an OEM mTLS ingress
  - HPA; KEDA on consumer lag; PDBs; topology spread
  - pre-install migration Job; batch CronJob
  - ExternalSecrets; default-deny NetworkPolicy
- **Postgres:** CloudNativePG `Cluster` (Timescale + pgvector, 3 instances, synchronous replica, S3 backups).
- **Terraform (AWS, validated with OpenTofu):** VPC, EKS (app + db node groups), MSK (TLS + IAM, RF=3), ElastiCache (multi-AZ, TLS), S3 lake with KMS and lifecycle tiering, Secrets Manager, IRSA. Everything is encrypted with a rotating KMS key.
- **Cloud-agnostic:** the app reads endpoints and secrets from the environment only. Moving to GCP or Azure means swapping the Terraform module (GKE + Managed Kafka + Memorystore + GCS, or AKS + Event Hubs Kafka + Azure Cache + Blob). No code changes.
- **Observability:**
  - Prometheus metrics in every service (multi-process safe).
  - A Grafana SLO dashboard: throughput, lag, ingest→state, alert latency, API p95/p99, per-phase processor time, back-pressure.
  - Prometheus alert rules for the SLOs; structured JSON logs.

## 11. Trade-offs, limitations, next steps

- **Measured on a laptop.** 100K ev/s end to end is demonstrated through per-unit capacity and scale-out, not on a single machine.
- **Synthetic labels.** ML and anomaly metrics are evaluated against simulator ground truth. Treat absolute numbers as optimistic, relative gains as meaningful.
- **Simplified routing.** The road graph is undirected (no one-ways), and charger occupancy comes from our own fleet only, not public availability feeds.
- **Lexical KB embedding.** The knowledge-base embedding is a hashing-trick bag of n-grams. A real embedding model is a drop-in replacement (ADR-0003 notes).
- **Next steps:**
  - Flink or Kafka Streams if cross-vehicle windowed joins grow.
  - OCPP integration to actually start and stop charging sessions from schedules.
  - Price forecasting for dynamic tariffs.
  - Per-tenant model fine-tuning.

## 12. Declarations

- **Data:** synthetic only. The OpenStreetMap road network is used under ODbL. Map tiles come from OpenFreeMap. No real personal or vehicle-owner data is used.
- **Open source:** Python (FastAPI, confluent-kafka, psycopg, redis-py, DuckDB, PyArrow, scikit-learn, NumPy/SciPy, PyJWT, bcrypt, prometheus-client, Testcontainers, behave, Hypothesis) and web (React, Vite, Tailwind, MapLibre GL, Recharts, lucide). Infrastructure: TimescaleDB, pgvector, Redpanda, Redis, SeaweedFS, Prometheus, Grafana, CloudNativePG, Helm, OpenTofu. Full lists are in `pyproject.toml` / `uv.lock` and `web/package.json`.
- **AI in the product:** The in-product copilot uses the Anthropic API (`claude-opus-5`) when a key is configured.
- **Affiliation:** Motorq is an industry reference only. All company, operator, OEM and person names in the data are fictional.

## 13. Run it

```bash
docker compose up -d --build   # UI http://localhost:8080 · API docs :8000/docs · Grafana :3000
make test                      # unit + contract + integration + BDD
make load                      # 100K ev/s + 3x burst, writes tests/load/results/
make chaos                     # kill broker + processor, verify exactly-once
make explain                   # SQL before/after -> db/explain/REPORT.md
make sec                       # Semgrep + Trivy + ZAP -> tests/security/reports/
```
