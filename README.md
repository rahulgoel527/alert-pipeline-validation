# Alert Pipeline Lab
> The code is AI-Assisted but design trade-offs are co-authored and reviewed before implementation. 
> This lab is intended for SDET technical assignment evaluation. 

A complete 8-service security alert processing pipeline built for SDET technical assignment. Demonstrates end-to-end alert generation, queuing, deduplication, storage, and observability — with a full test suite covering unit, E2E, and load testing.

---

## Quick Start

```bash
# 1. Register shared git hooks (one-time per clone)
git config core.hooksPath .githooks

# 2. Start the lab
docker compose -f services/docker-compose.yml --profile pipeline up --build -d
docker compose -f services/docker-compose.yml ps

# 3. Wait ~30s, then validate all 8 services
python services/validate_service.py

# 4. Run the test suite
python3 -m venv .venv
source .venv/bin/activate

pip install -r tests/requirements.txt
pytest tests/ -v
```

> The git hook prints a `/update-docs` reminder after any commit that touches `services/` or `tests/`.

---

## Architecture

```mermaid
flowchart LR
    GEN["Generator\n(every 10s)"]
    API["API :8000\nFastAPI"]
    PG[("PostgreSQL\nLedger")]
    RQ[("Redis\nalert_queue")]
    PROC["Processor\n(single-threaded)"]
    ES[("Elasticsearch\n:9200")]
    DASH["Dashboard\n:8050"]
    DEJ["Dejavu\n:1358"]

    GEN -- "POST /api/generate" --> API
    API -- "PRODUCED → QUEUED" --> PG
    API -- "lpush" --> RQ
    RQ -- "BLPOP" --> PROC
    PROC -- "PROCESSING → STORED\nor FAILED\nor DUPLICATE_DROPPED" --> PG
    PROC -- "es.index" --> ES
    PG -- "stats / ledger" --> DASH
    ES -- "search / list" --> API
    ES -- "browse" --> DEJ
```

### Alert State Machine

```mermaid
stateDiagram-v2
    [*] --> PRODUCED
    PRODUCED --> QUEUED
    QUEUED --> PROCESSING
    PROCESSING --> STORED : unique alert, ES write ok
    PROCESSING --> DUPLICATE_DROPPED : fingerprint already in ES
    PROCESSING --> FAILED : ES unavailable or 5% simulated failure
    PROCESSING --> PROCESSING : 2% stuck (reaper fires after 60 min)
```

### Fingerprint Dedup Window

```
sha256(source_ip + alert_type + floor(unix_time / 60))[:16]
         └── same IP + type within 60s → same fingerprint → DUPLICATE_DROPPED
```

---

## Services

| Service | Port | Role |
|---------|------|------|
| redis | — | Alert queue (`alert_queue` list) |
| postgres | 5432 | State ledger — every lifecycle transition |
| elasticsearch | 9200 | Final alert store (searchable, keyword-indexed) |
| api | 8000 | FastAPI: alert factory, REST endpoints, schema owner |
| generator | — | Calls `POST /api/generate` every 10s, 10% duplicate rate |
| processor | — | Consumes Redis, deduplicates, writes to ES |
| dashboard | 8050 | Live pipeline stats, auto-refresh every 5s |
| dejavu | 1358 | Browser-based ES data explorer |

---

## Test Coverage

```mermaid
flowchart TD
    subgraph unit["Unit Tests (44) — no services needed"]
        U1["Fingerprint 60s window contract"]
        U2["generate_alerts() count cap + source prefix"]
        U3["is_duplicate() hit / miss / NotFoundError"]
        U4["process_alert() failure injection\n(2% stuck · 5% fail · 10% slow)"]
        U5["log_ledger() metadata serialisation"]
        U6["Reaper SQL contract + FAILED entry shape"]
    end

    subgraph e2e["E2E Tests (19) — requires running lab"]
        E1["Full pipeline flow\nPRODUCED → QUEUED → STORED in ES"]
        E2["Duplicate detection\nDUPLICATE_DROPPED · ES exclusion · ledger metadata"]
        E3["Failure scenarios\nFAILED state · ES exclusion · pipeline recovery"]
        subgraph slow["Slow Tests (3) — Docker socket required"]
            S1["ES unavailable → FAILED"]
            S2["Redis pause → resume"]
            S3["Burst stability (50 alerts)"]
        end
    end

    subgraph load["Load Tests — Locust (separate from pytest)"]
        L1["POST /api/generate\nP50 < 50ms · P95 < 200ms"]
        L2["GET /api/stats\nP95 < 100ms"]
        L3["429 backpressure\nqueue depth ≥ 200"]
    end
```

### Run Tests

```bash
# Unit only (no lab needed)
pytest tests/unit/ -v

# E2E (lab must be running)
pytest tests/e2e/ -v

# Full suite excluding load
pytest tests/ -v --ignore=tests/load

# Load test (headless)
cd tests/load && locust -f locustfile.py --headless -u 50 -r 5 -t 2m
```

See [tests/Tests.md](tests/Tests.md) for full setup, marker reference, and design tradeoffs.

---

## See Also

- [services/lab_setup.md](services/lab_setup.md) — full architecture, API reference, tradeoffs, known limitations
- [tests/Tests.md](tests/Tests.md) — test prerequisites, markers, load test, parallel execution guide
- [API docs](http://localhost:8000/docs) — auto-generated OpenAPI docs (when lab is running)
