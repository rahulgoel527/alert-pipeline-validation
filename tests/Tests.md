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
docker compose ps
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
# .venv\Scripts\activate           # Windows
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

Expected: 62 tests pass (43 unit + 19 E2E). Unit tests complete instantly; E2E tests take ~60–120 seconds.

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
```

### By marker

| Marker | What it covers | Command |
|--------|---------------|---------|
| `unit` | Isolated business logic — no services required | `pytest tests/ -m unit -v` |
| `e2e` | Full pipeline flow: generate → queue → process → store | `pytest tests/ -m e2e -v` |
| `duplicates` | Fingerprint dedup: detection, ES exclusion, ledger metadata | `pytest tests/ -m duplicates -v` |
| `failures` | Failure rate validation, ES/Redis resilience, burst stability | `pytest tests/ -m failures -v` |
| `slow` | Container-restart tests (stops/pauses Docker services) | `pytest tests/ -m slow -v` |
| `load` | Locust-based throughput tests — see Load Tests section below | — |

Skip slow tests (for faster local iteration or CI without Docker socket):

```bash
pytest tests/ -v --ignore=tests/load -m "not slow"
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
| `unit/test_unit_processor.py` | `is_duplicate()` (hit/miss/NotFoundError), `process_alert()` failure injection (2% stuck, 5% fail, 10% slow), `log_ledger()` metadata serialization | The 2% stuck path is invisible until the reaper fires (60 min); failure thresholds can't be verified probabilistically |
| `unit/test_unit_api.py` | Fingerprint 60s window contract, `generate_alerts()` count cap (max 100) and source prefix logic, accounting balance formula | Window boundary only breaks under load; count cap is never reached by tests generating 5–50 alerts |
| `unit/test_unit_reaper.py` | FAILED ledger entry shape, `stuck_duration_minutes` typed as float not string, SQL query contract (DISTINCT ON, PROCESSING filter, parameterised timeout) | Reaper fires after 60 min — outside any E2E timeout; metadata type bugs silently corrupt the ledger |

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

Expected: 19 tests pass in ~60–120 seconds.

### What's covered

| File | What it tests |
|------|--------------|
| `e2e/test_e2e_flow.py` | Full pipeline flow: generate → PRODUCED/QUEUED in ledger → PROCESSING → STORED in ES; data integrity; multi-alert batch processing; stats accuracy |
| `e2e/test_duplicates.py` | Fingerprint dedup: DUPLICATE_DROPPED state, ES exclusion, ledger metadata, accounting balance after duplicates |
| `e2e/test_failure_scenarios.py` | FAILED state logging, ES exclusion of failed alerts, pipeline recovery, ES/Redis unavailability, burst stability, processing latency bounds |

### Marker

```bash
pytest tests/ -m "e2e or duplicates or failures" -v
```

### Slow Tests

Tests marked `@pytest.mark.slow` inject failures by stopping or pausing Docker containers via `docker compose` subprocess calls. They:

- Require the test runner to have Docker socket access
- Add approximately 60 seconds to the suite runtime
- Are safe to skip in environments without Docker access: `-m "not slow"`

These tests always restore the containers they touch — even on failure — so the lab remains usable after a run.

### Parallel Execution

Most E2E tests are parallel-safe: they generate their own alerts, track them by ID, and assert only on the state of those specific IDs. They never read aggregate counters and never mutate shared infrastructure.

A small subset cannot run in parallel — they either assert on pipeline-wide counts that other concurrent tests would pollute, or they stop/pause shared infrastructure that other tests depend on.

The current default is **serial execution**, enforced via `-p no:xdist` in `pytest.ini`. This is a pragmatic choice for simplicity, not a fundamental constraint. If faster CI is needed, the right design is to split the suite into two runs: parallel for atomic tests, serial for the non-parallel subset.

**Tests safe to run in parallel** — track only the alert IDs they generate, assert on per-ID ledger state, and do not touch shared counters or infrastructure:

| Test | Why it's parallel-safe |
|------|------------------------|
| `test_alert_generation_produces_to_queue` | Asserts PRODUCED state for its own alert ID only |
| `test_alert_flows_through_complete_pipeline` | Tracks a single alert ID from queue to ES |
| `test_alert_lifecycle_states_are_complete` | Asserts state ordering for one alert ID |
| `test_alert_data_integrity` | Retrieves from ES by its own alert ID |
| `test_multiple_alerts_all_processed` | Polls terminal state per tracked ID, no count assertions |
| `test_duplicate_alert_is_detected` | Reads existing DUPLICATE_DROPPED entries, no count threshold |
| `test_duplicate_not_stored_in_elasticsearch` | Checks ES absence for a known alert ID |
| `test_original_alert_still_stored_correctly` | Queries ES by fingerprint for a known duplicate pair |
| `test_duplicate_logged_in_ledger_with_metadata` | Reads metadata for a known alert ID |
| `test_different_fingerprints_both_stored` | Tracks two specific IDs it generated |
| `test_failed_alert_logged_in_ledger` | Reads FAILED entries, no count assertion |
| `test_failed_alert_not_in_elasticsearch` | Checks ES absence for known FAILED IDs |
| `test_system_continues_after_failure` | Tracks its own generated IDs only |

To run only these in parallel (requires `pip install pytest-xdist`):

```bash
pytest tests/ -m "e2e or duplicates" -v -n auto --ignore=tests/load
```

**Tests that must run serially:**

| Test | Intent | Why parallel is unsafe |
|------|--------|------------------------|
| `test_pipeline_stats_are_accurate` | Verifies `/api/stats` counters match actual ledger state after a known number of alerts | Uses `baseline_stats` to measure counter growth. A concurrent test generating alerts lands in the same counting window, inflating the delta and breaking the assertion. |
| `test_accounting_balanced_after_duplicates` | Verifies `accounting_balanced=true` holds after duplicates are introduced | Same counter-window problem — a concurrent burst can flip `accounting_balanced` to false during the assertion window even if this test's own alerts are balanced. |
| `test_elasticsearch_unavailable_handling` | Verifies processor marks alerts FAILED when ES is unreachable | Stops the ES container. Any concurrent test storing or retrieving an alert will fail with a connection error unrelated to its own logic. Requires exclusive access to the infrastructure. |
| `test_redis_connection_recovery` | Verifies processor resumes after a Redis pause | Pauses the Redis container. Any concurrent test polling for a terminal alert state will stall until Redis resumes, causing spurious timeouts. |
| `test_pipeline_stability_under_burst` | Verifies 50 alerts all reach terminal state within a time bound | Floods the single-worker processor queue with 50 alerts. Concurrent tests waiting on their own alerts will hit timeouts because the queue is saturated. |
| `test_processing_latency_within_bounds` | Verifies average PROCESSING→STORED latency stays under 5 seconds | Latency measurement is sensitive to queue depth. A concurrent burst from another test increases processing time, invalidating the latency assertion. |

---

## Load Tests

Load tests use [Locust](https://locust.io) to measure throughput and latency under sustained traffic. They are kept separate from the pytest suite and have their own dependency file.

### Run

Install dependencies first:

```bash
pip install -r tests/load/requirements.txt
```

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

## Design Tradeoffs

| Decision | Alternative | Reason |
|----------|-------------|--------|
| `LedgerClient` queries Postgres directly | Assert state only via the API | Gives tests an independent source of truth — catches bugs the API layer could mask |
| Session-scoped `api_client` and `ledger_client` | Function-scoped (fresh connection per test) | Reuses connections, faster suite; per-test `baseline_stats` snapshots isolate noise instead |
| `allow_stuck=True` in burst tests | Hard timeout per alert | The processor simulates 2% hung workers — ~1 in 50 alerts never completes; hard timeout would flake |
| `baseline_stats` snapshot per test | Truncate tables before each test | Non-destructive; tests run against a live lab without pausing the background generator |
| `@pytest.mark.slow` for container-restart tests | Always run container-restart tests | Docker socket not always available (CI, shared envs); the marker lets teams opt in |
| Serial execution enforced via `-p no:xdist` | Parallel execution with `pytest-xdist` | Shared pipeline state (counters, containers, queue) makes parallel runs unreliable — see Parallel Execution section |
