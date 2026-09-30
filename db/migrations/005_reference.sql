-- 005_reference.sql - static reference data (RBAC matrix, alert catalogue, OEMs, models, plans).
INSERT INTO permission(code, description) VALUES
 ('fleet:read',        'View fleets, KPIs and vehicle list'),
 ('vehicle:read',      'View vehicle detail and live state'),
 ('location:precise',  'See exact coordinates (otherwise masked to ~5 km geohash cell)'),
 ('telemetry:read',    'Query historical telemetry'),
 ('analytics:read',    'Batch analytics and battery health reports'),
 ('alert:read',        'View alerts'),
 ('alert:ack',         'Acknowledge / resolve alerts'),
 ('charging:plan',     'Compute charging plans and reachable chargers'),
 ('charging:schedule', 'Create charging schedules (an action)'),
 ('copilot:use',       'Ask the AI copilot read-only questions'),
 ('copilot:act',       'Let the copilot propose actions for confirmation'),
 ('driver:read',       'View driver personal data'),
 ('driver:erase',      'Execute right-to-erasure requests'),
 ('audit:read',        'Read the audit trail'),
 ('telemetry:write',   'Push OEM telemetry (machine clients)')
ON CONFLICT DO NOTHING;

INSERT INTO role(code, description) VALUES
 ('viewer',         'Read-only operations staff'),
 ('analyst',        'Data / sustainability analyst'),
 ('fleet_manager',  'Operates the fleet and charging'),
 ('admin',          'Tenant administrator'),
 ('oem_ingest',     'OEM cloud webhook client')
ON CONFLICT DO NOTHING;

INSERT INTO role_permission(role_code, permission_code)
SELECT r, p FROM (VALUES
 ('viewer','fleet:read'),('viewer','vehicle:read'),('viewer','alert:read'),('viewer','copilot:use'),
 ('analyst','fleet:read'),('analyst','vehicle:read'),('analyst','alert:read'),('analyst','copilot:use'),
 ('analyst','telemetry:read'),('analyst','analytics:read'),('analyst','charging:plan'),
 ('fleet_manager','fleet:read'),('fleet_manager','vehicle:read'),('fleet_manager','alert:read'),
 ('fleet_manager','copilot:use'),('fleet_manager','telemetry:read'),('fleet_manager','analytics:read'),
 ('fleet_manager','location:precise'),('fleet_manager','alert:ack'),('fleet_manager','charging:plan'),
 ('fleet_manager','charging:schedule'),('fleet_manager','copilot:act'),('fleet_manager','driver:read'),
 ('oem_ingest','telemetry:write')
) v(r, p)
ON CONFLICT DO NOTHING;
INSERT INTO role_permission(role_code, permission_code)
SELECT 'admin', code FROM permission WHERE code <> 'telemetry:write' ON CONFLICT DO NOTHING;

INSERT INTO alert_type(code, severity, description) VALUES
 ('LOW_RANGE_NO_CHARGER', 'critical', 'Predicted range is below road distance to the nearest available charger'),
 ('LOW_SOC',              'warning',  'State of charge below 15 % while driving'),
 ('BATTERY_OVERTEMP',     'critical', 'Battery pack temperature above safe threshold'),
 ('RAPID_SOH_DROP',       'warning',  'Battery state of health degrading much faster than cohort'),
 ('DTC_CRITICAL',         'critical', 'Critical diagnostic trouble code reported'),
 ('CHARGER_CONGESTION',   'info',     'Charging hotspot: demand exceeding local connector capacity'),
 ('UNAPPROVED_DEPOT',     'warning',  'Cluster of vehicles parked away from approved depots')
ON CONFLICT DO NOTHING;

INSERT INTO oem(code, name, payload_format) VALUES
 ('voltra',  'Voltra Motors', 'json-nested'),
 ('nimbus',  'Nimbus Auto',   'json-flat'),
 ('kestrel', 'Kestrel EV',    'pipe-text')
ON CONFLICT DO NOTHING;

INSERT INTO vehicle_model(code, oem_code, name, capacity_kwh, rated_range_km, max_dc_kw, max_ac_kw) VALUES
 ('VL-AURA',   'voltra',  'Voltra Aura',        40.5, 315,  50,  7.2),
 ('VL-STRATA', 'voltra',  'Voltra Strata',      72.0, 460, 150, 11.0),
 ('NB-CIRRUS', 'nimbus',  'Nimbus Cirrus',      58.0, 410, 120, 11.0),
 ('NB-HAULER', 'nimbus',  'Nimbus Hauler Van',  90.0, 330, 110, 22.0),
 ('KS-SWIFT',  'kestrel', 'Kestrel Swift',      30.2, 250,  30,  7.2),
 ('KS-RANGER', 'kestrel', 'Kestrel Ranger',     60.0, 380, 100, 11.0)
ON CONFLICT DO NOTHING;

INSERT INTO subscription_plan(code, name, max_vehicles, price_per_vehicle) VALUES
 ('starter',    'Starter',     1000,  4.00),
 ('growth',     'Growth',     25000,  3.20),
 ('enterprise', 'Enterprise', 250000, 2.40)
ON CONFLICT DO NOTHING;
