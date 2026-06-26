# Load Test

## Prerequisites

```bash
pip install -r tests/load/requirements.txt
```

## Run

```bash
locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 2m --host http://localhost:8000
```

- `-u 20`: 20 concurrent users
- `-r 5`: spawn 5 users/second
- `-t 2m`: run for 2 minutes

## What to Measure

| Metric | Target |
|--------|--------|
| POST /api/generate P50 | < 50ms |
| POST /api/generate P95 | < 200ms |
| POST /api/generate P99 | < 500ms |
| GET /api/stats P95 | < 100ms |
| Queue depth during burst | Monitor growth rate |
| Drain time after burst | Time until `accounting_balanced=true` |

## Post-Burst Validation

After the load test completes, poll `/api/stats` until balanced:

```python
import requests, time
while True:
    s = requests.get("http://localhost:8000/api/stats").json()
    print(f"queued={s['currently_queued']} processing={s['currently_processing']} balanced={s['accounting_balanced']}")
    if s['accounting_balanced']:
        print("Pipeline drained!")
        break
    time.sleep(2)
```
