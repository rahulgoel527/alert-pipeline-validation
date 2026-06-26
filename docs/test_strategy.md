# Test Strategy: Alert Processing Pipeline

## Scope
End-to-end validation of alert ingestion, processing, storage, and investigation across the full 8-service pipeline.

## System Under Test
- Alert Generator → Redis Queue → Processor → Elasticsearch (final store)
- State tracking: PostgreSQL Ledger (PRODUCED → QUEUED → PROCESSING → STORED / FAILED / DUPLICATE_DROPPED)
- Investigation surface: API (`:8000`) + Dashboard (`:8050`)

## Test Levels

| Level | What | How |
|-------|------|-----|
| Unit | Fingerprint logic, failure injection paths, accounting math, ledger serialisation, reaper SQL | Pytest + mocks — no services required |
| E2E: Flow | Full pipeline flow, state transitions, data integrity, stats accounting | Pytest + real Docker services |
| E2E: Dedup | Fingerprint dedup correctness (60s window, same/different source) | Pytest + ledger + ES assertions |
| E2E: Failure | ES/Redis unavailability, processor recovery, burst stability, latency bounds | Pytest + container stop/start |
| Performance | Throughput, P95 latency, queue drain after burst | Locust — separate from pytest suite |

## Key Risks & Coverage

| Risk | Priority | Coverage |
|------|----------|----------|
| Alert data loss | P0 | Accounting balance: `produced == stored + failed + duplicates` |
| Duplicate false positive | P1 | Fingerprint uniqueness, DUPLICATE_DROPPED ledger assertion |
| Duplicate false negative | P1 | Same-window dedup, ES exclusion check |
| Alert stuck in PROCESSING | P1 | 2%-rate stuck injection, reaper SQL contract |
| Latency degradation under load | P2 | Locust burst: P95 < 5s, queue drains to balanced |
| Service crash mid-processing | P2 | ES stop/restart, FAILED state + recovery assertions |

## Automation Approach
- **Framework:** Pytest + `requests` + `psycopg2` + Locust
- **Assertion strategy:** Dual-source — ES for final state, Postgres ledger for full history
- **Async handling:** `poll_until()` helper — no hardcoded `sleep()` calls
- **Test isolation:** Per-test baseline snapshots; tests generate own data, assert on deltas

## Entry Criteria
- All 8 Docker services healthy (`docker compose ps`)
- `GET /api/health` returns `{"status": "ok"}`
- Pipeline processes at least 1 alert end-to-end before suite runs

## Exit Criteria
- All unit and E2E tests pass
- `accounting_balanced: true` in `/api/stats` after suite
- `currently_processing == 0` in ledger
- Load test P95 < 5s; zero accounting gap after burst

## Known Gaps
- Single-node ES only — no multi-node failover
- No soak test (1h+) in automated suite
- Dedup edge case at exact 60s window boundary not covered
- Redis message format unversioned — no contract tests
- Multi-worker processor scaling untested
