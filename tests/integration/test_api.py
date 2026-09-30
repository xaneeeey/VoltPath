"""API against real Postgres/Redis: OAuth2 flows, RBAC, tenant isolation, masking, pagination,
charging plan, copilot (offline mode) with two-phase actions, audit trail and erasure."""

import time

import orjson
import pytest
import redis
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
PW = "voltpath-demo"


@pytest.fixture(scope="module")
def api(stack, world):
    from voltpath.api.app import create_app

    # live state for a few tenant-1 and tenant-2 vehicles
    rd = redis.Redis.from_url(stack["REDIS_URL"])
    now = time.time()
    for i in (0, 1, 2):
        vin = world.vin[i]
        rd.hset(f"vs:{int(world.tenant_id[i])}", vin, orjson.dumps({
            "vin": vin, "tenant_id": int(world.tenant_id[i]), "ts": now, "lat": 12.97161, "lon": 77.59463,
            "speed": 20.0, "soc": 12.0, "soh": 93.0, "temp": 30.0, "power": -4.0, "odo": 1000.0, "status": "DRIVING",
            "charger_id": None, "dtc": ["P0A7E"], "max_seq": 5}))
        rd.geoadd(f"geo:{int(world.tenant_id[i])}", (77.59463, 12.97161, vin))
    with TestClient(create_app()) as c:
        yield c


def token(c, user):
    r = c.post("/v1/auth/token", data={"grant_type": "password", "username": user, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_oauth_flows_and_jwks(api):
    assert api.post("/v1/auth/token", data={"grant_type": "password", "username": "viewer@acme.test",
                                            "password": "wrong"}).status_code == 401
    assert api.post("/v1/auth/token", data={"grant_type": "implicit"}).status_code == 400
    r = api.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": "oem-voltra",
                                         "client_secret": "dev-voltra-secret"})
    assert r.status_code == 200
    jwk = api.get("/.well-known/jwks.json").json()["keys"][0]
    assert jwk["alg"] == "RS256" and jwk["kty"] == "RSA"
    me = api.get("/v1/auth/me", headers=token(api, "analyst@acme.test")).json()
    assert me["role"] == "analyst" and "location:precise" not in me["permissions"]


def test_requires_auth_and_rejects_tampered_tokens(api):
    assert api.get("/v1/fleet/summary").status_code == 401
    h = token(api, "viewer@acme.test")
    bad = {"Authorization": h["Authorization"][:-4] + "AAAA"}
    assert api.get("/v1/fleet/summary", headers=bad).status_code == 401


def test_rbac_matrix(api, world):
    viewer, manager = token(api, "viewer@acme.test"), token(api, "fleetmanager@acme.test")
    vin = world.vin[0]
    assert api.post("/v1/charging/plan", json={"vin": vin}, headers=viewer).status_code == 403
    assert api.get("/v1/audit", headers=manager).status_code == 403
    assert api.delete("/v1/drivers/1/personal-data", headers=manager).status_code == 403
    assert api.post("/v1/charging/plan", json={"vin": vin}, headers=manager).status_code == 200


def test_location_masking_and_tenant_isolation(api, world):
    vin = world.vin[0]
    precise = api.get(f"/v1/vehicles/{vin}", headers=token(api, "fleetmanager@acme.test")).json()["state"]
    masked = api.get(f"/v1/vehicles/{vin}", headers=token(api, "viewer@acme.test")).json()["state"]
    assert precise["lat"] == pytest.approx(12.97161) and "location_masked" not in precise
    assert masked["location_masked"] and masked["lat"] != precise["lat"]
    other_tenant = token(api, "fleetmanager@zenith.test")
    assert api.get(f"/v1/vehicles/{vin}", headers=other_tenant).status_code == 404
    assert api.get("/v1/vehicles/INVALIDVIN", headers=other_tenant).status_code == 422


def test_keyset_pagination_is_complete_and_disjoint(api):
    h = token(api, "analyst@acme.test")
    seen, cursor = [], None
    for _ in range(4):
        r = api.get("/v1/vehicles", params={"limit": 50, **({"cursor": cursor} if cursor else {})}, headers=h).json()
        seen += [i["vin"] for i in r["items"]]
        cursor = r["next_cursor"]
    assert len(seen) == len(set(seen)) == 200 and seen == sorted(seen)
    assert api.get("/v1/vehicles", params={"cursor": "%%%"}, headers=h).status_code == 400


def test_summary_and_map(api):
    h = token(api, "fleetmanager@acme.test")
    s = api.get("/v1/fleet/summary", headers=h).json()
    assert s["vehicles"] > 0
    pts = api.get("/v1/map/vehicles", params={"zoom": 15, "bbox": "77.5,12.9,77.7,13.0"}, headers=h).json()
    assert pts["mode"] == "points" and len(pts["vehicles"]) >= 1
    chargers = api.get("/v1/map/chargers", params={"bbox": "77.55,12.95,77.65,13.0"}, headers=h).json()
    assert chargers["stations"] and all(77.55 <= st["lon"] <= 77.65 for st in chargers["stations"])


def test_reachable_chargers_and_plan(api, world):
    h = token(api, "fleetmanager@acme.test")
    vin = world.vin[0]
    r = api.get(f"/v1/vehicles/{vin}/reachable-chargers", params={"k": 3, "explain": True}, headers=h).json()
    assert r["chargers"] and r["chargers"][0]["road_km"] <= r["chargers"][-1]["road_km"]
    assert r["search"]["astar_expanded"] <= r["search"]["dijkstra_expanded"]
    plan = api.post("/v1/charging/plan", json={"vin": vin, "target_soc": 80}, headers=h).json()
    assert plan["cost"] <= plan["baseline_cost"] and plan["slots"]


def test_copilot_two_phase_action_and_audit(api, world):
    h = token(api, "fleetmanager@acme.test")
    vin = world.vin[0]
    r = api.post("/v1/copilot/chat", headers=h,
                 json={"messages": [{"role": "user", "content": f"Book the cheapest overnight charge for {vin}"}]}).json()
    assert r["mode"] == "rules" and r["actions"], r
    aid = r["actions"][0]["action_id"]
    assert api.post(f"/v1/copilot/actions/{aid}/confirm", headers=token(api, "viewer@acme.test")).status_code == 403
    ok = api.post(f"/v1/copilot/actions/{aid}/confirm", headers=h)
    assert ok.status_code == 200 and ok.json()["schedule_id"] > 0
    assert api.post(f"/v1/copilot/actions/{aid}/confirm", headers=h).status_code == 404   # single use
    time.sleep(1.2)   # async audit writer flushes every 0.5 s
    audit = api.get("/v1/audit", params={"actor_type": "agent"}, headers=token(api, "admin@acme.test")).json()
    assert any(a["action"] == "tool:schedule_charging" for a in audit["items"])


def test_copilot_cannot_escalate(api, world):
    """An analyst's copilot must not be able to book, even when asked to."""
    r = api.post("/v1/copilot/chat", headers=token(api, "analyst@acme.test"),
                 json={"messages": [{"role": "user", "content": f"Book a charge for {world.vin[0]}"}]}).json()
    assert not r["actions"] and not r["tools"][0]["ok"]


def test_kb_retrieval_finds_dtc(api):
    r = api.post("/v1/copilot/chat", headers=token(api, "viewer@acme.test"),
                 json={"messages": [{"role": "user", "content": "What does DTC P0A7E mean?"}]}).json()
    assert "P0A7E" in r["reply"]


def test_security_headers_and_rate_limit_headers(api):
    r = api.get("/v1/fleet/summary", headers=token(api, "viewer@acme.test"))
    for hdr in ("x-content-type-options", "x-frame-options", "strict-transport-security", "x-request-id",
                "x-ratelimit-limit"):
        assert hdr in r.headers


def test_login_rate_limited(api):
    codes = [api.post("/v1/auth/token", data={"grant_type": "password", "username": "x@y.test",
                                              "password": "nope"}).status_code for _ in range(25)]
    assert 429 in codes
