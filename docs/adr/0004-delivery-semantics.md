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
