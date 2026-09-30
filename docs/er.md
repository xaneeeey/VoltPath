# Relational model (3NF)

Source of truth: `db/migrations/001_core.sql` … `006_query_tuning.sql`.

```mermaid
erDiagram
  TENANT ||--o{ SUBSCRIPTION : has
  SUBSCRIPTION_PLAN ||--o{ SUBSCRIPTION : "priced by"
  TENANT ||--o{ APP_USER : employs
  ROLE ||--o{ APP_USER : grants
  ROLE ||--o{ ROLE_PERMISSION : ""
  PERMISSION ||--o{ ROLE_PERMISSION : ""
  ROLE ||--o{ API_CLIENT : grants
  TENANT ||--o{ DEPOT : operates
  TENANT ||--o{ FLEET : owns
  DEPOT ||--o{ FLEET : "home of"
  FLEET ||--o{ VEHICLE : contains
  OEM ||--o{ VEHICLE_MODEL : builds
  VEHICLE_MODEL ||--o{ VEHICLE : "is a"
  VEHICLE ||--|| BATTERY_PACK : carries
  TENANT ||--o{ DRIVER : employs
  DRIVER ||--o{ DRIVER_ASSIGNMENT : ""
  VEHICLE ||--o{ DRIVER_ASSIGNMENT : ""
  CHARGE_OPERATOR ||--o{ CHARGER_STATION : runs
  CHARGE_OPERATOR ||--o{ TARIFF : publishes
  TARIFF ||--o{ TARIFF_WINDOW : "time-of-use"
  CHARGER_STATION ||--o{ CHARGER : "connectors"
  TARIFF ||--o{ CHARGER : "prices"
  VEHICLE ||--o{ TRIP : drives
  DRIVER |o--o{ TRIP : "drove (nullable after erasure)"
  VEHICLE ||--o{ CHARGING_SESSION : ""
  CHARGER ||--o{ CHARGING_SESSION : ""
  VEHICLE ||--o{ CHARGING_SCHEDULE : ""
  APP_USER |o--o{ CHARGING_SCHEDULE : "created by"
  ALERT_TYPE ||--o{ ALERT : classifies
  VEHICLE ||--o{ ALERT : raises
  TENANT ||--o{ AUDIT_LOG : ""
  TENANT ||--o{ ERASURE_REQUEST : ""

  TENANT { int id PK "name UNIQUE" }
  VEHICLE { char17 vin PK "CHECK regex" int tenant_id "FK (fleet_id, tenant_id)" int fleet_id FK text model_code FK }
  BATTERY_PACK { int id PK char17 vin "UNIQUE FK" text chemistry "NMC|LFP" }
  DRIVER { int id PK text full_name "PII (nullable)" timestamptz erased_at "CHECK: erased ⇒ PII null" }
  CHARGER { int id PK int station_id FK text connector_type numeric max_kw int tariff_id FK }
  CHARGING_SESSION { bigint id PK char17 vin FK int charger_id FK timestamptz started_at "UNIQUE (vin, started_at)" }
  ALERT { bigint id PK text type_code FK text status "OPEN|ACK|RESOLVED" }
  AUDIT_LOG { bigint id PK text actor_type "user|agent|service" jsonb details "append-only" }
```

Time-series and analytics tables live outside the 3NF core: `telemetry` (hypertable), `telemetry_15m` (continuous aggregate), `battery_daily` (hypertable), `soh_forecast`, `fleet_energy_daily`, and `battery_embedding`/`kb_chunk` (pgvector).

## Normalisation notes
- **1NF.** Every column is atomic. DTC lists live in telemetry events and the lake, not in the relational core. Tariff time windows are rows (`tariff_window`), not arrays.
- **2NF.** All composite keys (`role_permission`, `driver_assignment (vin, from_ts)`) have no partial dependencies.
- **3NF.** Model attributes (capacity, rated range, OEM) sit in `vehicle_model`, not on `vehicle`. Operator names sit in `charge_operator`. Alert severity comes from `alert_type`. Prices come from the tariff through the charger, not from the session. Sessions store the realised `cost` as a historical fact, because tariffs change later.

## Deliberate denormalisations (documented, constraint-protected)
| Where | Why | How anomalies are prevented |
|---|---|---|
| `vehicle.tenant_id` (derivable via `fleet`) | RLS predicate and the hottest queries avoid a join | Composite FK `(fleet_id, tenant_id) → fleet(id, tenant_id)` makes a mismatch impossible |
| `tenant_id` on `trip`, `charging_session`, `alert`, `telemetry`, `battery_daily`, `soh_forecast` | RLS on each table without joins. Hypertables need the column for tenant-scoped views. | Written only by the processor and batch from the authoritative `vehicle.tenant_id` |
| `charger_station.geohash6` | Cheap spatial bucketing without PostGIS | Derived in the seed from lat/lon; never updated independently |
| `charging_session.cost`, `energy_kwh` | Historical invoice fact (tariffs change) | Immutable after insert |
