# Alert Pipeline Lab — Setup Guide
> The code is AI-Assisted but design trade-offs are co-authored by me and reviewed before implementation. 
> This lab is intended for SDET technical assignment evaluation. 

## Architecture Overview

```
┌──────────────┐   HTTP POST    ┌──────────────────────────────────────┐
│  Generator   │───────────────▶│              API  :8000              │
│  (Python)    │                │  (alert construction + queue writes) │
└──────────────┘                └───────┬──────────────┬───────────────┘
                                        │              │
                               PRODUCED/QUEUED      lpush
                                        │              │
                                        ▼              ▼
                               ┌─────────────┐   ┌─────────┐
                               │  PostgreSQL  │   │  Redis  │
                               │  (Ledger)    │   │ (Queue) │
                               └──────┬──────┘   └────┬────┘
                                      │               │ BLPOP
                               PROCESSING/     ┌──────┴──────┐
                               STORED/FAILED   │  Processor  │
                                      │        │  (Python)   │
                                      │        └──────┬──────┘
                                      │               │ es.index
                                      │               ▼
                                      │        ┌──────────────────┐
                                      │        │  Elasticsearch   │
                                      │        │  security_alerts │
                                      │        └──────────────────┘
                                      │
                          ┌───────────┴────────────┐
                          ▼                        ▼
                 ┌──────────────┐        ┌──────────────┐
                 │  Dashboard   │        │    Dejavu    │
                 │    :8050     │        │    :1358     │
                 └──────────────┘        └──────────────┘
```

**4 Python services:** api · processor · generator · dashboard — backed by Redis, Postgres, and Elasticsearch

---

## Services

**Pipeline services (Python):**

| Service | Port | Role |
|---------|------|------|
| api | 8000 | FastAPI: schema owner, alert factory, REST endpoints |
| processor | — | Consumes Redis, deduplicates, writes to ES |
| generator | — | Pure HTTP client — calls `POST /api/generate` |

**Infrastructure (always-on):**

| Service | Port | Role |
|---------|------|------|
| dashboard | 8050 | Live pipeline stats, auto-refresh every 5s |
| redis | 6379 | Alert queue (`alert_queue` list) |
| postgres | 5432 | State ledger — every lifecycle transition |
| elasticsearch | 9200 | Final alert store (searchable) |
| dejavu | 1358 | ES data browser (optional) |

---

## Data Flow

1. **Generator** calls `POST /api/generate` with `{"count": 1, "payload": {"source": "generator"}}`
2. **API** constructs the alert payload — assigns UUID, random type/severity/IPs, fingerprint, title prefix — and stores `source` as a first-class field
3. **API** writes `PRODUCED` → Postgres ledger
4. **API** pushes alert JSON → Redis `alert_queue` list
5. **API** writes `QUEUED` → Postgres ledger
6. **Processor** pops message from Redis (`BLPOP` with 5s timeout)
7. **Processor** writes `PROCESSING` → Postgres ledger
8. **Processor** checks ES for a matching fingerprint (deduplication):
   - **Duplicate** → writes `DUPLICATE_DROPPED` to ledger, discards alert
   - **Unique** → writes alert document to ES index `security_alerts`
9. **Processor** writes `STORED` (or `FAILED` on exception) → Postgres ledger
10. **Dashboard** and **API** read Postgres (stats, ledger history) and ES (alert search/list)

---

## Alert Model

```json
{
    "alert_id":    "550e8400-e29b-41d4-a716-446655440000",
    "title":       "[GENERATOR] Brute Force Login Attempt",
    "description": "Multiple failed SSH logins from 192.168.12.34",
    "severity":    "low | medium | high | critical",
    "source_ip":   "192.168.x.x",
    "dest_ip":     "10.0.x.x",
    "alert_type":  "brute_force | malware | phishing | port_scan | data_exfiltration",
    "timestamp":   "2026-06-26T12:34:56.789+00:00",
    "fingerprint": "a3f1b2c4d5e6f789",
    "source":      "generator | test | manual | unknown",
    "metadata":    {"source_service": "api"}
}
```

**Fingerprint** is `sha256(source_ip + alert_type + floor(unix_time / 60))[:16]` — a 16-char hex digest. Two alerts from the same source IP and alert type within the same 60-second window share a fingerprint and the second is deduped.

**Title prefix** — when `source` is provided, the title is prefixed `[SOURCE_UPPER]`. This lets you visually identify origin in the dashboard table and in Dejavu.

**`source` field** is indexed as a keyword in ES, enabling filtered search via `GET /api/alerts/search?source=test`.

---

## Postgres Ledger

```sql
CREATE TABLE alert_ledger (
    id             SERIAL PRIMARY KEY,
    alert_id       VARCHAR(64) NOT NULL,
    state          VARCHAR(30) NOT NULL,
    timestamp      TIMESTAMP DEFAULT NOW(),
    source_service VARCHAR(30) NOT NULL,
    metadata       JSONB DEFAULT '{}'
);
CREATE INDEX idx_alert_id ON alert_ledger(alert_id);
CREATE INDEX idx_state    ON alert_ledger(state);
```

**States:** `PRODUCED` → `QUEUED` → `PROCESSING` → `STORED` | `FAILED` | `DUPLICATE_DROPPED`

The API service is the **schema owner** — it runs `CREATE TABLE IF NOT EXISTS` and indexes on startup. Data persists across restarts; use `POST /api/reset` to wipe state when you need a clean slate.

---

## API Endpoints (port 8000)

```
GET  /api/health
     → {"status": "ok", "services": {"postgres": true, "elasticsearch": true, "redis": true}}

GET  /api/alerts?size=N                                (default 100, max 1000)
     → list of alert documents from ES, newest first

GET  /api/alerts/{alert_id}
     → single alert document from ES

GET  /api/alerts/search?q=&severity=&alert_type=&source=&size=
     → filtered search — all params optional, ANDed together

GET  /api/stats
     → pipeline accounting snapshot (see Stats section below)

GET  /api/ledger/{alert_id}
     → full ordered state history for one alert from Postgres

POST /api/generate
     body (all optional): {"count": 1, "payload": {"source": "manual"}, "force_fingerprint": "<hex>"}
     → {"generated": N, "alert_ids": [...], "fingerprints": [...]}
     → 429 {"detail": "Queue at capacity, try again later"} if Redis queue depth ≥ 200

POST /api/reset
     → {"reset": true}
     Truncates the Postgres ledger, drops and recreates the ES index, flushes the Redis queue.
     Use this when you need a guaranteed clean slate — e.g., before reproducing a specific defect.
```

`force_fingerprint` — supply a previous alert's fingerprint to guarantee the processor's deduplication logic is exercised. Used internally by the generator's 10% duplicate roll.

OpenAPI docs available at **http://localhost:8000/docs** when running.

---

## Stats Response

```json
{
    "total_produced":            42,
    "currently_queued":           2,
    "currently_processing":       1,
    "total_stored":              35,
    "total_failed":               2,
    "total_duplicates":           3,
    "unaccounted":                0,
    "accounting_balanced":     true,
    "last_event_at":   "2026-06-26T12:35:52Z",
    "avg_processing_latency_ms": 314.9
}
```

`accounting_balanced` is `true` when `total_stored + total_failed + total_duplicates + currently_queued + currently_processing == total_produced`. The validate script polls this until true.

---

## Generator Behavior

- Calls `POST /api/generate` with `source: "generator"` — no direct Redis or Postgres access
- **Startup:** 5 alerts seeded (sequential calls of count=1, 0.1s apart)
- **Continuous loop:** 1 alert every 10 seconds
- **10% duplicate roll:** on each loop iteration there is a 10% chance `force_fingerprint` (the previous alert's fingerprint) is passed, guaranteeing the processor's dedup path is exercised
- Waits for `GET /api/health` to report all services ready before starting

---

## Processor Behavior

- Polls Redis continuously using `BLPOP` with 5s timeout
- Writes `PROCESSING` to ledger before doing any work
- **Deduplication:** searches ES for a matching fingerprint; on hit writes `DUPLICATE_DROPPED` and discards the alert without writing to ES
- **Success path:** `es.index(index, id=alert_id, document=alert)` then writes `STORED`
- **Exception path:** writes `FAILED` with error metadata

Stuck/slow/failure simulation is intentionally absent from the processor code — those chaos scenarios are covered by the manual playbook in `tests/EXPLORATORY_CHAOS_TESTING.md`.

### Stuck-Alert Reaper

A background thread runs alongside the processor. Every `REAPER_INTERVAL_SECONDS` (default **300s / 5 min**) it queries Postgres for alerts whose latest ledger state is `PROCESSING` and that `PROCESSING` row is older than `STUCK_TIMEOUT_MINUTES` (default **60 minutes**). For each one it writes a `FAILED` entry with metadata:

```json
{"reason": "stuck_timeout", "stuck_duration_minutes": 63.2}
```

This means:
- Stuck alerts are **visible on the dashboard** as `PROCESSING` for up to an hour — a realistic hung-worker scenario
- After the timeout they flip to `FAILED` — `accounting_balanced` recovers automatically
- The full history (`PROCESSING` → `FAILED`) is preserved in the ledger for audit/test assertions

**Configuring the thresholds** via `docker-compose.yml` environment variables on the `event_processor` service:

```yaml
environment:
  STUCK_TIMEOUT_MINUTES: "60"    # how long before a stuck alert is failed
  REAPER_INTERVAL_SECONDS: "300" # how often the reaper checks
```

For faster testing (e.g. verify the reaper fires in a short test run), set both to smaller values:

```yaml
  STUCK_TIMEOUT_MINUTES: "2"
  REAPER_INTERVAL_SECONDS: "30"
```

---

## Dashboard (port 8050)

- Counter cards: Produced, Queued, Processing, Stored, Failed, Duplicates
- Accounting balance badge and avg latency
- Stuck alerts banner (processing > 60s) with alert IDs and timestamps
- Recent alerts table: Alert ID · Title (with source prefix) · Type · Severity · Source IP · Dest IP · Timestamp · State
- **"Browse in Dejavu →"** button — opens http://localhost:1358 pre-pointed at the `security_alerts` index
- **"Reset Pipeline"** button — wipes Postgres ledger, ES index, and Redis queue; requires typing `wipedata` in the confirm dialog
- Auto-refreshes every 5 seconds via `fetch()` — no page reload

**Dashboard endpoints (port 8050):**

```
GET  /dashboard/health        → infra + processor health snapshot
     → {"redis": {"status": "healthy|down"}, "postgres": {"status": "healthy|down"},
        "elasticsearch": {"status": "healthy|down"},
        "api": {"status": "healthy|unreachable", "latency_ms": 42},
        "processor": {"status": "healthy|stalled|down|not_started|unknown", "last_seen_s": 12}}
GET  /dashboard/stats         → pipeline accounting snapshot (same shape as /api/stats)
GET  /dashboard/alerts?size=N → recent alerts from ES, newest first (default 20, max 100)
GET  /dashboard/stuck         → alerts stuck in PROCESSING > 60s
POST /dashboard/reset         → wipe all data (ledger + ES index + Redis queue) → {"reset": true}
```

---

## Dejavu (port 1358)

Appbaseio Dejavu is a browser-based Elasticsearch data explorer. It connects directly from your browser to ES on port 9200 — CORS is pre-configured on the ES container.

**Connect:** open http://localhost:1358, enter `http://localhost:9200` as the cluster URL and `security_alerts` as the index, then click Connect.

**Filtering tips:**
- Click the filter icon (▼) next to `title` → contains → `GENERATOR` to see only generator events
- Filter `source` = `test` to isolate test-generated alerts
- Filter `timestamp` → is between → `2026-06-26T14:32:00Z` and `2026-06-26T14:33:00Z` for a minute window
- Or use the search bar: `source:test AND timestamp:[2026-06-26T14:32:00Z TO 2026-06-26T14:33:00Z]`

---

## Prerequisites

- Docker Desktop ≥ 24 (or Docker Engine + Compose plugin ≥ 2.20)
- 4 GB RAM available to Docker (Elasticsearch needs ~512 MB heap)
- Python 3.12 on the host (matches the `python:3.12-alpine` Dockerfiles)

---

## Run

```bash
cd services/
docker compose -p alertlab up -d
docker compose -p alertlab --profile pipeline up -d
```

The first command starts infrastructure (`redis`, `postgres`, `elasticsearch`, `dejavu`, `dashboard`). The second activates the three pipeline services (`api`, `event_processor`, `event_generator`).

Wait ~30 seconds for Elasticsearch to initialise, then check all services are up:

```bash
docker compose -p alertlab ps
```

---

## Local Development

Run the three pipeline services on the host against Docker infra (useful for debugging with a local debugger or faster iteration).

**Setup (one-time, from repo root):**

```bash
./setup_venv.sh
source .venv/bin/activate
```

The script enforces Python 3.12 (matching the Dockerfiles). If `python3.12` is not on your PATH, install it via [pyenv](https://github.com/pyenv/pyenv) or [python.org](https://www.python.org/downloads/).

```bash
# Start infra + dashboard first
cd services/
docker compose -p alertlab up -d

# In a new shell — set PYTHONPATH so Python finds the common/ package
cd services/
export PYTHONPATH=$(pwd)

# Terminal 1 — API
uvicorn api.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — Processor
python event_processor/main.py

# Terminal 3 — Generator
python event_generator/main.py
```

Stop by pressing `Ctrl+C` in each terminal. Infra and dashboard remain running.

---

## Validate

```bash
# With the .venv active (setup_venv.sh installs all deps including requests/psycopg2-binary)
python validate_service.py
```

Expected output ends with `Overall: PASS`.

---

## Access

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8050 |
| API health | http://localhost:8000/api/health |
| API docs (OpenAPI) | http://localhost:8000/docs |
| API stats | http://localhost:8000/api/stats |
| Recent alerts | http://localhost:8000/api/alerts |
| Dejavu | http://localhost:1358 |

---

## Generate Alerts on Demand

```bash
# Single alert, source unknown (no title prefix)
curl -X POST http://localhost:8000/api/generate

# Bulk with source label
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 50, "payload": {"source": "manual"}}'

# Filter to only manually-triggered alerts
curl "http://localhost:8000/api/alerts/search?source=manual"

# Filter by severity and type
curl "http://localhost:8000/api/alerts/search?severity=critical&alert_type=brute_force"
```

---

## Tear Down

**Stop pipeline services only** (leave infra + dashboard running — useful when iterating):
```bash
docker compose -p alertlab stop api event_processor event_generator
```

**Stop and remove pipeline containers** (infra + dashboard stays up):
```bash
docker compose -p alertlab rm -f -s api event_processor event_generator
```

**Stop everything** (infra + pipeline):
```bash
docker compose -p alertlab down
```

Add `-v` to also remove volumes (Postgres data). Omit it to keep data across restarts.

> Note: `docker compose -p alertlab down` tears down the shared network, which also stops infra containers. Use the targeted `stop`/`rm` commands above when you only want to restart the pipeline services.

---

## Clean Slate on Demand

Data persists across restarts. To wipe all state and start fresh, call:

```bash
curl -X POST http://localhost:8000/api/reset
```

This truncates `alert_ledger`, drops and recreates the `security_alerts` ES index, and flushes the `alert_queue` Redis list. Useful before reproducing a specific defect or resetting a test environment without restarting any services.

---

## Design Tradeoffs

| Decision | Alternative | Reason |
|----------|-------------|--------|
| Redis as queue | Kafka | Lightweight, instant startup, no ZooKeeper, sufficient for lab scale |
| Postgres ledger | In-memory / file log | SQL accounting queries, transactional inserts, reliable joins for stats |
| Generator as pure HTTP client | Direct Redis/Postgres writes | Single place (API) owns alert construction and queue logic; generator is replaceable |
| API as schema owner | Separate migration service | Idempotent init on startup, no extra tooling |
| Custom pipeline | Off-shelf SIEM | Full control: injectable failures, testable dedup, observable state machine |
| Separate dashboard service | Embed in API | Observation plane stays independent of data plane |
| `source` as top-level ES field | Buried in metadata | Indexable as keyword — enables `?source=test` filtered search for deterministic test assertions |
| `POST /api/reset` for clean slate | Wipe on every startup | Preserves data across restarts; explicit reset avoids wiping state needed to reproduce a defect |
| 429 on queue depth ≥ 200 | Unbounded queue | Surfaces real backpressure under load; prevents silent false-success when processor is overwhelmed |
| FastAPI + Uvicorn | Flask | Async, automatic OpenAPI docs, type hints |

---

## Known Limitations

- **Single ES node** — no HA, no replica shards; ES restart loses all indexed alerts (use `POST /api/reset` to re-sync after ES recovery)
- **No TLS or authentication** — lab environment only
- **Fingerprint dedup window is 60 seconds** — coarse; high burst generation of the same alert type can produce unintended duplicates
- **Stuck-alert scenarios require manual injection** — see `tests/EXPLORATORY_CHAOS_TESTING.md` for how to trigger and observe the reaper path
- **No dead-letter queue** — `FAILED` alerts are logged in the ledger but never retried
- **New Postgres connection per ledger write** in the processor — acceptable at lab throughput, not production-safe
- **Dejavu connects browser-direct to ES:9200** — CORS is pre-enabled; do not expose port 9200 publicly
