# Alert Pipeline Lab — Test Guide
> The code is AI-Assisted but design trade-offs are co-authored by me and reviewed before implementation. 
> This lab is intended for SDET technical assignment evaluation. 

## Prerequisites

- Lab must be running — see [lab_setup.md](../services/lab_setup.md) for setup instructions
- All 7 services (`redis`, `postgres`, `elasticsearch`, `generator`, `processor`, `api`, `dashboard`) must be healthy before running tests
- Python 3.11+
- Docker socket access — required only for `@pytest.mark.slow` failure-injection tests that restart containers

Verify the lab is up:

```bash
docker compose -p alertlab ps
# All services should show "healthy" or "running"

curl http://localhost:8000/api/health
# Expected: {"status":"ok","services":{"postgres":true,"elasticsearch":true,"redis":true}}
```

If any service is not ready, `pytest` will exit immediately with a clear message naming the unready services.

---

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
```

Install test dependencies:

```bash
pip install -r tests/requirements.txt
```

Verify collection (no errors means environment is correct):

```bash
pytest tests/ --collect-only --ignore=tests/load
```

---

## Run Tests

### Full suite (excluding load)

```bash
pytest tests/ -v --ignore=tests/load
```

Expected: 76 tests pass (35 unit + 41 E2E). Unit tests complete instantly; E2E tests take ~60–120 seconds.

### By test directory

```bash
pytest tests/unit/ -v               # unit tests only — no services needed
pytest tests/e2e/ -v                # E2E tests only — requires running lab
```

### By test file

```bash
pytest tests/e2e/test_e2e_flow.py -v
pytest tests/e2e/test_duplicates.py -v
pytest tests/e2e/test_failure_scenarios.py -v
pytest tests/e2e/test_api_endpoints.py -v
```

### By marker

| Marker | What it covers | Command |
|--------|---------------|---------|
| `unit` |Isolated business logic — no services required | `pytest tests/ -m unit -v` |
| `e2e` | All end-to-end tests — requires running lab | `pytest tests/ -m e2e -v` |
| `slow` | Subset of `e2e` — container-restart tests, requires Docker socket | `pytest tests/ -m slow -v` |

Skip slow tests (for faster local iteration or CI without Docker socket):

```bash
pytest tests/ -v --ignore=tests/load -m "e2e and not slow"
```

---

## Unit Tests

Unit tests cover isolated business logic — functions whose failure modes are probabilistic, time-dependent, or involve boundary conditions the E2E suite cannot exercise reliably. They require **no running services** and complete in under 1 second.

### Run

```bash
pytest tests/unit/ -v
```

### What's covered

| File | Logic tested | Why E2E can't catch it |
|------|-------------|------------------------|
| `unit/test_unit_processor.py` | `is_duplicate()` (hit/miss/NotFoundError), `process_alert()` DUPLICATE_DROPPED / STORED / FAILED paths, `log_ledger()` metadata serialization | Duplicate check and ES write failure modes can't be triggered reliably via E2E; stuck/slow/chaos scenarios are covered by the manual playbook |
| `unit/test_unit_api.py` | Fingerprint 60s window contract, `generate_alerts()` count cap (max 100) and source prefix logic, payload override merge, ledger atomicity (PRODUCED+QUEUED share one transaction), accounting balance formula | Window boundary only breaks under load; count cap is never reached by tests generating 5–50 alerts; transaction atomicity can't be observed via E2E |
| `unit/test_unit_reaper.py` | `reaper_loop()` writes FAILED entry with correct metadata for a stuck alert, skips INSERT when no stuck alerts, SQL contract (DISTINCT ON, PROCESSING filter, `make_interval` timeout) | Reaper fires after 60 min — outside any E2E timeout; tests call the real `reaper_loop()` with mocked Postgres and `time.sleep` |

### Marker

```bash
pytest tests/ -m unit -v            # unit tests only
pytest tests/ -m "not unit" -v      # skip unit tests
```

---

## E2E Tests

E2E tests validate observable pipeline behaviour end-to-end: generating alerts through the API, watching them flow through Redis and the processor, and asserting final state in Elasticsearch and the Postgres ledger. They require all lab services to be running.

### Run

```bash
pytest tests/e2e/ -v
```

Expected: 41 tests pass in ~60–120 seconds.

### What's covered

| File | What it tests |
|------|--------------|
| `e2e/test_e2e_flow.py` | Full pipeline flow: generate → PRODUCED/QUEUED in ledger → PROCESSING → STORED in ES; data integrity; multi-alert batch processing; stats accuracy |
| `e2e/test_duplicates.py` | Fingerprint dedup: DUPLICATE_DROPPED state, ES exclusion, ledger metadata, accounting balance after duplicates |
| `e2e/test_failure_scenarios.py` | FAILED state logging, ES exclusion of failed alerts, pipeline recovery, ES/Redis unavailability, burst stability, processing latency bounds |
| `e2e/test_api_endpoints.py` | API surface: health, list/search/get alerts, ledger endpoint, generate (force_fingerprint, payload override, count clamping), stats schema |

### Marker

```bash
pytest tests/ -m e2e -v               # all 41 E2E tests
pytest tests/ -m "e2e and not slow" -v # skip container-restart tests
pytest tests/ -m slow -v              # container-restart tests only
```

### Slow Tests

Tests marked `@pytest.mark.slow` inject failures by stopping or pausing Docker containers via `docker compose` subprocess calls. They:

- Require the test runner to have Docker socket access
- Add approximately 60 seconds to the suite runtime
- Are safe to skip in environments without Docker access: `-m "not slow"`

These tests always restore the containers they touch — even on failure — so the lab remains usable after a run.

### Parallel Execution

The current default is **serial execution**, enforced via `-p no:xdist` in `pytest.ini`. Tests that stop/pause containers or assert on pipeline-wide counters require exclusive access to shared infrastructure. If faster CI is needed, split the suite: parallel for per-ID tests, serial for infrastructure/counter tests.

---

## Load Tests

Load tests use [Locust](https://locust.io) to measure throughput and latency under sustained traffic. They are kept separate from the pytest suite and have their own dependency file.

### Run

Web UI mode (recommended for manual testing):

```bash
cd tests/load
locust -f locustfile.py
```

Open [http://localhost:8089](http://localhost:8089). The host, user count, and spawn rate are pre-filled from `locust.conf` — adjust as needed and click **Start**.

Headless mode (scripted / CI):

```bash
cd tests/load
locust -f locustfile.py --headless
```

Uses `locust.conf` defaults: 20 users, spawn rate 5/s, 2 minute run against `http://localhost:8000`. Override any default inline:

```bash
locust -f locustfile.py --headless -u 50 -r 10 -t 5m
```

### What to observe

| Metric | Target |
|--------|--------|
| POST /api/generate P50 | < 50 ms |
| POST /api/generate P95 | < 200 ms |
| GET /api/stats P95 | < 100 ms |
| Failure rate | < 1% (HTTP errors, not pipeline failures) |

### Post-burst drain check

After the load test ends, verify the pipeline drains to balanced:

```python
import requests, time
while True:
    s = requests.get("http://localhost:8000/api/stats").json()
    print(f"queued={s['currently_queued']} processing={s['currently_processing']} balanced={s['accounting_balanced']}")
    if s["accounting_balanced"]:
        print("Pipeline drained.")
        break
    time.sleep(2)
```

---

## Exploratory + Chaos Testing

Not all scenarios can be automated deterministically. Scenarios involving long timeouts (reaper), race conditions (ES refresh), mid-transaction crashes, or infrastructure data loss are documented as a manual playbook:

```bash
# Open the playbook:
cat tests/EXPLORATORY_CHAOS_TESTING.md
```

See [EXPLORATORY_CHAOS_TESTING.md](EXPLORATORY_CHAOS_TESTING.md) for 6 runnable scenarios mapped to the assignment's 5 production issues: data loss (Redis kill), partial degradation (incomplete results), burst slowdown, duplicate race condition, stuck processing (reaper), and mid-transaction crash (consistency gap).

---

## Design Tradeoffs

| Decision | Alternative | Reason |
|----------|-------------|--------|
| `LedgerClient` queries Postgres directly | Assert state only via the API | Gives tests an independent source of truth — catches bugs the API layer could mask |
| Session-scoped `api_client` and `ledger_client` | Function-scoped (fresh connection per test) | Reuses connections, faster suite; per-test `baseline_stats` snapshots isolate noise instead |
| `allow_stuck=True` in burst tests | Hard timeout per alert | Stuck alerts can occur via chaos injection (see EXPLORATORY_CHAOS_TESTING.md); hard timeout would flake |
| `baseline_stats` snapshot per test | Truncate tables before each test | Non-destructive; tests run against a live lab without pausing the background generator |
| `@pytest.mark.slow` for container-restart tests | Always run container-restart tests | Docker socket not always available (CI, shared envs); the marker lets teams opt in |
| Serial execution enforced via `-p no:xdist` | Parallel execution with `pytest-xdist` | Shared pipeline state (counters, containers, queue) makes parallel runs unreliable — see Parallel Execution section |
