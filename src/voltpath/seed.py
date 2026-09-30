"""Load the deterministic 100K-vehicle world into Postgres (idempotent).

Uses COPY (binary-free text protocol) for the large tables: ~100K vehicles, 100K packs,
60K drivers load in seconds instead of minutes of row-by-row INSERTs.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import bcrypt
import psycopg

from voltcore.energy import MODELS_BY_CODE
from voltpath.config import settings, setup_logging
from voltpath.fleetgen import CHEMISTRY, OPERATORS, TODAY, generate

ROLES = ["admin", "fleet_manager", "analyst", "viewer"]


def demo_users(password: str) -> list[tuple[int, str, str, str, str]]:
    """(tenant_id, email, name, hash, role) - test credentials documented in README/.env.example."""
    h = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()
    out = []
    for tid, dom in ((1, "acme"), (2, "zenith"), (3, "metro")):
        for role in ROLES:
            out.append((tid, f"{role.replace('_', '')}@{dom}.test", f"{dom.title()} {role.replace('_', ' ').title()}",
                        h, role))
    return out


def seed(url: str | None = None, force: bool = False) -> bool:
    log = setup_logging("seed")
    s = settings()
    url = url or s.database_url
    with psycopg.connect(url) as conn:
        if not force and conn.execute("SELECT count(*) FROM vehicle").fetchone()[0] > 0:
            log.info("already seeded - skipping")
            return False
        w = generate(s.seed, s.n_vehicles)
        log.info("seeding %d vehicles, %d chargers", len(w.vin), len(w.chargers))
        with conn.cursor() as cur:
            cur.execute("TRUNCATE tenant, charge_operator, tariff, charger_station, api_client RESTART IDENTITY CASCADE")
            for t in w.tenants:
                cur.execute("INSERT INTO tenant(id, name) VALUES (%s, %s)", (t.id, t.name))
                cur.execute("INSERT INTO subscription(tenant_id, plan_code, starts_on, status) VALUES (%s,%s,%s,'ACTIVE')",
                            (t.id, t.plan, date(2025, 1, 1)))
            for u in demo_users(s.seed_password):
                cur.execute("INSERT INTO app_user(tenant_id, email, full_name, password_hash, role_code) "
                            "VALUES (%s,%s,%s,%s,%s)", u)
            for i, op in enumerate(OPERATORS, start=1):
                cur.execute("INSERT INTO charge_operator(id, name) VALUES (%s,%s)", (i, op))
            for t in w.tariffs:
                cur.execute("INSERT INTO tariff(id, operator_id, name, default_price) VALUES (%s,%s,%s,%s)",
                            (t.id, t.operator_id, t.name, t.default_price))
                for a, b, p in t.windows:
                    cur.execute("INSERT INTO tariff_window(tariff_id, start_min, end_min, price) VALUES (%s,%s,%s,%s)",
                                (t.id, a, b, p))
            with cur.copy("COPY charger_station(id, operator_id, name, lat, lon, geohash6, graph_node) FROM STDIN") as cp:
                for st in w.stations:
                    cp.write_row((st.id, st.operator_id, st.name, st.lat, st.lon, st.geohash6, st.node))
            with cur.copy("COPY charger(id, station_id, connector_type, max_kw, tariff_id) FROM STDIN") as cp:
                for c in w.chargers:
                    cp.write_row((c.id, c.station_id, c.connector_type, c.max_kw, c.tariff_id))
            for d in w.depots:
                cur.execute("INSERT INTO depot(id, tenant_id, name, lat, lon, site_cap_kw) VALUES (%s,%s,%s,%s,%s,%s)",
                            (d.id, d.tenant_id, d.name, d.lat, d.lon, d.site_cap_kw))
            for f in w.fleets:
                cur.execute("INSERT INTO fleet(id, tenant_id, name, depot_id) VALUES (%s,%s,%s,%s)",
                            (f.id, f.tenant_id, f.name, f.depot_id))
            with cur.copy("COPY vehicle(vin, tenant_id, fleet_id, model_code, registration, model_year, "
                          "commissioned_on) FROM STDIN") as cp:
                for i, v in enumerate(w.vin):
                    cp.write_row((v, int(w.tenant_id[i]), int(w.fleet_id[i]), w.model_code[i], w.registration[i],
                                  int(w.model_year[i]), w.commissioned[i]))
            with cur.copy("COPY battery_pack(vin, serial, chemistry, manufactured_on, nominal_kwh) FROM STDIN") as cp:
                for i, v in enumerate(w.vin):
                    code = w.model_code[i]
                    cp.write_row((v, f"BP-{v[-8:]}-{i:06d}", CHEMISTRY[code],
                                  w.commissioned[i] - timedelta(days=60), MODELS_BY_CODE[code].capacity_kwh))
            with cur.copy("COPY driver(id, tenant_id, full_name, email, phone, license_no) FROM STDIN") as cp:
                for d in w.drivers:
                    cp.write_row(d)
            with cur.copy("COPY driver_assignment(vin, driver_id, from_ts) FROM STDIN") as cp:
                for vin, did in w.assignments:
                    cp.write_row((vin, did, TODAY - timedelta(days=90)))
            for tbl in ("tenant", "charge_operator", "tariff", "charger_station", "charger", "depot", "fleet", "driver",
                        "app_user"):
                cur.execute(f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), (SELECT max(id) FROM {tbl}))")
            # OEM webhook clients (client-credentials) - secret = the OEM HMAC secret in dev.
            for oem, secret in s.oem_secret_map.items():
                cur.execute("INSERT INTO api_client(client_id, tenant_id, secret_hash, role_code, description) "
                            "VALUES (%s, NULL, %s, 'oem_ingest', %s)",
                            (f"oem-{oem}", bcrypt.hashpw(secret.encode(), bcrypt.gensalt(rounds=10)).decode(),
                             f"{oem} cloud webhook"))
        conn.commit()
        conn.execute("ANALYZE")
    log.info("seed complete")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    seed(force=args.force)
