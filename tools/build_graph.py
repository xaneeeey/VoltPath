"""Build the routable road graph from an OpenStreetMap Overpass export.

    uv run python tools/build_graph.py --fetch            # download (public OSM data, ODbL)
    uv run python tools/build_graph.py --in blr.json      # build data/bangalore_graph.npz

Steps: ways -> consecutive-node edges (haversine metres) -> contract degree-2 chains that are
shorter than 60 m (keeps geometry, drops ~half the nodes) -> keep largest connected component
(union-find) so every node is reachable from every other.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voltcore.geo import haversine_km  # noqa: E402
from voltcore.graph import RoadGraph, connected_components  # noqa: E402

BBOX = (12.85, 77.45, 13.10, 77.75)  # Bengaluru
HW = "motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link"
MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]


def fetch(out: Path) -> None:
    q = f'[out:json][timeout:120];way["highway"~"^({HW})$"]{BBOX};(._;>;);out skel qt;'
    data = urllib.parse.urlencode({"data": q}).encode()
    for url in MIRRORS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "voltpath/1.0",
                                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 - fixed https URLs
                body = r.read()
            json.loads(body)
            out.write_bytes(body)
            print(f"fetched {len(body)/1e6:.1f} MB from {url}")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"mirror {url} failed: {exc}")
    raise SystemExit("all Overpass mirrors failed")


def build(src: Path, dst: Path) -> None:
    osm = json.loads(src.read_text())
    coords = {e["id"]: (e["lat"], e["lon"]) for e in osm["elements"] if e["type"] == "node"}
    ways = [e["nodes"] for e in osm["elements"] if e["type"] == "way"]

    idx: dict[int, int] = {}
    lat, lon = [], []

    def nid(osm_id: int) -> int:
        if osm_id not in idx:
            idx[osm_id] = len(lat)
            la, lo = coords[osm_id]
            lat.append(la)
            lon.append(lo)
        return idx[osm_id]

    edges: dict[tuple[int, int], float] = {}
    for w in ways:
        for a, b in zip(w, w[1:], strict=False):
            if a not in coords or b not in coords or a == b:
                continue
            u, v = nid(a), nid(b)
            key = (min(u, v), max(u, v))
            d = haversine_km(*coords[a], *coords[b]) * 1000
            edges[key] = min(d, edges.get(key, d))

    # Contract short degree-2 chains: node with exactly 2 neighbours and both edges < 60 m.
    adj: dict[int, dict[int, float]] = {}
    for (u, v), d in edges.items():
        adj.setdefault(u, {})[v] = d
        adj.setdefault(v, {})[u] = d
    removed = 0
    for n in list(adj):
        nb = adj.get(n)
        if nb is None or len(nb) != 2:
            continue
        (a, da), (b, db) = nb.items()
        if da > 60 or db > 60 or a == b or b in adj[a]:
            continue
        adj[a][b] = adj[b][a] = da + db
        del adj[a][n], adj[b][n], adj[n]
        removed += 1

    alive = sorted(adj)
    edge_list = [(u, v, d) for u in alive for v, d in adj[u].items() if u < v]
    remap = {old: i for i, old in enumerate(alive)}
    comps = connected_components(len(alive), [(remap[u], remap[v]) for u, v, _ in edge_list])
    keep = set(comps[0])
    print(f"nodes={len(lat)} contracted={removed} alive={len(alive)} components={len(comps)} "
          f"largest={len(keep)}")

    final = {old: i for i, old in enumerate(o for o in alive if remap[o] in keep)}
    flat = [lat[o] for o in final]
    flon = [lon[o] for o in final]
    fedges = [(final[u], final[v], d) for u, v, d in edge_list if u in final and v in final]
    g = RoadGraph.from_edges(flat, flon, fedges)
    dst.parent.mkdir(parents=True, exist_ok=True)
    g.save(dst)
    print(f"graph: V={g.n} E={len(fedges)} -> {dst} ({dst.stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--in", dest="src", default="data/osm_bangalore.json")
    ap.add_argument("--out", default="data/bangalore_graph.npz")
    args = ap.parse_args()
    src = Path(args.src)
    if args.fetch or not src.exists():
        fetch(src)
    build(src, Path(args.out))
