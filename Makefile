SHELL := /bin/bash
COMPOSE := docker compose
PY := uv run

.PHONY: help up down restart logs ps init graph test test-unit test-int test-contract test-bdd cov lint \
        load load-k6 soak chaos sec explain web-dev bench batch train clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  %-14s %s\n", $$1, $$2}'

up:              ## build + start the whole stack (migrate, seed 100K vehicles, simulate, serve UI on :8080)
	$(COMPOSE) up -d --build
	@echo "UI  http://localhost:8080   API docs http://localhost:8000/docs   Grafana http://localhost:3000   Redpanda console http://localhost:8081"

down:            ## stop the stack (keeps data volumes)
	$(COMPOSE) down

clean:           ## stop and delete all data volumes
	$(COMPOSE) down -v

restart:         ## rebuild + restart app services only
	$(COMPOSE) up -d --build ingest processor lake-writer simulator api web batch

logs:            ## tail app logs
	$(COMPOSE) logs -f --tail=50 ingest processor api simulator

ps:
	$(COMPOSE) ps

graph:           ## re-download OSM roads and rebuild data/bangalore_graph.npz
	$(PY) python tools/build_graph.py --fetch

batch:           ## run all batch jobs now (history, rollup, forecast, energy, model)
	$(COMPOSE) exec batch python -m voltpath.batch all

train:           ## retrain the range model and print metrics
	$(COMPOSE) exec batch python -m voltpath.batch train

# ------------------------------------------------------------------ quality
lint:            ## ruff + typecheck web
	$(PY) ruff check src tests tools
	cd web && npx tsc -b

test: test-unit test-contract test-int test-bdd   ## every automated suite

test-unit:       ## fast unit tests with coverage (no docker)
	$(PY) pytest tests/unit --cov --cov-report=term --cov-report=xml --cov-report=html -p no:warnings

test-contract:   ## consumer/provider contract tests (event schema + API)
	$(PY) pytest tests/contract -p no:warnings

test-int:        ## integration tests on real Postgres/Timescale, Redis, Redpanda (Testcontainers)
	$(PY) pytest tests/integration -m integration -p no:warnings

test-bdd:        ## BDD acceptance scenarios against the running stack (make up first)
	cd tests/bdd && $(PY) behave --no-capture

cov: test-unit   ## coverage report -> htmlcov/index.html

# ------------------------------------------------------------------ performance / resilience
load:            ## 100K ev/s ingest load for 2 min (+3x burst), reports throughput, p95/p99 and lag
	$(PY) python tests/load/ingest_load.py --rate 100000 --duration 120 --burst 3

load-k6:         ## same scenario with k6 (docker)
	docker run --rm -i --network host -v $(PWD)/tests/load:/scripts grafana/k6 run /scripts/ingest.js

soak:            ## 30-minute soak at 30K ev/s
	$(PY) python tests/load/ingest_load.py --rate 30000 --duration 1800

chaos:           ## kill broker + a processor mid-stream and prove recovery without data loss
	$(PY) python tests/chaos/kill_and_recover.py

sec:             ## SAST (semgrep), dependency + image scan (trivy), DAST baseline (ZAP)
	./tests/security/run_scans.sh

explain:         ## EXPLAIN ANALYZE before/after for the slowest queries -> db/explain/
	$(PY) python db/explain/run_explain.py

bench:           ## algorithm micro-benchmarks (A* vs Dijkstra, DP, sketches)
	$(PY) python tools/bench_algorithms.py

web-dev:         ## Vite dev server with API proxy
	cd web && npm run dev
