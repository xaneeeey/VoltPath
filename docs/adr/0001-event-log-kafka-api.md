# ADR-0001: Kafka-API event log (Redpanda locally, managed Kafka in cloud)

- Status: accepted · Date: 2026-09-30

## Context
100K vehicles × 1 Hz = 100K events/s with 3× bursts. The stream has to be durable and partitioned (per-vehicle ordering), and replayable, because the processor, the lake writer and future consumers read it independently and must survive restarts without data loss.

## Decision
- The log speaks the **Kafka protocol**. Local and CI use **Redpanda** (a single binary without ZooKeeper, starts in about 3 s, fits a laptop). The Terraform stack targets **Amazon MSK**. Any Kafka works without code changes: services only see `KAFKA_BOOTSTRAP`.
- `telemetry.raw` has 12 partitions keyed by VIN, which gives per-vehicle ordering and up to 12 parallel consumers per group. Retention is 24 h, enough to replay a full day into a new consumer.
- `telemetry.dlq` holds rejected records with a reason and is retained for 7 days for OEM onboarding and debugging.
- The producer is idempotent (`enable.idempotence`, `acks=all`, lz4, 20 ms linger).

## Consequences
- The processor, lake writer and any future ML feature job are separate consumer groups. Each can scale, lag and be replayed on its own.
- Partition count caps consumer parallelism. At 100K ev/s we measured about 15–20K ev/s per Python processor, so 12 partitions give headroom. Raising the count later requires a key-aware migration.
- MQTT was not chosen. The platform is device-free and ingests from OEM clouds over HTTPS webhooks (ADR-0006), so a broker for vehicle connections isn't needed.
