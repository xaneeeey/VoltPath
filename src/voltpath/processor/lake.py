"""Lake writer: full-fidelity telemetry -> Parquet on S3 (cold tier / batch analytics / replay).

Separate consumer group (`lake`) from the processor so a slow object store never delays alerts,
and either side can be replayed independently. Files are Hive-partitioned
  s3://<bucket>/telemetry/dt=YYYY-MM-DD/hour=HH/p<partition>-<first_offset>.parquet
Naming by (partition, first offset) makes a replayed flush overwrite the same object. Offsets
are committed only after the object is durably written (at-least-once; batch queries dedup
on (vin, seq)).
"""

from __future__ import annotations

import logging
import signal
import time
from collections import defaultdict
from datetime import datetime

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from confluent_kafka import Consumer, TopicPartition
from prometheus_client import Counter, Histogram, start_http_server

from voltpath.config import settings, setup_logging
from voltpath.infra import s3fs

ROWS = Counter("lake_rows_total", "rows written to parquet")
FILES = Counter("lake_files_total", "parquet objects written")
FLUSH = Histogram("lake_flush_seconds", "flush duration", buckets=(.1, .5, 1, 2, 5, 10, 30))

SCHEMA = pa.schema([
    ("ts", pa.timestamp("ms", tz="UTC")), ("vin", pa.string()), ("oem", pa.string()), ("seq", pa.int64()),
    ("lat", pa.float64()), ("lon", pa.float64()), ("speed_kmh", pa.float32()), ("soc_pct", pa.float32()),
    ("soh_pct", pa.float32()), ("batt_temp_c", pa.float32()), ("odo_km", pa.float64()), ("power_kw", pa.float32()),
    ("evt", pa.string()), ("dtc", pa.list_(pa.string())), ("charger_id", pa.int32()),
])
FLUSH_EVERY_S = 60
FLUSH_ROWS = 250_000


class LakeWriter:
    def __init__(self):
        self.s = settings()
        self.log = logging.getLogger("lake")
        self.fs = s3fs()
        self.consumer = Consumer({
            "bootstrap.servers": self.s.kafka_bootstrap, "group.id": "lake", "enable.auto.commit": False,
            "auto.offset.reset": "earliest", "partition.assignment.strategy": "cooperative-sticky",
            "max.poll.interval.ms": 300_000,
        })
        self.buf: dict[int, list[dict]] = defaultdict(list)
        self.first_offset: dict[int, int] = {}
        self.last_offset: dict[int, int] = {}
        self.running = True

    def run(self):
        self.consumer.subscribe([self.s.topic_raw], on_revoke=lambda c, parts: self.flush())
        last = time.time()
        while self.running:
            for m in self.consumer.consume(num_messages=10_000, timeout=0.5):
                if m.error():
                    continue
                p = m.partition()
                self.first_offset.setdefault(p, m.offset())
                self.last_offset[p] = m.offset()
                self.buf[p].append(orjson.loads(m.value()))
            size = sum(len(b) for b in self.buf.values())
            if size >= FLUSH_ROWS or (size and time.time() - last >= FLUSH_EVERY_S):
                self.flush()
                last = time.time()
        self.flush()
        self.consumer.close()

    def flush(self):
        if not any(self.buf.values()):
            return
        t0 = time.perf_counter()
        commits = []
        for p, rows in list(self.buf.items()):
            if not rows:
                continue
            groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
            for r in rows:
                ts = datetime.fromisoformat(r["ts"])
                r["ts"] = ts
                groups[(ts.strftime("%Y-%m-%d"), ts.hour)].append(r)
            for (dt, hour), grp in groups.items():
                table = pa.Table.from_pylist([{k: g.get(k) for k in SCHEMA.names} for g in grp], schema=SCHEMA)
                path = f"{self.s.lake_bucket}/telemetry/dt={dt}/hour={hour:02d}/p{p:02d}-{self.first_offset[p]}.parquet"
                pq.write_table(table, path, filesystem=self.fs, compression="zstd")
                FILES.inc()
                ROWS.inc(len(grp))
            commits.append(TopicPartition(self.s.topic_raw, p, self.last_offset[p] + 1))
        if commits:
            self.consumer.commit(offsets=commits, asynchronous=False)
        self.buf.clear()
        self.first_offset.clear()
        FLUSH.observe(time.perf_counter() - t0)
        self.log.info("flushed %d partitions to the lake in %.2fs", len(commits), time.perf_counter() - t0)


def main():
    setup_logging("lake")
    start_http_server(settings().metrics_port)
    w = LakeWriter()
    signal.signal(signal.SIGTERM, lambda *_: setattr(w, "running", False))
    w.run()


if __name__ == "__main__":
    main()
