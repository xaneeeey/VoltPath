# ADR-0006: Device-free ingestion via signed OEM webhooks, normalised at the edge

- Status: accepted · Date: 2026-09-30

## Context
Like the reference company, we integrate with OEM clouds, not aftermarket dongles. Each OEM has its own dialect:
- units (metric vs imperial, fractions vs percent)
- timestamp formats
- payload shape (nested JSON, flat JSON, pipe-delimited text)

Data formats change more often with software-defined vehicles.

## Decision
- One HTTPS endpoint per OEM: `POST /v1/oem/{oem}/telemetry`, with HMAC-SHA256 signatures and batches of up to 1,000 records.
- Adapters are **pure functions** registered by name (`voltcore.oem`), paired with encoders used by the simulator and by contract tests. Onboarding an OEM means one adapter, one encoder and a round-trip test, with no downtime (a rolling deploy of the stateless gateway).
- The canonical schema v1 (`voltcore.schema.TelemetryEvent`) is the only contract downstream services see. A hand-written hot-path validator mirrors it, and a unit test asserts both agree.
- Anything that fails parsing or validation goes to the DLQ with a machine-readable reason. Nothing is silently dropped.

## Consequences
- The gateway is stateless and horizontally scalable behind a load balancer.
- A schema change is versioned (`v` field). Consumers ignore unknown fields, and the processor's checkpoint loader tolerates new ones.
