# ADR-0003: Stream processing as partition-owning Python consumers (not Flink/Spark)

- Status: accepted · Date: 2026-09-30

## Context
Real-time requirements:
- dedup and out-of-order handling
- per-vehicle state (trips, charging sessions)
- windowed aggregates and heavy hitters
- alerting in under 5 s, including a road-graph search

The team is one person for 48 hours, and all domain logic (routing, DP, rules, ML features) is Python.

## Decision
Build the processor the way Kafka Streams works: a consumer group where each instance owns partitions and the per-VIN state lives in local memory. State is checkpointed to Redis in the same write as the public live state, and restored lazily when a new owner first sees a VIN.
- Hot path uses `confluent-kafka` (librdkafka) and `orjson`, and coalesces one Redis write per VIN per batch.
- Warm-store writes use an optimistic `COPY`, falling back to `INSERT … ON CONFLICT` only when a replay collides with existing rows.
- Offsets are committed only after all sinks succeed.

## Consequences
- Measured on an M-series laptop, sharing CPU with the whole stack:
  - one processor sustains about 15–20K ev/s
  - three processors drained a 3.6M backlog while taking 20K ev/s of live input
  - 100K ev/s needs about 6–8 replicas, which the 12 partitions allow
- No separate cluster to run, and the same code and tests serve batch and stream.
- Flink would give exactly-once state snapshots and event-time windows out of the box. We emulate the parts we need: an exact seq-window dedup, a watermark for late events (late events still go to storage but never touch live state), and idempotent sinks. If windowed joins across vehicles become central, migrating to Flink or Kafka Streams is the next step.
