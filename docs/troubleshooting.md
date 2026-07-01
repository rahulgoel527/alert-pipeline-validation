# Troubleshooting Exercise: Alert Pipeline Production Incident

## Incident Summary

| Symptom | Observed Value |
|---------|---------------|
| Alerts received (generator logs) | 12 |
| Alerts visible via API (`/api/alerts`) | 9 |
| Missing alerts | 3 |
| Processing latency increase | 5× (200ms → 1000ms+) |
| Service crash | None (`docker compose -p alertlab ps` shows all healthy) |

---

## 1. Investigation Plan

Work through each layer in order: metrics → queue → ledger → storage → infrastructure.

### 1.1 Metrics (API-based) — Start Here

```bash
curl http://localhost:8000/api/stats | python -m json.tool
```

**What to look for:**

- `total_produced` should equal `total_stored + total_failed + total_duplicates` — any gap is your missing count
- `avg_processing_latency_ms` — confirm 5× elevation (baseline ~200ms, incident >1000ms)
- `currently_processing` — non-zero means alerts are stuck mid-flight
- `accounting_balanced` field — if `false`, the pipeline itself detected the gap

**Healthy baseline output structure:**

```json
{
  "total_produced": 12,
  "total_queued": 12,
  "total_processing": 0,
  "total_stored": 9,
  "total_failed": 0,
  "total_duplicates": 0,
  "currently_processing": 3,
  "avg_processing_latency_ms": 1050,
  "accounting_balanced": false
}
```

The gap above (12 produced, 9 stored, 3 stuck in `currently_processing`) is the primary signal.

---

### 1.2 Logs

```bash
# Errors and failures in event_processor (last 10 minutes)
docker compose -p alertlab logs event_processor --since 10m | grep -E "FAILED|ERROR|timeout|exception"

# Confirm how many alerts the event_generator says it produced
docker compose -p alertlab logs event_generator --since 10m | grep "PRODUCED"

# How many PROCESSING transitions did the event_processor log?
docker compose -p alertlab logs event_processor --since 10m | grep "PROCESSING" | wc -l

# Any ES write errors?
docker compose -p alertlab logs event_processor --since 10m | grep -E "elasticsearch|es_client|write"

# Slow-path alerts (event_processor logs slowness flag)
docker compose -p alertlab logs event_processor --since 10m | grep -i "slow"

# Full event_processor tail for context
docker compose -p alertlab logs event_processor 2>&1 | tail -80
```

**Key signals:**
- ES connection refused / timeout → confirms storage layer issue
- Count of `PROCESSING` log lines less than 12 → processor never picked up all alerts
- `STUCK alert` log line → the 2% hung-worker simulation fired (see H4)
- Slow path log lines with a 2–10 second delay → the 10% slowness simulation contributing to elevated latency

---

### 1.3 Queue (Redis)

```bash
# How many alerts are still waiting to be consumed?
redis-cli -h localhost LLEN alert_queue

# Inspect queue contents without consuming
redis-cli -h localhost LRANGE alert_queue 0 -1

# Check Redis memory and connection health
redis-cli -h localhost INFO server | grep -E "redis_version|uptime"
redis-cli -h localhost INFO clients | grep connected_clients
```

**Expected in healthy state:** `LLEN alert_queue` returns 0 after all alerts processed.

**If non-zero:** Alerts are backed up — processor is slow or stopped.

**If 0 but alerts still missing:** Processor consumed from queue but failed after that point.

---

### 1.4 Ledger (PostgreSQL)

Connect to the Postgres container:

```bash
docker exec -it $(docker compose -p alertlab ps -q postgres) psql -U postgres -d alerts
```

**Find the 3 missing alerts (non-terminal state):**

```sql
SELECT alert_id, MAX(state) AS last_state, MAX(timestamp) AS last_seen
FROM alert_ledger
WHERE alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger
    WHERE state IN ('STORED', 'FAILED', 'DUPLICATE_DROPPED')
)
GROUP BY alert_id
ORDER BY last_seen;
```

**Check for alerts stuck in PROCESSING:**

```sql
SELECT
    alert_id,
    timestamp AS stuck_since,
    NOW() - timestamp AS stuck_duration
FROM alert_ledger
WHERE state = 'PROCESSING'
AND alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger
    WHERE state IN ('STORED', 'FAILED', 'DUPLICATE_DROPPED')
)
ORDER BY timestamp;
```

**Full state timeline for a specific suspicious alert:**

```sql
-- Replace <alert_id> with an ID from the query above
SELECT state, timestamp, metadata
FROM alert_ledger
WHERE alert_id = '<alert_id>'
ORDER BY timestamp;
```

**Measure per-alert end-to-end latency:**

```sql
SELECT
    a.alert_id,
    MAX(CASE WHEN a.state = 'PRODUCED'  THEN a.timestamp END) AS produced_at,
    MAX(CASE WHEN a.state = 'STORED'    THEN a.timestamp END) AS stored_at,
    EXTRACT(EPOCH FROM (
        MAX(CASE WHEN a.state = 'STORED'   THEN a.timestamp END) -
        MAX(CASE WHEN a.state = 'PRODUCED' THEN a.timestamp END)
    )) AS latency_seconds
FROM alert_ledger a
GROUP BY a.alert_id
ORDER BY latency_seconds DESC NULLS FIRST
LIMIT 20;
```

**Check for alerts lost between QUEUED and PROCESSING:**

```sql
SELECT alert_id, MAX(state) AS last_state
FROM alert_ledger
WHERE alert_id IN (
    SELECT DISTINCT alert_id FROM alert_ledger WHERE state = 'QUEUED'
)
AND alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger WHERE state = 'PROCESSING'
)
GROUP BY alert_id;
```

**Check DUPLICATE_DROPPED entries:**

```sql
SELECT alert_id, timestamp, metadata
FROM alert_ledger
WHERE state = 'DUPLICATE_DROPPED'
ORDER BY timestamp DESC
LIMIT 20;
```

---

### 1.5 Storage (Elasticsearch)

```bash
# Cluster health — red/yellow signals a problem
curl -s localhost:9200/_cluster/health?pretty

# How many alerts actually made it to ES?
curl -s localhost:9200/security_alerts/_count | python -m json.tool

# Write thread pool — queue > 0 or rejected > 0 means ES is backed up
curl -s "localhost:9200/_cat/thread_pool/write?v&h=node_name,active,queue,rejected"

# Index stats — check indexing rate and any errors
curl -s "localhost:9200/_cat/indices/security_alerts?v"

# Check for any circuit breaker trips
curl -s localhost:9200/_nodes/stats/breaker?pretty | python -m json.tool
```

**Key signals:**
- `status: yellow` → replica unassigned (expected for single-node, not a problem)
- `status: red` → shard unavailable, writes failing
- `write` thread pool `queue > 0` → ES accepting work slower than processor sends it
- `rejected > 0` → ES dropped write requests

---

### 1.6 Infrastructure

```bash
# Container resource consumption
docker stats --no-stream

# Service health status
docker compose -p alertlab ps

# Check for OOM kills or restarts
docker inspect $(docker compose -p alertlab ps -q event_processor) | python -m json.tool | grep -A5 '"State"'
```

**What to look for:**
- Processor container `RestartCount > 0` → it crashed and recovered (explains lost-in-flight alerts)
- High CPU on processor or ES → resource contention
- High memory on ES near container limit → GC pressure causing write latency

---

## 2. Root Cause Hypotheses

### Hypothesis 1: Alerts Stuck in PROCESSING Due to ES Write Timeout

**Description:** The processor dequeued all 12 alerts from Redis and updated the ledger to `PROCESSING`, but when it attempted to write to Elasticsearch, ES was slow or unresponsive. The ES client timed out. The processor caught the timeout exception, but because the error path doesn't update the ledger state to `FAILED`, the 3 alerts remain in `PROCESSING` forever — invisible to `/api/alerts` (which reads from ES) but not counted as failed.

**Explains all symptoms:**
- **3 missing:** Visible in ledger as `PROCESSING`, not in ES, so not returned by `/api/alerts`
- **5× latency:** ES under load causes slow writes for the 9 that succeeded
- **No crash:** Timeout is caught as an exception; the process keeps running

**Confirm with:**
```sql
-- Should return exactly 3 rows
SELECT alert_id, timestamp
FROM alert_ledger
WHERE state = 'PROCESSING'
AND alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger
    WHERE state IN ('STORED', 'FAILED', 'DUPLICATE_DROPPED')
);
```
```bash
# ES write thread pool should show non-zero queue or rejected
curl -s "localhost:9200/_cat/thread_pool/write?v&h=node_name,active,queue,rejected"
```

**Eliminate with:** If the above SQL returns 0 rows, all alerts have a terminal state — this is not the cause.

---

### Hypothesis 2: Dedup Anomaly — Elevated DUPLICATE_DROPPED Count

**Description:** The processor's dedup check queries ES for the fingerprint before writing. When ES is slow or unavailable, `is_duplicate()` catches the exception and returns `False` — meaning it defaults to "not a duplicate" on error. This means ES slowness cannot cause false-positive drops. However, it's worth checking whether the 3 missing alerts landed in `DUPLICATE_DROPPED` for a legitimate but unexpected reason — for example, the generator sent fingerprint-colliding alerts faster than the 60-second window rolled.

**Note:** This hypothesis is a weaker candidate than H1 or H4 given the error-path behavior of `is_duplicate()`. Include it to rule out unexpected dedup collisions before concluding data loss.

**Confirm with:**
```sql
-- Find DUPLICATE_DROPPED alerts and their fingerprints
SELECT alert_id, metadata
FROM alert_ledger
WHERE state = 'DUPLICATE_DROPPED'
ORDER BY timestamp DESC;
```
```bash
# Then check if those fingerprints actually exist in ES
curl -s localhost:9200/security_alerts/_search -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"fingerprint":"<fingerprint_value>"}}}'
```
If the fingerprint IS found in ES, the dedup was correct — not a false positive. If it is NOT found, that is a separate bug in the dedup path.

**Eliminate with:** If `DUPLICATE_DROPPED` count matches expected generator duplicate rate (~10%) and every dropped alert has a matching fingerprint in ES, this hypothesis is eliminated.

---

### Hypothesis 3: Redis Messages Consumed but Processor Died Before Ledger Write

**Description:** The processor called `BLPOP` on `alert_queue` — atomically removing messages from Redis. Before it could write the `PROCESSING` state to the ledger, the goroutine or thread processing those 3 alerts panicked or hung. The messages are gone from Redis, not yet written to the ledger beyond `QUEUED`, and never written to ES. They are effectively lost.

**Explains all symptoms:**
- **3 missing:** Messages consumed from Redis but neither ledger nor ES has a `PROCESSING` or later state for them
- **5× latency:** Processor instability — partial hangs cause processing slowdowns system-wide
- **No crash:** The Docker container didn't restart; only the internal goroutine/thread died

**Confirm with:**
```sql
-- Alerts that have QUEUED but no PROCESSING entry
SELECT q.alert_id
FROM alert_ledger q
WHERE q.state = 'QUEUED'
AND q.alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger WHERE state = 'PROCESSING'
);
```
```bash
# Redis queue should be empty (messages already consumed)
redis-cli -h localhost LLEN alert_queue
```

**Eliminate with:** If every alert with a `QUEUED` entry also has a `PROCESSING` entry, messages were not lost between these two states.

---

## 3. Validation Steps

### Validate Hypothesis 1 (ES Write Timeout / Stuck PROCESSING)

```bash
# Step 1: Find stuck alerts (2 minutes)
docker exec -it $(docker compose -p alertlab ps -q postgres) psql -U postgres -d alerts -c "
SELECT alert_id, timestamp, NOW() - timestamp AS stuck_for
FROM alert_ledger
WHERE state = 'PROCESSING'
AND alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger
    WHERE state IN ('STORED','FAILED','DUPLICATE_DROPPED')
)
ORDER BY timestamp;"

# Step 2: Check ES write pressure (30 seconds)
curl -s "localhost:9200/_cat/thread_pool/write?v&h=node_name,active,queue,rejected"

# Step 3: Check ES cluster health (30 seconds)
curl -s localhost:9200/_cluster/health?pretty
```

| Result | Interpretation |
|--------|---------------|
| SQL returns 3 rows + ES thread queue > 0 | **Hypothesis 1 confirmed** |
| SQL returns 0 rows | Hypothesis 1 eliminated — move to H2 or H3 |
| ES thread pool looks healthy | ES not the bottleneck — consider H3 |

**Time estimate:** 3 minutes

---

### Validate Hypothesis 2 (Dedup Anomaly)

```bash
# Step 1: Pull DUPLICATE_DROPPED alerts and their fingerprints (2 minutes)
docker exec -it $(docker compose -p alertlab ps -q postgres) psql -U postgres -d alerts -c "
SELECT alert_id, metadata FROM alert_ledger
WHERE state = 'DUPLICATE_DROPPED'
ORDER BY timestamp DESC LIMIT 10;"

# Step 2: For each fingerprint from above, verify it exists in ES (1 minute per fingerprint)
curl -s localhost:9200/security_alerts/_search \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"fingerprint":"<value_from_step_1>"}},"_source":["alert_id","fingerprint","timestamp"]}'
```

| Result | Interpretation |
|--------|---------------|
| Fingerprint found in ES | Dedup was correct — Hypothesis 2 eliminated |
| Fingerprint NOT found in ES | Dedup bug — alert dropped without a legitimate collision |
| DUPLICATE_DROPPED count >> 10% of produced | Unexpected collision rate — investigate fingerprint generation |

**Time estimate:** 5 minutes

---

### Validate Hypothesis 3 (Lost Between QUEUED and PROCESSING)

```bash
# Step 1: Find alerts stuck at QUEUED (2 minutes)
docker exec -it $(docker compose -p alertlab ps -q postgres) psql -U postgres -d alerts -c "
SELECT q.alert_id, q.timestamp AS queued_at
FROM alert_ledger q
WHERE q.state = 'QUEUED'
AND q.alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger WHERE state = 'PROCESSING'
)
ORDER BY q.timestamp;"

# Step 2: Confirm Redis queue is drained (30 seconds)
redis-cli -h localhost LLEN alert_queue

# Step 3: Check for container restarts (1 minute)
docker inspect $(docker compose -p alertlab ps -q event_processor) | python -m json.tool | grep -A3 '"RestartCount"'
```

| Result | Interpretation |
|--------|---------------|
| SQL returns rows + Redis LLEN = 0 | **Hypothesis 3 confirmed** — messages lost in transit |
| SQL returns 0 rows | Hypothesis 3 eliminated |
| RestartCount > 0 | Confirms processor instability |

**Time estimate:** 4 minutes

---

## 4. Recommended Improvements

### Monitoring & Alerting

| Alert | Threshold | Why |
|-------|-----------|-----|
| Alerts stuck in PROCESSING | > 60 seconds | Catches H1 and H3 before users notice |
| Redis queue depth | > 100 alerts | Leading indicator of processor slowdown |
| `avg_processing_latency_ms` | > 2000ms | 10× baseline — catches ES degradation early |
| `accounting_balanced == false` | > 30 seconds | Direct signal of data loss |
| Processor log errors | Any ERROR/FAILED rate > 10% | Catch failure-rate regression |

**Dashboard enhancement:** Add a latency trend sparkline over the last 5 minutes so operators can see if latency is climbing or recovering.

---

### Resilience

**Dead Letter Queue (DLQ):**
Add a Redis list `alert_dlq` where alerts are pushed on unrecoverable failure. Processor moves alerts there instead of silently dropping them. Enables replay.

```
# Current (broken): alert lost on ES timeout
# Proposed: LPUSH alert_dlq <alert_payload> + ledger state = FAILED_DLQ
```

**Explicit FAILED state on ES timeout:**
The processor should catch write timeout exceptions and explicitly update the ledger state to `FAILED` with an error reason. Currently, a timeout leaves state as `PROCESSING` forever.

**Processor heartbeat/watchdog:**
A background thread that monitors in-flight alert age. If any alert has been in `PROCESSING` for more than 30 seconds, it sets the state to `TIMEOUT` and pushes the alert to the DLQ.

**Retry mechanism:**
Before permanent failure, retry ES writes up to 3 times with exponential backoff (1s, 2s, 4s). Log each retry attempt.

---

### Testing Gaps to Address

| Test | Purpose |
|------|---------|
| Chaos: kill processor mid-batch | Verify no alerts lost between BLPOP and ledger write |
| Soak: 1-hour constant load | Catch memory leaks and slow degradation |
| Dedup edge case: 60s window boundary | Alert at T=0 and T=60 should NOT be deduped |
| ES degradation: throttle network | Verify graceful FAILED state, not stuck PROCESSING |

---

### Observability

**Structured logging with correlation IDs:**
Every log line should include `alert_id` as a field so you can trace a single alert's journey across all log sources.

```
# Current: 2024-01-01T12:00:01Z PROCESSING alert
# Proposed: {"time":"2024-01-01T12:00:01Z","level":"info","alert_id":"abc123","state":"PROCESSING","latency_ms":210}
```

**Per-alert processing duration metric:**
Compute `STORED.timestamp - PROCESSING.timestamp` per alert and expose it as a histogram. Enables P95/P99 latency tracking.

**Queue consumer lag:**
Track the age of the oldest unprocessed message in Redis. This is a leading indicator — it starts climbing before latency metrics do.
