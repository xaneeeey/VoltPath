-- 001_core.sql - relational core (3NF). ACID / CP data: tenancy, identity, fleet master data,
-- charging infrastructure, billing and audit.
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- tenancy & billing
CREATE TABLE tenant (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription_plan (
    code                 TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    max_vehicles         INT  NOT NULL CHECK (max_vehicles > 0),
    price_per_vehicle    NUMERIC(10,2) NOT NULL CHECK (price_per_vehicle >= 0)
);

CREATE TABLE subscription (
    id          SERIAL PRIMARY KEY,
    tenant_id   INT  NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    plan_code   TEXT NOT NULL REFERENCES subscription_plan(code),
    starts_on   DATE NOT NULL,
    ends_on     DATE,
    status      TEXT NOT NULL CHECK (status IN ('ACTIVE','PAST_DUE','CANCELLED')),
    CHECK (ends_on IS NULL OR ends_on > starts_on)
);
CREATE UNIQUE INDEX subscription_one_active ON subscription(tenant_id) WHERE status = 'ACTIVE';

-- ---------------------------------------------------------------- identity & RBAC
CREATE TABLE role (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE permission (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE role_permission (
    role_code       TEXT NOT NULL REFERENCES role(code) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permission(code) ON DELETE CASCADE,
    PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE app_user (
    id             SERIAL PRIMARY KEY,
    tenant_id      INT    NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    email          CITEXT NOT NULL UNIQUE,
    full_name      TEXT   NOT NULL,
    password_hash  TEXT   NOT NULL,
    role_code      TEXT   NOT NULL REFERENCES role(code),
    disabled       BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX app_user_tenant ON app_user(tenant_id);

-- Service accounts for OAuth2 client-credentials (machine-to-machine, e.g. OEM webhooks).
CREATE TABLE api_client (
    client_id      TEXT PRIMARY KEY,
    tenant_id      INT  REFERENCES tenant(id) ON DELETE CASCADE,
    secret_hash    TEXT NOT NULL,
    role_code      TEXT NOT NULL REFERENCES role(code),
    description    TEXT NOT NULL
);

-- ---------------------------------------------------------------- vehicles
CREATE TABLE oem (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    payload_format  TEXT NOT NULL CHECK (payload_format IN ('json-nested','json-flat','pipe-text'))
);

CREATE TABLE vehicle_model (
    code             TEXT PRIMARY KEY,
    oem_code         TEXT NOT NULL REFERENCES oem(code),
    name             TEXT NOT NULL,
    capacity_kwh     NUMERIC(6,2) NOT NULL CHECK (capacity_kwh > 0),
    rated_range_km   INT NOT NULL CHECK (rated_range_km > 0),
    max_dc_kw        NUMERIC(6,1) NOT NULL,
    max_ac_kw        NUMERIC(6,1) NOT NULL
);

CREATE TABLE depot (
    id           SERIAL PRIMARY KEY,
    tenant_id    INT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    site_cap_kw  NUMERIC(8,1) NOT NULL CHECK (site_cap_kw > 0),
    UNIQUE (id, tenant_id)
);

CREATE TABLE fleet (
    id          SERIAL PRIMARY KEY,
    tenant_id   INT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    depot_id    INT,
    UNIQUE (tenant_id, name),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (depot_id, tenant_id) REFERENCES depot(id, tenant_id)
);

-- Deliberate denormalisation: vehicle.tenant_id duplicates fleet.tenant_id so row-level
-- security and the hottest queries avoid a join. The composite FK makes the duplicate
-- impossible to get out of sync (the anomaly 3NF protects against).
CREATE TABLE vehicle (
    vin              CHAR(17) PRIMARY KEY CHECK (vin ~ '^[A-HJ-NPR-Z0-9]{17}$'),
    tenant_id        INT  NOT NULL,
    fleet_id         INT  NOT NULL,
    model_code       TEXT NOT NULL REFERENCES vehicle_model(code),
    registration     TEXT NOT NULL,
    model_year       SMALLINT NOT NULL CHECK (model_year BETWEEN 2015 AND 2030),
    commissioned_on  DATE NOT NULL,
    active           BOOLEAN NOT NULL DEFAULT true,
    FOREIGN KEY (fleet_id, tenant_id) REFERENCES fleet(id, tenant_id)
);
CREATE INDEX vehicle_tenant_vin ON vehicle(tenant_id, vin);
CREATE INDEX vehicle_fleet ON vehicle(fleet_id);

CREATE TABLE battery_pack (
    id              SERIAL PRIMARY KEY,
    vin             CHAR(17) NOT NULL UNIQUE REFERENCES vehicle(vin) ON DELETE CASCADE,
    serial          TEXT NOT NULL UNIQUE,
    chemistry       TEXT NOT NULL CHECK (chemistry IN ('NMC','LFP')),
    manufactured_on DATE NOT NULL,
    nominal_kwh     NUMERIC(6,2) NOT NULL
);

-- Personal data lives only here (and is the target of right-to-erasure).
CREATE TABLE driver (
    id           SERIAL PRIMARY KEY,
    tenant_id    INT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    full_name    TEXT,
    email        CITEXT,
    phone        TEXT,
    license_no   TEXT,
    erased_at    TIMESTAMPTZ,
    CHECK (erased_at IS NULL OR (full_name IS NULL AND email IS NULL AND phone IS NULL AND license_no IS NULL))
);
CREATE INDEX driver_tenant ON driver(tenant_id);

CREATE TABLE driver_assignment (
    vin        CHAR(17) NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    driver_id  INT NOT NULL REFERENCES driver(id) ON DELETE CASCADE,
    from_ts    TIMESTAMPTZ NOT NULL,
    to_ts      TIMESTAMPTZ,
    PRIMARY KEY (vin, from_ts)
);
CREATE INDEX driver_assignment_driver ON driver_assignment(driver_id);

-- ---------------------------------------------------------------- charging infrastructure
CREATE TABLE charge_operator (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE tariff (
    id             SERIAL PRIMARY KEY,
    operator_id    INT NOT NULL REFERENCES charge_operator(id),
    name           TEXT NOT NULL,
    currency       CHAR(3) NOT NULL DEFAULT 'INR',
    default_price  NUMERIC(8,3) NOT NULL CHECK (default_price >= 0)
);

CREATE TABLE tariff_window (
    id         SERIAL PRIMARY KEY,
    tariff_id  INT NOT NULL REFERENCES tariff(id) ON DELETE CASCADE,
    start_min  SMALLINT NOT NULL CHECK (start_min BETWEEN 0 AND 1439),
    end_min    SMALLINT NOT NULL CHECK (end_min BETWEEN 0 AND 1440),
    price      NUMERIC(8,3) NOT NULL CHECK (price >= 0)
);
CREATE INDEX tariff_window_tariff ON tariff_window(tariff_id);

CREATE TABLE charger_station (
    id           SERIAL PRIMARY KEY,
    operator_id  INT NOT NULL REFERENCES charge_operator(id),
    name         TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    geohash6     CHAR(6) NOT NULL,
    graph_node   INT NOT NULL
);
CREATE INDEX charger_station_geohash ON charger_station(geohash6);

CREATE TABLE charger (
    id              SERIAL PRIMARY KEY,
    station_id      INT NOT NULL REFERENCES charger_station(id) ON DELETE CASCADE,
    connector_type  TEXT NOT NULL CHECK (connector_type IN ('CCS2','TYPE2','CHADEMO','GBT')),
    max_kw          NUMERIC(6,1) NOT NULL CHECK (max_kw > 0),
    tariff_id       INT NOT NULL REFERENCES tariff(id)
);
CREATE INDEX charger_station_idx ON charger(station_id);

-- ---------------------------------------------------------------- operations
CREATE TABLE trip (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    INT NOT NULL REFERENCES tenant(id),
    vin          CHAR(17) NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    driver_id    INT REFERENCES driver(id) ON DELETE SET NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    distance_km  NUMERIC(8,2),
    energy_kwh   NUMERIC(8,3),
    start_soc    NUMERIC(5,2),
    end_soc      NUMERIC(5,2)
);
CREATE INDEX trip_vin_started ON trip(vin, started_at DESC);

CREATE TABLE charging_session (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    INT NOT NULL REFERENCES tenant(id),
    vin          CHAR(17) NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    charger_id   INT NOT NULL REFERENCES charger(id),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    start_soc    NUMERIC(5,2) NOT NULL,
    end_soc      NUMERIC(5,2),
    energy_kwh   NUMERIC(8,3),
    cost         NUMERIC(10,2),
    UNIQUE (vin, started_at)
);
CREATE INDEX charging_session_tenant_started ON charging_session(tenant_id, started_at DESC);

CREATE TABLE charging_schedule (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenant(id),
    vin             CHAR(17) NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    charger_id      INT REFERENCES charger(id),
    created_by      INT REFERENCES app_user(id),
    created_via     TEXT NOT NULL CHECK (created_via IN ('ui','api','agent')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    target_soc      NUMERIC(5,2) NOT NULL,
    expected_cost   NUMERIC(10,2) NOT NULL,
    plan            JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED','ACTIVE','DONE','CANCELLED'))
);
CREATE INDEX charging_schedule_tenant ON charging_schedule(tenant_id, created_at DESC);

CREATE TABLE alert_type (
    code         TEXT PRIMARY KEY,
    severity     TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    description  TEXT NOT NULL
);

CREATE TABLE alert (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INT NOT NULL REFERENCES tenant(id),
    vin         CHAR(17) NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    type_code   TEXT NOT NULL REFERENCES alert_type(code),
    message     TEXT NOT NULL,
    event_ts    TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACK','RESOLVED')),
    acked_by    INT REFERENCES app_user(id),
    acked_at    TIMESTAMPTZ,
    details     JSONB NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------- compliance
-- Append-only: the application role gets INSERT/SELECT only (see 004_security.sql).
CREATE TABLE audit_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    INT,
    user_id      INT,
    actor_type   TEXT NOT NULL CHECK (actor_type IN ('user','agent','service')),
    action       TEXT NOT NULL,
    resource     TEXT,
    status_code  SMALLINT,
    ip           INET,
    details      JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE erasure_request (
    id            SERIAL PRIMARY KEY,
    tenant_id     INT NOT NULL REFERENCES tenant(id),
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('driver')),
    subject_id    INT NOT NULL,
    requested_by  INT REFERENCES app_user(id),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    summary       JSONB NOT NULL DEFAULT '{}'
);
