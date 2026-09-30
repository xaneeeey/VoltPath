-- 004_security.sql - least-privilege application role + row-level security (tenant isolation).
-- The API connects as `voltpath_app` and runs every request inside a transaction with
-- `SET LOCAL app.tenant_id = <jwt tenant>`; Postgres itself then refuses cross-tenant rows,
-- so a missing WHERE clause in application code cannot leak data (defence in depth).
-- Pipelines (processor/batch) connect as the owner, which bypasses RLS by design.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voltpath_app') THEN
        CREATE ROLE voltpath_app LOGIN PASSWORD 'voltpath_app';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO voltpath_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO voltpath_app;
GRANT INSERT, UPDATE ON alert, charging_schedule, driver, trip, erasure_request, driver_assignment
    TO voltpath_app;
GRANT DELETE ON driver_assignment TO voltpath_app;
GRANT INSERT ON audit_log TO voltpath_app;                -- append-only: no UPDATE/DELETE
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO voltpath_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM PUBLIC;

CREATE OR REPLACE FUNCTION current_tenant() RETURNS INT
    LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::INT $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['fleet','depot','vehicle','driver','trip','charging_session','charging_schedule',
                             'alert','subscription','battery_daily','soh_forecast',
                             'fleet_energy_daily','battery_embedding','erasure_request']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I TO voltpath_app
                        USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())', t);
    END LOOP;
END $$;

-- Compressed hypertables (columnstore) cannot carry RLS policies, so the app role reads
-- telemetry only through security-barrier views pinned to the session tenant.
REVOKE ALL ON telemetry, telemetry_15m FROM voltpath_app;
CREATE OR REPLACE VIEW telemetry_v WITH (security_barrier) AS
    SELECT * FROM telemetry WHERE tenant_id = current_tenant();
CREATE OR REPLACE VIEW telemetry_15m_v WITH (security_barrier) AS
    SELECT * FROM telemetry_15m WHERE tenant_id = current_tenant();
GRANT SELECT ON telemetry_v, telemetry_15m_v TO voltpath_app;

-- Audit rows: a tenant can read only its own; inserts allowed for own tenant or platform (NULL).
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_read ON audit_log;
DROP POLICY IF EXISTS audit_write ON audit_log;
CREATE POLICY audit_read  ON audit_log FOR SELECT TO voltpath_app USING (tenant_id = current_tenant());
CREATE POLICY audit_write ON audit_log FOR INSERT TO voltpath_app
    WITH CHECK (tenant_id IS NULL OR tenant_id = current_tenant());

-- Users: visible within tenant; login lookup goes through a SECURITY DEFINER function so the
-- app role never needs cross-tenant SELECT on credentials.
ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON app_user;
CREATE POLICY tenant_isolation ON app_user TO voltpath_app USING (tenant_id = current_tenant());

CREATE OR REPLACE FUNCTION auth_lookup(p_email TEXT)
RETURNS TABLE (id INT, tenant_id INT, email TEXT, full_name TEXT, password_hash TEXT, role_code TEXT, disabled BOOLEAN)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT id, tenant_id, email::TEXT, full_name, password_hash, role_code, disabled
    FROM app_user WHERE email = p_email::citext
$$;
REVOKE ALL ON FUNCTION auth_lookup(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_lookup(TEXT) TO voltpath_app;

CREATE OR REPLACE FUNCTION client_lookup(p_client_id TEXT)
RETURNS TABLE (client_id TEXT, tenant_id INT, secret_hash TEXT, role_code TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT client_id, tenant_id, secret_hash, role_code FROM api_client WHERE client_id = p_client_id
$$;
REVOKE ALL ON FUNCTION client_lookup(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION client_lookup(TEXT) TO voltpath_app;
REVOKE SELECT ON api_client FROM voltpath_app;

-- Right to erasure (GDPR Art. 17 / DPDP s.12): wipe PII, keep pseudonymous operational facts.
CREATE OR REPLACE FUNCTION erase_driver(p_driver INT, p_requested_by INT)
RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE n_trips INT; n_assign INT; req INT;
BEGIN
    INSERT INTO erasure_request(tenant_id, subject_type, subject_id, requested_by)
        VALUES (current_tenant(), 'driver', p_driver, p_requested_by) RETURNING id INTO req;
    UPDATE trip SET driver_id = NULL WHERE driver_id = p_driver;
    GET DIAGNOSTICS n_trips = ROW_COUNT;
    DELETE FROM driver_assignment WHERE driver_id = p_driver;
    GET DIAGNOSTICS n_assign = ROW_COUNT;
    UPDATE driver SET full_name = NULL, email = NULL, phone = NULL, license_no = NULL, erased_at = now()
        WHERE id = p_driver;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'driver % not found', p_driver USING ERRCODE = 'no_data_found';
    END IF;
    UPDATE erasure_request SET completed_at = now(),
        summary = jsonb_build_object('trips_unlinked', n_trips, 'assignments_deleted', n_assign)
        WHERE id = req;
    RETURN jsonb_build_object('request_id', req, 'trips_unlinked', n_trips, 'assignments_deleted', n_assign);
END $$;
GRANT EXECUTE ON FUNCTION erase_driver(INT, INT) TO voltpath_app;
GRANT UPDATE (driver_id) ON trip TO voltpath_app;
