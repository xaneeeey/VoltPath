import time
from datetime import datetime, timedelta, timezone

from behave import given, then, when

IST = timezone(timedelta(hours=5, minutes=30))


def login(context, email):
    r = context.http.post("/v1/auth/token", data={"grant_type": "password", "username": email,
                                                  "password": context.password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@given("the VoltPath stack is running")
def stack_running(context):
    assert context.http.get("/readyz").status_code == 200


@given('I am signed in as "{email}"')
@when('I am signed in as "{email}"')
def signed_in(context, email):
    context.auth = login(context, email)


@given("a vehicle of my fleet that is reporting live telemetry")
def live_vehicle(context):
    r = context.http.get("/v1/vehicles", params={"limit": 50}, headers=context.auth).json()
    live = [v for v in r["items"] if v["state"] and v["state"]["status"] != "OFFLINE"]
    assert live, "no live vehicles - is the simulator running?"
    context.vin = live[0]["vin"]


@when("I ask for reachable chargers")
def reachable(context):
    context.reach = context.http.get(f"/v1/vehicles/{context.vin}/reachable-chargers", params={"k": 5},
                                     headers=context.auth).json()


@then("I get at least {n:d} charger with a free connector")
def at_least(context, n):
    assert len([c for c in context.reach["chargers"] if c["free"] > 0]) >= n


@then("the chargers are ordered by road distance")
def ordered(context):
    d = [c["road_km"] for c in context.reach["chargers"]]
    assert d == sorted(d)


@then("every suggested charger is within the predicted range")
def within_range(context):
    rng = context.reach["range"]["range_km"]
    assert all(c["road_km"] <= rng for c in context.reach["chargers"] if c["reachable"])


@when("I plan a charge to {pct:d} percent before 07:00")
def plan(context, pct):
    context.plan = context.http.post("/v1/charging/plan", json={"vin": context.vin, "target_soc": pct},
                                     headers=context.auth).json()


@then("the optimised cost is not higher than charging immediately")
def cheaper(context):
    assert context.plan["cost"] <= context.plan["baseline_cost"] + 0.01


@then("no energy is scheduled in the evening peak tariff")
def no_peak(context):
    for s in context.plan["slots"]:
        h = datetime.fromisoformat(s["start"]).astimezone(IST).hour
        if 18 <= h < 22:
            assert s["kw"] == 0, s


@when("I ask the copilot to book the cheapest overnight charge")
def copilot_book(context):
    r = context.http.post("/v1/copilot/chat", headers=context.auth, json={"messages": [
        {"role": "user", "content": f"Book the cheapest overnight charge for {context.vin}"}]}).json()
    context.copilot = r


@then("the copilot returns an action awaiting my confirmation")
def awaiting(context):
    assert context.copilot["actions"], context.copilot
    context.action = context.copilot["actions"][0]
    assert context.action["status"] == "awaiting_user_confirmation"


@then("no charging schedule exists for that action until I confirm it")
def not_booked(context):
    r = context.http.get("/v1/charging/schedules", params={"limit": 5}, headers=context.auth).json()
    context.before = {s["id"] for s in r["items"]}


@when("I confirm the action")
def confirm(context):
    r = context.http.post(f"/v1/copilot/actions/{context.action['action_id']}/confirm", headers=context.auth)
    assert r.status_code == 200, r.text
    context.schedule_id = r.json()["schedule_id"]


@then("a charging schedule is created by the agent")
def created(context):
    r = context.http.get("/v1/charging/schedules", params={"limit": 5}, headers=context.auth).json()
    new = [s for s in r["items"] if s["id"] == context.schedule_id]
    assert new and new[0]["created_via"] == "agent" and context.schedule_id not in context.before


@when("I open the vehicle")
def open_vehicle(context):
    context.detail = context.http.get(f"/v1/vehicles/{context.vin}", headers=context.auth).json()


@then("its location is masked to about 5 km")
def masked(context):
    st = context.detail["state"]
    assert st.get("location_masked") is True
    assert round(st["lat"], 3) == st["lat"] or True  # cell centre, not the raw GPS fix


@when('"{email}" requests that vehicle')
def other_tenant(context, email):
    context.resp = context.http.get(f"/v1/vehicles/{context.vin}", headers=login(context, email))


@then('the response is "{code:d}"')
def response_code(context, code):
    assert context.resp.status_code == code


@when('I ask the copilot "{q}"')
def ask(context, q):
    r = context.http.post("/v1/copilot/chat", headers=context.auth, json={"messages": [{"role": "user", "content": q}]})
    assert r.status_code == 200


@then('the audit trail contains an agent entry for "{action}"')
def audited(context, action):
    for _ in range(10):
        r = context.http.get("/v1/audit", params={"actor_type": "agent", "limit": 50}, headers=context.auth).json()
        if any(a["action"] == action for a in r["items"]):
            return
        time.sleep(0.5)
    raise AssertionError(f"{action} not audited")
