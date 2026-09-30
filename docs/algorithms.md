# Algorithms and data structures

All implementations live in `src/voltcore/` (pure Python + NumPy, no framework), are unit-tested with ≥ 99 % line coverage (`tests/unit`), and are benchmarked by `make bench` → `docs/bench.json` (M-series laptop, stack running).

| Area | Where used | Algorithm | Complexity | Measured |
|---|---|---|---|---|
| **Graph: nearest reachable charger** | Range alert (stream), `/reachable-chargers`, copilot | Multi-target Dijkstra from the vehicle's node, cut off at predicted range, stop after k free chargers | O((V+E) log V) bounded by the range ball; independent of charger count | 0.37 ms median (k=3, 1,200 stations, 45K-node OSM graph) |
| **Graph: point-to-point route** | Route drawn to the chosen charger, simulator trips | A* with haversine heuristic (admissible: road ≥ great-circle; consistent) vs Dijkstra baseline | same worst case, far fewer expansions | **8.0× fewer nodes** (2,004 vs 15,962 median), 12.9 ms vs 55.8 ms |
| **Graph: connected components** | Building the routable graph (drop islands); unapproved-depot detection | Union-find with path halving + union by size; grid-bucketed single-linkage clustering | O(E α(V)); clustering ~O(n) for spread points | OSM: 17 components → keep the 45,373-node giant |
| **DP: charging schedule** | `/charging/plan`, copilot booking, depot scheduling | DP over (15-min slot × energy delivered): SoC-dependent CC-CV taper + convex battery-wear cost, so greedy "cheapest slots" is not optimal | O(T·E·X) time (vectorised over E), O(T·E) back-pointers | 7–40 ms per plan; **37–54 % cheaper** than charging immediately; brute-force equality test on small instances |
| **Depot power cap** | Fleet overnight plan | Earliest-deadline-first over vehicles, each DP within the remaining site allowance | O(n log n + n·DP) | cap never exceeded (property test) |
| **DP: GPS trip/stop segmentation** | Trip reconstruction from noisy speed traces | 2-state HMM, Viterbi | O(N) | 19 ms for a full day at 10 s (8,640 points) |
| **Regex + parsing: VIN** | Ingest validation | `^[A-HJ-NPR-Z0-9]{17}$` + ISO 3779 check digit, LRU-cached (the fleet is finite) | O(1) | 2.7 µs cold, 0.07 µs cached |
| **Regex + parsing: OBD-II DTC** | Every OEM adapter | `\b([PCBU])([0-3])([0-9A-F])([0-9A-F]{2})\b` over lists, `;`/`,` strings or free text; dedup preserving order | O(len) | property tests |
| **Regex: legacy pipe format** | Kestrel adapter | Anchored full-line pattern with typed groups → DLQ reason on mismatch | O(len) | |
| **Streaming: dedup** | Ingest (first line) | Rotating Bloom filter (2 generations, 2M keys, fp 1e-3, k=10, bytearray bits) | O(k) per event, 3.6 MB | 22 µs/add in Python; measured FP 0.0 at 1 % load |
| **Streaming: exact dedup + ordering** | Processor | Per-VIN max-seq + 64-bit sliding bitmap; watermark by event time; sequence-reset detection | O(1) per event, 16 B per vehicle | chaos test: 15,000/15,000 exactly once |
| **Streaming: heavy hitters** | Charging hot-spots (`/analytics/hot-zones`) | Count-Min Sketch (ε=0.002, δ=0.01 → 1360×5) + bounded min-heap top-K, 15-min tumbling window, mergeable across replicas | O(d) per event, O(K) query | top-10 recall 1.0 on a Zipf(1.3) stream |
| **Geo indexing** | Map clustering, privacy masking, station buckets | Geohash encode/decode/neighbours; O(1) integer grid cells for per-2 s clustering of 100K vehicles; Redis GEO (geohash-52 sorted set) for bbox queries | O(precision) / O(1) | 6.1 µs/encode |
| **Vector search** | "Vehicles degrading like this one", KB retrieval | 16-d embedding (smoothed, normalised SoH trajectory + usage profile); pgvector HNSW (m=16, ef_construction=64), cosine | ~O(log n) | |
| **Robust statistics** | Battery anomaly detection | Per-pack SoH regression slope (DuckDB `regr_slope`) → robust z-score within model cohort (median / 1.4826·MAD) | O(n) | **precision 0.973 / recall 0.972** at z>3.5 vs simulator ground truth; 0.996 / 0.971 at z>4 |
| **ML: range prediction** | Vehicle card, range alert, copilot | Histogram gradient boosting on SoC, SoH, temperature, speed, HVAC, model and **recent Wh/km** vs baseline `rated × SoC × SoH` | train O(n·features·iters) | **MAE 6.8 km vs 22.0 km (−69 %)**; baseline over-estimates range 84 % of the time (dangerous), model 52 % |

## Notes and honest caveats
- **Admissibility.** Edge weights are the sum of haversine segment lengths along the road, so the straight-line haversine distance never overestimates. A* therefore returns the same optimal distance as Dijkstra (asserted on random pairs in `test_real_graph_astar_matches_dijkstra`).
- **One-way streets are ignored** (the graph is undirected). The OSM extract keeps motorway→tertiary roads only. Both are documented simplifications. The loader (`tools/build_graph.py`) is where a directed OSM import would go.
- **The ML labels come from our own simulator physics**, so absolute errors are optimistic. The meaningful claim is the *relative* improvement: the model exploits a signal the baseline structurally ignores (a vehicle's own recent consumption, which carries hidden payload and driving-style factors). The same holds for the anomaly-detection precision and recall.
- **The Bloom filter is only the cheap first line of dedup.** It can drop a real event on a false positive (1 in 1,000 per replica at full load). Exact dedup is the processor's seq window, and storage is idempotent by primary key.
