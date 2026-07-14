# Alert Pipeline Lab — Test Guide
> The code is AI-Assisted but design trade-offs are co-authored by me and reviewed before implementation.
> This lab is intended for SDET technical assignment evaluation.

## Contents

- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Run Tests](#run-tests)
- [Unit Tests](#unit-tests)
- [E2E Tests](#e2e-tests)
- [Load Tests](#load-tests)
- [Exploratory + Chaos Testing](#exploratory--chaos-testing)
- [Design Tradeoffs](#design-tradeoffs)

---

## Prerequisites

- Lab must be running — see [lab_setup.md](../services/lab_setup.md)
- All 7 services (`redis`, `postgres`, `elasticsearch`, `api`, `processor`, `generator`, `dashboard`) must be healthy
- Python 3.11+
- Docker socket access — required only for `@pytest.mark.slow` failure-injection tests that restart containers

Verify the lab is up:

```bash
docker compose -p alertlab ps
curl http://localhost:8000/api/health
# Expected: {"status":"ok","services":{"postgres":true,"elasticsearch":true,"redis":true}}
```

If any service is not ready, `pytest` exits immediately with a message naming the unready services.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
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

Expected: 78 tests pass (35 unit + 43 E2E). Unit tests complete instantly; E2E takes ~60–120 seconds.

### By directory or file

```bash
pytest tests/unit/ -v               # unit only — no services needed
pytest tests/e2e/ -v                # E2E only — requires running lab

pytest tests/e2e/test_e2e_flow.py -v
pytest tests/e2e/test_duplicates.py -v
pytest tests/e2e/test_failure_scenarios.py -v
pytest tests/e2e/test_api_endpoints.py -v
```

### By marker

| Marker | What it covers | Command |
|--------|----------------|---------|
| `unit` | Isolated business logic — no services required | `pytest tests/ -m unit -v` |
| `e2e` | All end-to-end tests — requires running lab | `pytest tests/ -m e2e -v` |
| `slow` | Container-restart failure injection — requires Docker socket | `pytest tests/ -m slow -v` |
| `load` | CI Locust wrapper — 30s headless burst, asserts HTML report produced | `pytest tests/load/ -m load -v` |

Skip slow tests (faster local iteration or CI without Docker socket):

```bash
pytest tests/ -v --ignore=tests/load -m "e2e and not slow"
```

---

## Unit Tests

Cover isolated business logic — functions whose failure modes are probabilistic, time-dependent, or involve boundary conditions E2E can't exercise reliably. **No running services required.** Complete in under 1 second.

```bash
pytest tests/unit/ -v
```

| File | Logic tested |
|------|-------------|
| `test_unit_processor.py` | `is_duplicate()` hit/miss/NotFoundError; `process_alert()` DUPLICATE_DROPPED / STORED / FAILED paths; `log_ledger()` metadata serialization |
| `test_unit_api.py` | Fingerprint 60s window contract; `generate_alerts()` count cap and source prefix; payload override merge; ledger atomicity (PRODUCED+QUEUED in one transaction); accounting balance formula |
| `test_unit_reaper.py` | `reaper_loop()` writes FAILED with correct metadata for a stuck alert; skips INSERT when none stuck; SQL contract (DISTINCT ON, PROCESSING filter, `make_interval` timeout) |

The "Why E2E can't catch it" rationale: duplicate check and ES failure modes can't be triggered reliably via HTTP; the reaper fires after 60 min — outside any E2E timeout.

---

## E2E Tests

Validate observable pipeline behaviour end-to-end: generating alerts through the API, watching them flow through Redis and the processor, and asserting final state in Elasticsearch and the Postgres ledger. **Requires all 7 lab services.**

```bash
pytest tests/e2e/ -v
```

Expected: 43 tests pass in ~60–180 seconds.

| File | What it tests |
|------|--------------|
| `test_e2e_flow.py` | Full pipeline flow: generate → PRODUCED/QUEUED in ledger → PROCESSING → STORED in ES → findable via search API; data integrity; multi-alert batch; stats accuracy |
| `test_duplicates.py` | Fingerprint dedup: DUPLICATE_DROPPED state, ES exclusion, ledger metadata, accounting balance after duplicates |
| `test_failure_scenarios.py` | FAILED state and error metadata; pipeline recovery; ES/Redis/Postgres unavailability; ES retry-success path; burst stability; processing latency bounds; Prometheus endpoint reachability; alerts survive processor pause |
| `test_api_endpoints.py` | API surface: health, list/search/get alerts, ledger endpoint, generate (force_fingerprint, payload override, count clamping), stats schema |

**Slow tests** (`@pytest.mark.slow`) inject failures by stopping or pausing Docker containers via subprocess. They require Docker socket access, add ~60s to runtime, and always restore any container they touch. Skip with `-m "e2e and not slow"`.

**Execution model:** serial by default (`-p no:xdist` in `pytest.ini`). Shared pipeline state — counters, containers, queue — makes parallel runs unreliable.

---

## Load Tests

Uses [Locust](https://locust.io) to measure throughput and latency under sustained traffic. Separate from the pytest suite with its own dependency file.

**Web UI (manual):**
```bash
cd tests/load
locust -f locustfile.py
# Open http://localhost:8089 — host/user count/spawn rate pre-filled from locust.conf
```

**Headless (scripted / CI):**
```bash
cd tests/load
locust -f locustfile.py --headless          # uses locust.conf: 20 users, 5/s, 2 min
locust -f locustfile.py --headless -u 50 -r 10 -t 5m   # override inline
```

**CI pytest wrapper:**
```bash
pytest tests/load/test_load_ci.py -v -m load
# or: make load-test
```

### Targets

| Metric | Target |
|--------|--------|
| POST /api/generate P50 | < 50 ms |
| POST /api/generate P95 | < 200 ms |
| GET /api/stats P95 | < 100 ms |
| Failure rate | < 1% (HTTP errors, not pipeline failures) |

After the run, poll `GET /api/stats` until `accounting_balanced` is `true` to confirm the pipeline drained.

---

## Exploratory + Chaos Testing

Not all scenarios can be automated deterministically — long timeouts (reaper), race conditions (ES refresh), mid-transaction crashes, and infrastructure data loss are documented as a manual playbook:

```bash
cat tests/EXPLORATORY_CHAOS_TESTING.md
```

See [EXPLORATORY_CHAOS_TESTING.md](EXPLORATORY_CHAOS_TESTING.md) for 6 runnable scenarios mapped to the assignment's 5 production issues: data loss (Redis kill), partial degradation, burst slowdown, duplicate race condition, stuck processing (reaper), and mid-transaction crash.

---

## Design Tradeoffs

| Decision | Alternative | Reason |
|----------|-------------|--------|
| `LedgerClient` queries Postgres directly | Assert state only via API | Independent source of truth — catches bugs the API layer could mask |
| Session-scoped `api_client` and `ledger_client` | Function-scoped | Reuses connections; per-test `baseline_stats` snapshots isolate noise instead |
| `poll_until` with broad TERMINAL set in burst tests | Hard assert per alert | Stuck alerts can occur via chaos injection; broad terminal set avoids flake |
| `baseline_stats` snapshot per test | Truncate tables before each test | Non-destructive; tests run against a live lab without pausing the background generator |
| `@pytest.mark.slow` for container-restart tests | Always run them | Docker socket not always available in CI or shared envs |
| Serial execution via `-p no:xdist` | `pytest-xdist` parallel | Shared pipeline state (counters, containers, queue) makes parallel runs unreliable |
