# Exploratory + Chaos Testing

Manual playbook for scenarios that can't be automated deterministically. Each maps to a production issue from the assignment scenario. Run them independently — no ordering dependency.

**Assignment production issues covered:**
1. Some alerts disappear after ingestion → Scenario 1
2. Investigation API occasionally returns incomplete results → Scenario 2
3. System slows down during burst traffic → Scenario 3
4. Duplicate alerts appear intermittently → Scenario 4
5. Some alerts remain in processing indefinitely → Scenario 5
6. Bonus: ES/Ledger consistency gap → Scenario 6

---

## Prerequisites

```bash
cd services/
docker compose ps                    # All 8 services running
curl -s http://localhost:8000/api/health | jq .  # All services true
```

Tools needed: `jq`, `docker`, `curl`

---

## Scenario 1: Alert Disappearance (Redis Data Loss)

**Production issue**: Some alerts disappear after ingestion.

**What we're testing**: If Redis crashes mid-flight, queued alerts vanish permanently — PRODUCED/QUEUED in ledger but never reach PROCESSING.

**Why not automated**: Requires `docker kill` (data-destroying) and produces a permanent accounting gap with no recovery path.

### Steps

```bash
# 1. Pause processor (stop draining)
docker compose pause event_processor

# 2. Generate 10 alerts
RESPONSE=$(curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 10}')
echo $RESPONSE | jq '.alert_ids'

# 3. Confirm queued in Redis
docker compose exec redis redis-cli LLEN alert_queue

# 4. Kill Redis (SIGKILL — no flush, data lost)
docker compose kill redis

# 5. Restart Redis (fresh, empty queue)
docker compose up -d redis
sleep 2

# 6. Unpause processor
docker compose unpause event_processor
sleep 5
```

### Verification

```bash
# Accounting should be imbalanced:
curl -s http://localhost:8000/api/stats | jq '{total_produced, total_stored, total_failed, total_duplicates, unaccounted, accounting_balanced}'
# Expected: accounting_balanced = false, unaccounted >= 10

# Alerts stuck at QUEUED forever:
docker exec -it $(docker compose ps -q postgres) psql -U postgres -d alerts -c "
SELECT count(*) as lost FROM alert_ledger
WHERE state = 'QUEUED'
AND alert_id NOT IN (
    SELECT DISTINCT alert_id FROM alert_ledger WHERE state = 'PROCESSING'
);"
```

### Impact

Permanent data loss — the at-most-once delivery gap of BLPOP. Production fix: Redis Streams with XACK, or a dead-letter reconciliation job.

---

## Scenario 2: Partial Service Degradation (Incomplete API Results)

**Production issue**: Investigation API occasionally returns incomplete results.

**What we're testing**: When ES is down but other services are up, read endpoints fail while write path (to Redis) may still succeed — leading to alerts that are ingested but invisible.

**Why not automated**: Tests qualitative degradation behavior, not binary pass/fail.

### Steps

```bash
# === ES down — alerts ingested but invisible ===

# 1. Stop Elasticsearch
docker compose stop elasticsearch

# 2. Health check
curl -s http://localhost:8000/api/health | jq .
# Expected: elasticsearch = false

# 3. Generate alerts (pushes to Redis — may still work)
curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 3}' | jq .

# 4. Try to search/list alerts (requires ES)
curl -s http://localhost:8000/api/alerts?size=5
# Expected: error or empty

# 5. Check processor logs — should show FAILED (ES write exceptions)
docker compose logs event_processor --since 30s | tail -10

# 6. Restart ES and verify recovery
docker compose start elasticsearch
sleep 30
curl -s http://localhost:8000/api/health | jq .

# === Redis down — writes fail, reads still work ===

# 7. Stop Redis
docker compose stop redis

# 8. Try to generate (requires Redis for queue)
curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" -d '{"count": 1}'
# Expected: error

# 9. Read operations still work (from ES)
curl -s http://localhost:8000/api/alerts?size=2 | jq 'length'

# 10. Restart
docker compose start redis
sleep 2
```

### Impact

When ES is down: alerts get FAILED in ledger but are invisible to investigation API — explains "incomplete results." The system doesn't crash but the investigation surface is degraded.

---

## Scenario 3: Burst Traffic Slowdown

**Production issue**: System slows down during burst traffic.

**What we're testing**: Under a rapid burst of 50+ alerts, does processing latency degrade? Does the queue back up? Does the system recover after the burst?

**Why not automated**: Latency observation over time is qualitative — need to watch trends, not assert a single number.

### Steps

```bash
# 1. Baseline latency
curl -s http://localhost:8000/api/stats | jq '{avg_processing_latency_ms, queue_depth}'

# 2. Burst: 50 alerts as fast as possible
curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 50}' | jq .generated

# 3. Immediately check queue depth
curl -s http://localhost:8000/api/stats | jq '{queue_depth, currently_processing}'

# 4. Monitor drain (run every 5s for ~60s)
for i in $(seq 1 12); do
  echo "$(date '+%H:%M:%S') $(curl -s http://localhost:8000/api/stats | jq '{queue_depth, avg_processing_latency_ms, accounting_balanced}')"
  sleep 5
done

# 5. Final check — should be balanced
curl -s http://localhost:8000/api/stats | jq '{accounting_balanced, avg_processing_latency_ms, queue_depth}'
```

### What to watch for

- Queue depth spikes then drains back to 0
- Latency increases during burst, recovers after
- `accounting_balanced` returns to `true` after drain
- No alerts permanently stuck

### Impact

If latency doesn't recover or `accounting_balanced` stays false after drain, there's a resource leak or bottleneck under load.

---

## Scenario 4: Duplicate Race Condition (ES Refresh Window)

**Production issue**: Duplicate alerts appear intermittently.

**What we're testing**: Two alerts with identical fingerprints submitted simultaneously may both get STORED because ES search hasn't refreshed yet when the second one checks.

**Why not automated**: Depends on sub-second timing between processor popping two items before ES refreshes its index (~1s interval).

### Steps

```bash
# 1. Confirm pipeline is idle
curl -s http://localhost:8000/api/stats | jq '{currently_queued, currently_processing}'

# 2. Pause processor — queue both alerts before processing starts
docker compose pause event_processor

# 3. Generate 2 alerts with SAME fingerprint
RESP_A=$(curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 1, "force_fingerprint": "racetest00000001"}')
ID_A=$(echo $RESP_A | jq -r '.alert_ids[0]')

RESP_B=$(curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 1, "force_fingerprint": "racetest00000001"}')
ID_B=$(echo $RESP_B | jq -r '.alert_ids[0]')

echo "Alert A: $ID_A"
echo "Alert B: $ID_B"

# 4. Unpause — processor pops both nearly simultaneously
docker compose unpause event_processor
sleep 5

# 5. Check results
echo "Alert A:" && curl -s http://localhost:8000/api/ledger/$ID_A | jq '.[].state'
echo "Alert B:" && curl -s http://localhost:8000/api/ledger/$ID_B | jq '.[].state'

# 6. How many docs with this fingerprint in ES?
curl -s "http://localhost:9200/security_alerts/_search" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"fingerprint":"racetest00000001"}}}' | jq '.hits.total.value'
```

### Expected Behavior

- **Ideal**: One STORED, one DUPLICATE_DROPPED
- **Race exposed**: Both STORED (ES count = 2) — duplicate appeared in investigation

### Impact

If both are STORED, this is the root cause of "duplicate alerts appear intermittently." Fix: refresh-before-check, or use ES `_create` with fingerprint as document ID.

---

## Scenario 5: Alerts Stuck in Processing (Reaper Recovery)

**Production issue**: Some alerts remain in processing indefinitely.

**What we're testing**: When the processor is killed mid-flight, alerts get stuck in PROCESSING. The reaper thread eventually marks them FAILED.

**Why not automated**: Reaper timeout is 60 minutes; even reduced to 1 minute, depends on background thread timing.

### Prerequisites

Temporarily set fast reaper in `docker-compose.yml` (event_processor environment):
```yaml
STUCK_TIMEOUT_MINUTES: "1"
REAPER_INTERVAL_SECONDS: "10"
```

Then: `docker compose up -d event_processor`

### Steps

```bash
# 1. Pause processor
docker compose pause event_processor

# 2. Generate 5 alerts
RESPONSE=$(curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 5}')
echo $RESPONSE | jq '.alert_ids'

# 3. Unpause — processor starts consuming
docker compose unpause event_processor

# 4. Kill immediately mid-processing
sleep 0.3
docker compose kill event_processor

# 5. Restart processor (reaper thread starts fresh)
docker compose up -d event_processor

# 6. Wait for reaper to fire (STUCK_TIMEOUT=1min + REAPER_INTERVAL=10s)
echo "Waiting 75s for reaper..."
sleep 75
```

### Verification

```bash
# Check for stuck alerts that got reaped:
curl -s http://localhost:8000/api/stats | jq '{total_failed, accounting_balanced}'

# Check specific alert ledger for stuck_timeout metadata:
# (use an alert_id from step 2)
curl -s http://localhost:8000/api/ledger/<alert_id> | jq '.[] | select(.state=="FAILED") | .metadata'
# Expected: {"reason": "stuck_timeout", "stuck_duration_minutes": ...}
```

### Impact

Confirms the reaper correctly recovers stuck alerts. Without it, these alerts stay in PROCESSING forever — invisible to investigation API but counted as "in flight."

**Remember to reset STUCK_TIMEOUT_MINUTES and REAPER_INTERVAL_SECONDS to defaults after this test.**

---

## Scenario 6: Processor Crash Mid-Transaction (ES/Ledger Inconsistency)

**Production issue**: Demonstrates a subtle data consistency gap that combines issues 1 + 5.

**What we're testing**: If the processor crashes after writing to ES but before updating the ledger to STORED, the alert exists in ES (visible in investigation) but the ledger says PROCESSING → eventually FAILED by reaper. Accounting diverges from reality.

**Why not automated**: Requires killing the process at an exact point mid-transaction.

### Prerequisites

Temporarily add a sleep to widen the crash window in `services/event_processor/main.py`:
```python
# After es.index() call, before log_ledger(STORED):
import time; time.sleep(5)
```

Rebuild: `docker compose up -d --build event_processor`

Also set fast reaper: `STUCK_TIMEOUT_MINUTES=1`, `REAPER_INTERVAL_SECONDS=10`

### Steps

```bash
# 1. Generate 1 alert
RESPONSE=$(curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" -d '{"count": 1}')
ALERT_ID=$(echo $RESPONSE | jq -r '.alert_ids[0]')
echo "Alert: $ALERT_ID"

# 2. Wait for it to hit the sleep (after ES write, before ledger update)
sleep 3

# 3. Kill processor mid-transaction
docker compose kill event_processor

# 4. Check: alert IS in ES
curl -s http://localhost:8000/api/alerts/$ALERT_ID | jq '.alert_id'
# Expected: returns the alert (200 OK)

# 5. Check: ledger shows only PROCESSING (no STORED)
curl -s http://localhost:8000/api/ledger/$ALERT_ID | jq '.[].state'
# Expected: PRODUCED, QUEUED, PROCESSING

# 6. Restart processor, wait for reaper
docker compose up -d event_processor
sleep 75

# 7. Ledger now shows FAILED (reaper), but alert is still in ES
curl -s http://localhost:8000/api/ledger/$ALERT_ID | jq '.[].state'
# Expected: PRODUCED, QUEUED, PROCESSING, FAILED
curl -s http://localhost:8000/api/alerts/$ALERT_ID | jq '.alert_id'
# Expected: still returns the alert (orphaned in ES)
```

### Impact

Accounting says FAILED, ES says it exists. Investigation API shows the alert but stats don't count it as STORED. This is the eventual consistency gap inherent in non-atomic cross-system writes.

**Remember to remove the `time.sleep(5)` and rebuild after this test.**

---

## Reset Between Scenarios

```bash
cd services/
docker compose down -v
docker compose up -d
sleep 15
curl -s http://localhost:8000/api/health | jq .
```
