# VoltPath architecture

## System context and data flow

```mermaid
flowchart LR
  subgraph OEM["OEM clouds (device-free)"]
    V[Voltra<br/>nested JSON]:::oem
    N[Nimbus<br/>flat JSON, imperial]:::oem
    K[Kestrel<br/>pipe text]:::oem
  end
  SIM[[Simulator<br/>100K EVs, bursts, dupes,<br/>out-of-order, outages]] -.emulates.-> OEM
  OEM -- "HTTPS webhooks<br/>HMAC-SHA256, 1K/batch" --> ING

  subgraph EDGE["Ingest gateway (stateless, N replicas × 4 workers)"]
    ING[verify → normalise → validate<br/>→ Bloom dedup → produce<br/>202 only after broker ack]
  end
  ING -- "key=VIN, lz4, acks=all" --> RAW[(telemetry.raw<br/>12 partitions, 24 h)]
  ING -- rejects + reason --> DLQ[(telemetry.dlq)]

  RAW -- "group: processor" --> PROC
  RAW -- "group: lake" --> LAKE

  subgraph STREAM["Stream processor (partition owners)"]
    PROC[exact seq-window dedup · watermark<br/>per-VIN state machine · rules<br/>A*/Dijkstra range check · Count-Min top-K]
  end
  PROC -- "live state + checkpoint, GEO,<br/>KPI partials, cooldowns, pub/sub" --> REDIS[(Redis)]
  PROC -- "1 pt / 30 s / vehicle (COPY)" --> TS[(TimescaleDB<br/>telemetry hypertable<br/>+ 15-min cagg)]
  PROC -- "trips, sessions, alerts" --> PG[(PostgreSQL 3NF + RLS)]
  LAKE[Lake writer] -- "Parquet, zstd, Hive partitions" --> S3[(S3 / SeaweedFS lake)]

  subgraph BATCH["Batch layer (DuckDB)"]
    B1[history · daily rollup<br/>SoH regression · cohort z-score<br/>embeddings · range model]
  end
  S3 --> B1
  B1 --> TS & PG
  B1 -- "16-d degradation vectors" --> VEC[(pgvector HNSW)]

  subgraph SERVE["API (FastAPI) + Copilot"]
    API[OAuth2/JWT RS256 · RBAC · RLS<br/>rate limit · keyset pagination · audit]
    AG[Charging Copilot<br/>Claude tool use, guardrails,<br/>two-phase actions]
  end
  REDIS & TS & PG & VEC --> API
  API <--> AG
  API -- "REST + WebSocket" --> WEB[React UI<br/>car-screen style, dark/light]
  PROM[(Prometheus)] -. scrape .- ING & PROC & API & LAKE
  PROM --> GRAF[Grafana SLO board]
  classDef oem fill:#1e2a4a,stroke:#3e6ae1,color:#fff
```

## Request path: "Can this vehicle reach a charger?"

```mermaid
sequenceDiagram
  participant UI as Web UI
  participant API
  participant R as Redis
  participant PG as PostgreSQL (RLS)
  participant G as Road graph (in-process)
  UI->>API: GET /v1/vehicles/{vin}/reachable-chargers (Bearer JWT)
  API->>API: verify RS256 + require(charging:plan)
  API->>PG: vehicle master (SET LOCAL app.tenant_id)
  API->>R: HGET vs:{tenant} vin (live state)
  API->>PG: last 5 trips → recent Wh/km
  API->>API: ML range (GBM) vs baseline
  API->>R: occ:* (free connectors)
  API->>G: multi-target Dijkstra, cutoff = range
  G-->>API: k nearest free chargers + routes
  API-->>UI: chargers, ETA, arrival SoC, polyline (masked if no location:precise)
  API--)PG: audit_log (async batch)
```

## Deployment (Kubernetes, any cloud)

```mermaid
flowchart TB
  subgraph K8S["Kubernetes (EKS / GKE / AKS) - Helm chart infra/helm/voltpath"]
    IN[Ingress TLS 1.3<br/>+ OEM mTLS host]
    ING2[ingest Deployment<br/>HPA 4-24]
    PR[processor Deployment<br/>KEDA on consumer lag 3-12]
    LW[lake-writer]
    AP[api Deployment HPA 3-12]
    WB[web nginx]
    CJ[batch CronJob 6 h]
    HK[init Job: migrate, topics, bucket]
    CN[(CloudNativePG<br/>Timescale+pgvector, 3 instances, sync replica)]
  end
  IN --> ING2 & AP & WB
  MSK[(Managed Kafka<br/>MSK / Confluent / Event Hubs)]
  RC[(Managed Redis)]
  OBJ[(Object store S3/GCS/Blob<br/>lifecycle tiering)]
  SM[(Secret manager → External Secrets)]
  ING2 --> MSK --> PR & LW
  PR --> RC & CN
  LW --> OBJ
  AP --> RC & CN
  CJ --> OBJ & CN
  SM -.-> K8S
```

- **Stateless:** ingest, api and web scale horizontally on CPU through the HPA.
- **Processors** scale on Kafka lag through KEDA, capped at 12 by the partition count.
- **No single point of failure:**
  - Kafka RF=3 with `min.insync.replicas=2`
  - Redis primary plus replica with automatic failover
  - Postgres with 3 CNPG instances and a synchronous replica
  - PodDisruptionBudgets, and pods spread across AZs
- **Recovery** was demonstrated locally by `make chaos` (broker and processor SIGKILL, zero loss).

## Hot / warm / cold data lifecycle

| Tier | Store | Resolution | Retention | Monthly cost at 100K vehicles (AWS list prices, estimate) |
|---|---|---|---|---|
| Hot | Redis | latest state per vehicle, 2 s KPIs | live | ~$250 (r7g.large ×2) |
| Warm | TimescaleDB (compressed after 1 d) | 1 pt / 30 s / vehicle + 15-min cagg | 30 d | ~$1,100 (3× r7g.2xlarge + 1.5 TB gp3) |
| Cold | Parquet on S3 (zstd, ~90 B/event) | full 1 Hz fidelity | 3 y (IA after 30 d, Glacier IR after 90 d) | grows ~$0.6K/month each month; ~$6.5K at steady state |
| History | `battery_daily` hypertable + Parquet | daily per vehicle | 3 y | negligible |

Raw volume at 1 Hz is 8.6B events/day, about 0.8 TB/day as Parquet (vs 8.6 TB/day raw JSON). The warm tier keeps 1/30 of the samples and compresses about 10–20×.

## Consistency choices (CAP / PACELC)

| Data | Choice | Why |
|---|---|---|
| Billing, subscriptions, schedules, audit, RBAC | **CP**, PC/EC (sync replica, serializable where needed) | Money and compliance: a stale or lost write is unacceptable. We accept a small write-latency cost. |
| Charging sessions and trips | CP, written idempotently from the stream | Derived from events and replayable, but billing-relevant |
| Live vehicle state, KPIs, map clusters | **AP**, PA/EL | A second of staleness is fine. Availability and latency matter more; it can be rebuilt from Kafka. |
| Raw telemetry (log, lake) | AP at the edge (OEM retries) + durable log (RF=3) | Ingestion must never block. Order matters only per vehicle, which the VIN key guarantees. |
| Alerts | At-least-once + cooldown dedup | A duplicate alert is annoying. A missed critical alert is worse. |
