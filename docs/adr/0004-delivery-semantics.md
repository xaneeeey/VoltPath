# ADR-0004: At-least-once delivery + idempotent sinks = effectively-once

- Status: accepted · Date: 2026-09-30

## Context
OEM clouds retry webhooks (duplicates), buffer during outages (bursts of old data), and reorder across vehicles. Our own services crash and rebalance. Billing-relevant facts (charging sessions, costs) must not double count, and alerts must not spam.

## Decision
| Layer | Mechanism |
|---|---|
| Ingest | HMAC-signed webhooks with a 5-minute replay window. A per-replica **rotating Bloom filter** on (vin, seq) drops cheap exact retransmits (FP 1e-4, bounded memory). Kafka producer is idempotent. |
| Processor | **Exact** per-VIN dedup: a max-seq plus 64-bit bitmap sliding window. Sequence-reset detection when seq jumps backwards while event time moves forward (unit reboot). Offsets are committed after sinks. |
| Sinks | Telemetry PK `(vin, ts)`. `charging_session UNIQUE (vin, started_at)`. Alert cooldown via Redis `SET NX EX`. Live state is last-writer-wins guarded by event time. KPI partials are *recomputed*, never incremented. Lake files are named by (partition, first offset). |
| Back-pressure | Ingest returns `429 Retry-After` when the producer queue exceeds `INGEST_MAX_INFLIGHT`. The simulator (standing in for OEM clouds) honours it, and its bounded send queue blocks the tick loop, so pressure propagates to the source instead of dropping data. |

## Consequences
- Exactly-once transactions (Kafka EOS) aren't needed and don't span Redis/Postgres anyway.
- The Bloom filter can drop a genuinely new event on a false positive (1 in 10,000 per replica). Accepted: periodic telemetry is redundant, and lifecycle events carry distinct seqs.
- The chaos test (`make chaos`) kills the broker and a processor mid-stream and asserts there are no seq gaps in the warm store for sampled vehicles.

## Evidence: what the chaos test found (and fixed)
The first `make chaos` run **lost about 30K acknowledged events**. Two defects combined:
1. Redpanda's dev-container mode enables **write caching**: the broker acks before fsync, so SIGKILL drops acknowledged data. The idempotent producer then hit `OUT_OF_ORDER_SEQUENCE` (fatal), and librdkafka purged its queue.
2. The gateway returned `202` once records were **queued locally**, not once the broker had them.

Fixes:
- The gateway answers `202` only after the broker acknowledges every record in the batch (`acks=all`). Otherwise it returns `503 Retry-After`.
- Batch keys are added to the Bloom filter only after the ack, so a retried batch isn't mistaken for a duplicate.
- The gateway replaces the producer automatically after a fatal error.
- `write_caching_default=false` is set on the cluster and `write.caching=false` on each topic.

The rerun stored **15,000 / 15,000** events exactly once. The client retried 4 times while the broker was down (`tests/chaos/results/`).
