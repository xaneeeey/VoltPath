"""Schema, row-level security, append-only audit and right-to-erasure on a real TimescaleDB."""

import psycopg
import pytest

pytestmark = pytest.mark.integration


def app_conn(stack, tenant: int):
    c = psycopg.connect(stack["APP_DATABASE_URL"])
    c.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    return c


def test_seed_counts(stack):
    with psycopg.connect(stack["DATABASE_URL"]) as c:
        assert c.execute("SELECT count(*) FROM vehicle").fetchone()[0] == 3000
        assert c.execute("SELECT count(*) FROM charger").fetchone()[0] > 4000
        assert c.execute("SELECT count(*) FROM app_user").fetchone()[0] == 12
        # extensions present
        exts = {r[0] for r in c.execute("SELECT extname FROM pg_extension")}
        assert {"timescaledb", "vector", "citext"} <= exts


def test_rls_isolates_tenants(stack):
    with app_conn(stack, 1) as c1, app_conn(stack, 2) as c2:
        t1 = {r[0] for r in c1.execute("SELECT DISTINCT tenant_id FROM vehicle")}
        t2 = {r[0] for r in c2.execute("SELECT DISTINCT tenant_id FROM vehicle")}
        assert t1 == {1} and t2 == {2}
        vin_of_2 = c2.execute("SELECT vin FROM vehicle LIMIT 1").fetchone()[0]
        assert c1.execute("SELECT count(*) FROM vehicle WHERE vin = %s", (vin_of_2,)).fetchone()[0] == 0


def test_rls_without_tenant_sees_nothing(stack):
    with psycopg.connect(stack["APP_DATABASE_URL"]) as c:
        assert c.execute("SELECT count(*) FROM vehicle").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM driver").fetchone()[0] == 0


def test_rls_blocks_cross_tenant_writes(stack):
    with app_conn(stack, 1) as c:
        vin = c.execute("SELECT vin FROM vehicle LIMIT 1").fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO alert(tenant_id, vin, type_code, message, event_ts) "
                      "VALUES (2, %s, 'LOW_SOC', 'x', now())", (vin,))


def test_telemetry_only_through_tenant_view(stack):
    with psycopg.connect(stack["DATABASE_URL"]) as owner:
        v1 = owner.execute("SELECT vin FROM vehicle WHERE tenant_id = 1 LIMIT 1").fetchone()[0]
        v2 = owner.execute("SELECT vin FROM vehicle WHERE tenant_id = 2 LIMIT 1").fetchone()[0]
        for vin, t in ((v1, 1), (v2, 2)):
            owner.execute("INSERT INTO telemetry VALUES (now(), %s, %s, 12.9, 77.5, 10, 50, 95, 30, 100, -2, 'PERIODIC')",
                          (vin, t))
        owner.commit()
    with app_conn(stack, 1) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM telemetry")
    with app_conn(stack, 1) as c:
        vins = {r[0] for r in c.execute("SELECT vin FROM telemetry_v")}
        assert v1 in vins and v2 not in vins


def test_audit_log_is_append_only(stack):
    with app_conn(stack, 1) as c:
        c.execute("INSERT INTO audit_log(tenant_id, actor_type, action) VALUES (1, 'user', 'test')")
        c.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE audit_log SET action = 'tampered'")
    with app_conn(stack, 1) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM audit_log")


def test_right_to_erasure(stack):
    with app_conn(stack, 1) as c:
        did = c.execute("SELECT id FROM driver WHERE erased_at IS NULL LIMIT 1").fetchone()[0]
        res = c.execute("SELECT erase_driver(%s, NULL)", (did,)).fetchone()[0]
        c.commit()
        row = c.execute("SELECT full_name, email, phone, license_no, erased_at FROM driver WHERE id = %s",
                        (did,)).fetchone()
        assert row[:4] == (None, None, None, None) and row[4] is not None
        assert res["assignments_deleted"] >= 0
        assert c.execute("SELECT count(*) FROM driver_assignment WHERE driver_id = %s", (did,)).fetchone()[0] == 0
        assert c.execute("SELECT completed_at IS NOT NULL FROM erasure_request WHERE id = %s",
                         (res["request_id"],)).fetchone()[0]


def test_auth_lookup_is_the_only_cross_tenant_path(stack):
    with psycopg.connect(stack["APP_DATABASE_URL"]) as c:
        assert c.execute("SELECT count(*) FROM app_user").fetchone()[0] == 0
        row = c.execute("SELECT * FROM auth_lookup('viewer@zenith.test')").fetchone()
        assert row is not None and row[1] == 2
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM api_client")
