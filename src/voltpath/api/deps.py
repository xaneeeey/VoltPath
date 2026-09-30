"""Shared runtime resources for the API: DB pools, Redis, reference caches, tenant transactions."""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import orjson
import redis.asyncio as aioredis
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from voltcore.graph import RoadGraph
from voltpath.api.auth import Principal
from voltpath.config import settings

log = logging.getLogger("api")


@dataclass
class Station:
    id: int
    name: str
    operator: str
    lat: float
    lon: float
    node: int
    connectors: list[dict]


class Resources:
    pool: AsyncConnectionPool        # app role (RLS enforced)
    svc_pool: AsyncConnectionPool    # owner role: audit writer + auth lookups only
    redis: aioredis.Redis
    graph: RoadGraph
    stations: dict[int, Station]
    station_by_node: dict[int, int]
    charger_station: dict[int, int]
    tariffs: dict[int, tuple[float, list]]
    range_model: object | None = None

    async def start(self):
        s = settings()
        self.pool = AsyncConnectionPool(s.app_database_url, min_size=4, max_size=40, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": False})
        self.svc_pool = AsyncConnectionPool(s.database_url, min_size=1, max_size=5, open=False,
                                            kwargs={"row_factory": dict_row, "autocommit": True})
        await self.pool.open(wait=True, timeout=60)
        await self.svc_pool.open(wait=True, timeout=60)
        self.redis = aioredis.Redis.from_url(s.redis_url)
        self.graph = RoadGraph.load(s.graph_path)
        await self.load_reference()
        self.load_model()

    async def load_reference(self):
        from voltcore.charging_dp import TariffWindow

        async with self.svc_pool.connection() as c:
            st_rows = await (await c.execute(
                "SELECT s.id, s.name, o.name AS operator, s.lat, s.lon, s.graph_node FROM charger_station s "
                "JOIN charge_operator o ON o.id = s.operator_id")).fetchall()
            ch_rows = await (await c.execute("SELECT id, station_id, connector_type, max_kw, tariff_id FROM charger")).fetchall()
            tw = await (await c.execute("SELECT tariff_id, start_min, end_min, price FROM tariff_window")).fetchall()
            tf = await (await c.execute("SELECT id, default_price FROM tariff")).fetchall()
            perms = await (await c.execute("SELECT role_code, permission_code FROM role_permission")).fetchall()
        self.stations = {r["id"]: Station(r["id"], r["name"], r["operator"], r["lat"], r["lon"], r["graph_node"], [])
                         for r in st_rows}
        self.charger_station = {}
        for r in ch_rows:
            self.stations[r["station_id"]].connectors.append(
                {"id": r["id"], "type": r["connector_type"], "max_kw": float(r["max_kw"]), "tariff_id": r["tariff_id"]})
            self.charger_station[r["id"]] = r["station_id"]
        self.station_by_node = {s.node: s.id for s in self.stations.values()}
        windows: dict[int, list] = {}
        for r in tw:
            windows.setdefault(r["tariff_id"], []).append(TariffWindow(r["start_min"], r["end_min"], float(r["price"])))
        self.tariffs = {r["id"]: (float(r["default_price"]), windows.get(r["id"], [])) for r in tf}
        rp: dict[str, set] = {}
        for r in perms:
            rp.setdefault(r["role_code"], set()).add(r["permission_code"])
        self.role_permissions = {k: frozenset(v) for k, v in rp.items()}

    def load_model(self):
        from voltpath.ml import load_range_model

        self.range_model = load_range_model()

    async def stop(self):
        await self.pool.close()
        await self.svc_pool.close()
        await self.redis.aclose()


R = Resources()


@asynccontextmanager
async def tenant_tx(p: Principal):
    """Transaction pinned to the caller's tenant: RLS policies read app.tenant_id."""
    if p.tenant_id is None:
        raise HTTPException(403, "no tenant context")
    async with R.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(p.tenant_id),))
            yield conn


# ------------------------------------------------------------------ keyset cursors
def encode_cursor(*values) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(list(values))).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> list | None:
    if not cursor:
        return None
    try:
        return orjson.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "invalid cursor") from exc
