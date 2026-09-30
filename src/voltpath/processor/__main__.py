"""Stream processor: Kafka `telemetry.raw` -> hot state (Redis), alerts, warm store (Timescale),
trips & charging sessions (Postgres). Consumer group `processor`; scale by adding replicas
(up to the partition count).

Delivery semantics: at-least-once consumption + idempotent sinks = effectively-once:
  - offsets committed only after every sink for the batch succeeded
  - telemetry  : PK (vin, ts) + ON CONFLICT DO NOTHING
  - sessions   : UNIQUE (vin, started_at) + ON CONFLICT DO NOTHING
  - alerts     : Redis SET NX cooldown key per (type, vin)
  - hot state  : last-writer-wins guarded by event time; partial aggregates recomputed, not incremented
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from collections import defaultdict

import orjson
import psycopg
import redis
from confluent_kafka import Consumer, KafkaError, TopicPartition
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from voltcore.charging_dp import TariffWindow, price_at
from voltcore.energy import MODELS_BY_CODE
from voltcore.graph import RoadGraph
from voltcore.schema import parse_ts
from voltcore.sketches import CountMinSketch
from voltpath.config import settings, setup_logging
from voltpath.processor.rules import (
    COOLDOWN_S,
    RANGE_CHECK_EVERY_S,
    RANGE_CHECK_SOC,
    Alert,
    range_rule,
    simple_rules,
)
from voltpath.processor.state import DUPLICATE, NEW, TOO_LATE, VehicleState, cell_key

PROCESSED = Counter("processor_events_total", "events by outcome", ["result"])
AGE = Histogram("processor_event_age_seconds", "ingest-to-state latency (now - event ts)",
                buckets=(.1, .25, .5, 1, 2, 5, 10, 30, 60, 300))
ALERT_LAT = Histogram("processor_alert_latency_seconds", "event ts -> alert published",
                      buckets=(.25, .5, 1, 2, 3, 5, 10, 30))
ALERTS = Counter("processor_alerts_total", "alerts raised", ["type"])
BATCH = Histogram("processor_batch_seconds", "batch processing time", buckets=(.01, .05, .1, .25, .5, 1, 2))
LAG = Gauge("processor_consumer_lag", "messages behind high watermark", ["partition"])
STORED = Counter("processor_rows_stored_total", "rows written", ["table"])
TELEMETRY_REPLAY = Counter("processor_telemetry_replay_batches_total", "batches that hit the duplicate-key slow path")
PHASE = Histogram("processor_phase_seconds", "time per pipeline phase", ["phase"],
                  buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2))
# pre-bound children: label lookup costs ~4 us, too much per event at 100K ev/s
C_APPLIED, C_DUP, C_LATE = (PROCESSED.labels(x) for x in ("applied", "duplicate", "late"))
PH_PROCESS, PH_REDIS, PH_PG = (PHASE.labels(x) for x in ("process", "redis", "postgres"))

OFFLINE_S = 180
WARM_EVERY_S = 30
STATE_FLUSH_S = 2.0


class Reference:
    """Master data cached in memory (refreshed on start; 100K rows ~ 20 MB)."""

    def __init__(self, conn: psycopg.Connection):
        self.vehicle = {r[0]: (r[1], MODELS_BY_CODE[r[2]]) for r in
                        conn.execute("SELECT vin, tenant_id, model_code FROM vehicle")}
        self.driver = dict(conn.execute("SELECT vin, driver_id FROM driver_assignment WHERE to_ts IS NULL"))
        windows: dict[int, list[TariffWindow]] = defaultdict(list)
        for tid, a, b, p in conn.execute("SELECT tariff_id, start_min, end_min, price FROM tariff_window"):
            windows[tid].append(TariffWindow(a, b, float(p)))
        self.tariff_default = {r[0]: float(r[1]) for r in conn.execute("SELECT id, default_price FROM tariff")}
        self.charger = {}
        for cid, sid, tid, kw in conn.execute("SELECT id, station_id, tariff_id, max_kw FROM charger"):
            self.charger[cid] = (sid, windows[tid], self.tariff_default[tid], float(kw))
        self.station_node = dict(conn.execute("SELECT id, graph_node FROM charger_station"))
        self.station_cap = dict(conn.execute("SELECT station_id, count(*) FROM charger GROUP BY station_id"))

    def price(self, charger_id: int, ts: float) -> float:
        _sid, windows, default, _kw = self.charger[charger_id]
        minute_ist = int((ts / 60 + 330) % 1440)
        return price_at(windows, minute_ist, default)


class Processor:
    def __init__(self):
        self.s = settings()
        self.log = logging.getLogger("processor")
        self.pg = psycopg.connect(self.s.database_url)
        self.ref = Reference(self.pg)
        self.pg.commit()
        self.r = redis.Redis.from_url(self.s.redis_url)
        self.graph = RoadGraph.load(self.s.graph_path)
        self.states: dict[int, dict[str, VehicleState]] = defaultdict(dict)
        self.cms = CountMinSketch(eps=0.002, delta=0.01, top_k=15)
        self.cms_reset = time.time()
        self.free_nodes: dict[int, int] = {n: sid for sid, n in self.ref.station_node.items()}
        self.last_agg = 0.0
        self.consumer: Consumer | None = None   # created in run(): process() is usable without a broker
        self.running = True
        with self.pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE tstage (LIKE telemetry INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
        self.pg.commit()

    # ------------------------------------------------------------------ rebalance
    # State is restored lazily: the first time this owner sees a VIN it HMGETs the checkpoint
    # the previous owner left in `vs:{tenant}` (see _restore), so rebalances are instant.
    def on_assign(self, consumer, partitions):
        self.log.info("assigned partitions %s", [tp.partition for tp in partitions])
        consumer.incremental_assign(partitions)

    def on_revoke(self, consumer, partitions):
        for tp in partitions:
            self.states.pop(tp.partition, None)
            try:
                LAG.remove(str(tp.partition))
            except KeyError:
                pass
        consumer.incremental_unassign(partitions)

    def _restore(self, missing: dict[int, list[tuple[int, str]]]) -> None:
        """missing: tenant -> [(partition, vin)] not yet in memory."""
        for tenant, items in missing.items():
            raws = self.r.hmget(f"vs:{tenant}", [v for _, v in items])
            for (p, vin), raw in zip(items, raws, strict=True):
                st = (VehicleState.from_checkpoint({**orjson.loads(raw), "vin": vin, "tenant_id": tenant}) if raw
                      else VehicleState(vin, tenant))
                self.states[p][vin] = st

    # ------------------------------------------------------------------ main loop
    def run(self):
        self.consumer = Consumer({
            "bootstrap.servers": self.s.kafka_bootstrap,
            "group.id": "processor",
            "client.id": socket.gethostname(),
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "partition.assignment.strategy": "cooperative-sticky",
            "fetch.min.bytes": 1,
            "max.poll.interval.ms": 120_000,
        })
        self.consumer.subscribe([self.s.topic_raw], on_assign=self.on_assign, on_revoke=self.on_revoke)
        last_lag = 0.0
        while self.running:
            msgs = self.consumer.consume(num_messages=5000, timeout=0.25)
            if msgs:
                t0 = time.perf_counter()
                self.process(msgs)
                self.consumer.commit(asynchronous=False)
                BATCH.observe(time.perf_counter() - t0)
            now = time.time()
            if now - self.last_agg >= STATE_FLUSH_S:
                self.publish_aggregates(now)
                self.last_agg = now
            if now - last_lag >= 5:
                self.report_lag()
                last_lag = now
        self.consumer.close()

    def process(self, msgs):
        now = time.time()
        t_start = time.perf_counter()
        rows, alerts, trips, sessions = [], [], [], []
        touched: dict[str, VehicleState] = {}                   # coalesced: one Redis write per VIN
        n_dup = n_late = n_applied = n_unknown = 0
        decoded = []
        missing: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for m in msgs:
            if m.error():
                if m.error().code() != KafkaError._PARTITION_EOF:  # noqa: SLF001
                    self.log.error("consume error: %s", m.error())
                continue
            e = orjson.loads(m.value())
            ref = self.ref.vehicle.get(e["vin"])
            if ref is None:
                n_unknown += 1
                continue
            p = m.partition()
            if e["vin"] not in self.states[p]:
                missing[ref[0]].append((p, e["vin"]))
                self.states[p][e["vin"]] = None  # placeholder, filled by _restore
            decoded.append((p, e, ref))
        if missing:
            self._restore(missing)
        for p, e, (tenant_id, model) in decoded:
            vin = e["vin"]
            st = self.states[p][vin]
            ts = parse_ts(e["ts"]).timestamp()
            verdict = st.check_seq(e["seq"], ts)
            if verdict == DUPLICATE:
                n_dup += 1
                continue
            in_order = verdict == NEW and ts >= st.ts
            # warm store: downsample periodic events to 1 per 30 s per vehicle; keep all lifecycle events
            if e["evt"] != "PERIODIC" or ts - st.stored_ts >= WARM_EVERY_S or not in_order:
                rows.append((e["ts"], vin, tenant_id, e["lat"], e["lon"], e["speed_kmh"], e["soc_pct"], e["soh_pct"],
                             e["batt_temp_c"], e["odo_km"], e["power_kw"], e["evt"]))
                if in_order:
                    st.stored_ts = ts
            if verdict == TOO_LATE or not in_order:
                n_late += 1
                touched[vin] = st  # dedup window changed - checkpoint it
                continue
            facts = st.apply(e, ts)
            n_applied += 1
            if n_applied % 8 == 0:
                AGE.observe(max(0.0, now - ts))   # sampled: the histogram shape is what matters
            touched[vin] = st
            for kind, f in facts:
                (trips if kind == "trip" else sessions).append((st, f))
            if e["evt"] == "CHARGE_START" and e.get("charger_id") in self.ref.charger:
                sid = self.ref.charger[e["charger_id"]][0]
                self.cms.add(str(sid))
            # ---- alerts (fresh events only)
            if now - ts < 120:
                alerts.extend(simple_rules(vin, tenant_id, e, st.status, ts))
                if (st.status == "DRIVING" and e["soc_pct"] < RANGE_CHECK_SOC
                        and ts - st.last_range_check >= RANGE_CHECK_EVERY_S):
                    st.last_range_check = ts
                    a = range_rule(vin, tenant_id, e, ts, model, self.graph, self.free_nodes)
                    if a:
                        alerts.append(a)
        C_APPLIED.inc(n_applied)
        C_DUP.inc(n_dup)
        C_LATE.inc(n_late)
        if n_unknown:
            PROCESSED.labels("unknown_vin").inc(n_unknown)
        PH_PROCESS.observe(time.perf_counter() - t_start)
        self.flush(rows, touched, trips, sessions, alerts)

    # ------------------------------------------------------------------ sinks
    def flush(self, rows, touched: dict[str, VehicleState], trips, sessions, alerts):
        t0 = time.perf_counter()
        states: dict[int, dict[str, bytes]] = defaultdict(dict)
        geo: dict[int, list] = defaultdict(list)
        for vin, st in touched.items():
            states[st.tenant_id][vin] = orjson.dumps(st.to_checkpoint())
            if st.moved():
                geo[st.tenant_id].extend((st.lon, st.lat, vin))
        pipe = self.r.pipeline(transaction=False)
        for tenant, m in states.items():
            pipe.hset(f"vs:{tenant}", mapping=m)       # checkpoint == live state (API projects public fields)
        for tenant, g in geo.items():
            pipe.geoadd(f"geo:{tenant}", g)
        pipe.execute()
        PH_REDIS.observe(time.perf_counter() - t0)
        t0 = time.perf_counter()

        # cooldown dedup for alerts (idempotent across replays / replicas)
        fired: list[Alert] = []
        if alerts:
            pipe = self.r.pipeline(transaction=False)
            for a in alerts:
                pipe.set(f"cd:{a.type_code}:{a.vin}", 1, nx=True, ex=COOLDOWN_S.get(a.type_code, 1800))
            fired = [a for a, ok in zip(alerts, pipe.execute(), strict=True) if ok]

        if rows:
            self.write_telemetry(rows)
        with self.pg.cursor() as cur:
            for st, f in trips:
                s, e = f["start"], f["end"]
                km = max(0.0, e["odo"] - s["odo"])
                model = self.ref.vehicle[st.vin][1]
                kwh = max(0.0, (s["soc"] - e["soc"]) / 100 * model.capacity_kwh * st.soh / 100)
                cur.execute("INSERT INTO trip(tenant_id, vin, driver_id, started_at, ended_at, distance_km, energy_kwh, "
                            "start_soc, end_soc) VALUES (%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s,%s,%s)",
                            (st.tenant_id, st.vin, self.ref.driver.get(st.vin), s["ts"], e["ts"], round(km, 2),
                             round(kwh, 3), round(s["soc"], 2), round(e["soc"], 2)))
            STORED.labels("trip").inc(len(trips))
            for st, f in sessions:
                s, e = f["start"], f["end"]
                cid = s.get("charger_id")
                if cid not in self.ref.charger:
                    continue
                model = self.ref.vehicle[st.vin][1]
                kwh = max(0.0, (e["soc"] - s["soc"]) / 100 * model.capacity_kwh * st.soh / 100) / 0.92
                cost = kwh * self.ref.price(cid, s["ts"])
                cur.execute("INSERT INTO charging_session(tenant_id, vin, charger_id, started_at, ended_at, start_soc, "
                            "end_soc, energy_kwh, cost) VALUES (%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s,%s,%s) "
                            "ON CONFLICT (vin, started_at) DO NOTHING",
                            (st.tenant_id, st.vin, cid, s["ts"], e["ts"], round(s["soc"], 2), round(e["soc"], 2),
                             round(kwh, 3), round(cost, 2)))
            STORED.labels("charging_session").inc(len(sessions))
            published = []
            for a in fired:
                cur.execute("INSERT INTO alert(tenant_id, vin, type_code, message, event_ts, details) "
                            "VALUES (%s,%s,%s,%s,to_timestamp(%s),%s) RETURNING id, created_at",
                            (a.tenant_id, a.vin, a.type_code, a.message, a.event_ts, orjson.dumps(a.details).decode()))
                aid, created = cur.fetchone()
                published.append((a, aid, created))
        self.pg.commit()
        PH_PG.observe(time.perf_counter() - t0)
        if published:
            pipe = self.r.pipeline(transaction=False)
            for a, aid, created in published:
                pipe.publish(f"alerts:{a.tenant_id}", orjson.dumps({
                    "id": aid, "vin": a.vin, "type": a.type_code, "message": a.message, "event_ts": a.event_ts,
                    "created_at": created.isoformat(), "details": a.details}))
                ALERTS.labels(a.type_code).inc()
                ALERT_LAT.observe(max(0.0, time.time() - a.event_ts))
            pipe.execute()

    TCOLS = "ts, vin, tenant_id, lat, lon, speed_kmh, soc_pct, soh_pct, batt_temp_c, odo_km, power_kw, evt"

    def write_telemetry(self, rows) -> None:
        """Optimistic fast path: COPY straight into the hypertable (one round trip, no index probe per
        row for conflict handling). A replay after a crash hits the (vin, ts) primary key; only then
        fall back to staging + INSERT ... ON CONFLICT DO NOTHING. Same end state, ~5x cheaper normally."""
        try:
            with self.pg.cursor() as cur, cur.copy(f"COPY telemetry ({self.TCOLS}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
            self.pg.commit()
            STORED.labels("telemetry").inc(len(rows))
            return
        except psycopg.errors.UniqueViolation:
            self.pg.rollback()
            TELEMETRY_REPLAY.inc()
        with self.pg.cursor() as cur:
            with cur.copy(f"COPY tstage ({self.TCOLS}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
            cur.execute("INSERT INTO telemetry SELECT * FROM tstage ON CONFLICT DO NOTHING")
            STORED.labels("telemetry").inc(cur.rowcount)
        self.pg.commit()

    # ------------------------------------------------------------------ periodic aggregates
    def publish_aggregates(self, now: float):
        t_agg = time.perf_counter()
        self._publish_aggregates(now)
        PHASE.labels("aggregates").observe(time.perf_counter() - t_agg)

    def _publish_aggregates(self, now: float):
        """Per-partition partial KPIs, map cells and charger occupancy, recomputed from local state
        (idempotent: a new owner simply overwrites the partition's keys)."""
        pipe = self.r.pipeline(transaction=False)
        for p, states in self.states.items():
            kpi: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            cells: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))
            occ: dict[int, int] = defaultdict(int)
            for st in states.values():
                if st is None:
                    continue
                k = kpi[st.tenant_id]
                status = st.status if now - st.ts < OFFLINE_S else "OFFLINE"
                k[status] += 1
                k["n"] += 1
                if status != "OFFLINE":
                    k["soc_sum"] += st.soc
                    k["online"] += 1
                    if st.soc < 20:
                        k["low_soc"] += 1
                    if st.power > 0:
                        k["charging_kw"] += st.power
                    c = cells[st.tenant_id][cell_key(st.lat, st.lon)]
                    c[0] += 1
                    c[1] += st.soc
                    c[2] += st.soc < 20
                if status == "CHARGING" and st.charger_id in self.ref.charger:
                    occ[self.ref.charger[st.charger_id][0]] += 1
            for tenant, k in kpi.items():
                pipe.delete(f"kpi:{tenant}:{p}")
                pipe.hset(f"kpi:{tenant}:{p}", mapping=dict(k))
                pipe.expire(f"kpi:{tenant}:{p}", 30)
            for tenant, cs in cells.items():
                pipe.delete(f"cells:{tenant}:{p}")
                pipe.hset(f"cells:{tenant}:{p}", mapping={g: f"{v[0]}|{v[1]:.1f}|{v[2]}" for g, v in cs.items()})
                pipe.expire(f"cells:{tenant}:{p}", 30)
            pipe.delete(f"occ:{p}")
            if occ:
                pipe.hset(f"occ:{p}", mapping=occ)
            pipe.expire(f"occ:{p}", 30)
        # charging hot-spots (Count-Min top-K over a 15-minute tumbling window)
        pipe.set(f"hot:{socket.gethostname()}", orjson.dumps(self.cms.topk()), ex=900)
        if now - self.cms_reset > 900:
            self.cms = CountMinSketch(eps=0.002, delta=0.01, top_k=15)
            self.cms_reset = now
        pipe.execute()
        # refresh free-charger view (all partitions) for the range rule
        occupied: dict[int, int] = defaultdict(int)
        for key in self.r.scan_iter("occ:*", count=100):
            for sid, n in self.r.hgetall(key).items():
                occupied[int(sid)] += int(n)
        self.free_nodes = {node: sid for sid, node in self.ref.station_node.items()
                           if occupied.get(sid, 0) < self.ref.station_cap.get(sid, 1)}

    def report_lag(self):
        try:
            for tp in self.consumer.assignment():
                lo, hi = self.consumer.get_watermark_offsets(tp, timeout=1, cached=True)
                pos = self.consumer.position([TopicPartition(tp.topic, tp.partition)])[0].offset
                if pos >= 0:
                    LAG.labels(str(tp.partition)).set(max(0, hi - pos))
        except Exception as exc:  # noqa: BLE001 - metrics must never kill the loop
            self.log.debug("lag report failed: %s", exc)


def main():
    setup_logging("processor")
    start_http_server(settings().metrics_port)
    p = Processor()
    signal.signal(signal.SIGTERM, lambda *_: setattr(p, "running", False))
    p.log.info("processor started (%s)", os.environ.get("HOSTNAME", socket.gethostname()))
    p.run()


if __name__ == "__main__":
    main()

