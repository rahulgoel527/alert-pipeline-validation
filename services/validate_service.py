#!/usr/bin/env python3
"""
Host-side validation script for alert-pipeline-lab.
Run: pip install requests psycopg2-binary && python validate_service.py
"""
import sys
import time

import psycopg2
import requests

API = "http://localhost:8000"
DASHBOARD = "http://localhost:8050"
ES = "http://localhost:9200"
PG = {"host": "localhost", "port": 5432, "dbname": "alert_ledger", "user": "ledger", "password": "ledger123"}

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = []

def check(label, ok, detail=""):
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    results.append(ok)
    return ok


print("\n=== Alert Pipeline Lab — Validation ===\n")

# ── 0. Wait for stack to be ready ─────────────────────────────────────────────
print("0. Waiting for stack to be ready (up to 120s)...")
deadline = time.time() + 120
ready = False
while time.time() < deadline:
    try:
        r = requests.get(f"{API}/api/health", timeout=3)
        data = r.json()
        svcs = data.get("services", {})
        if r.status_code == 200 and svcs.get("postgres") and svcs.get("elasticsearch") and svcs.get("redis"):
            ready = True
            break
    except Exception:
        pass
    time.sleep(2)

if not ready:
    print(f"  [{FAIL}] Stack not ready after 120s — aborting")
    sys.exit(1)
print(f"  [{PASS}] Stack ready\n")

# ── 1. Service reachability ──────────────────────────────────────────────────
print("1. Service reachability")

try:
    r = requests.get(f"{API}/api/health", timeout=5)
    data = r.json()
    check("API /api/health returns 200", r.status_code == 200)
    check("API reports postgres healthy", data.get("services", {}).get("postgres") is True)
    check("API reports elasticsearch healthy", data.get("services", {}).get("elasticsearch") is True)
    check("API reports redis healthy", data.get("services", {}).get("redis") is True)
except Exception as e:
    check("API /api/health reachable", False, str(e))

try:
    r = requests.get(DASHBOARD, timeout=5)
    check("Dashboard HTTP 200", r.status_code == 200)
except Exception as e:
    check("Dashboard reachable", False, str(e))

try:
    r = requests.get(f"{ES}/_cluster/health", timeout=5)
    status = r.json().get("status", "")
    check("Elasticsearch cluster healthy", status in ("green", "yellow"), f"status={status}")
except Exception as e:
    check("Elasticsearch reachable", False, str(e))

try:
    conn = psycopg2.connect(**PG)
    conn.close()
    check("Postgres direct connection", True)
except Exception as e:
    check("Postgres direct connection", False, str(e))

# ── 2. Generate alerts ────────────────────────────────────────────────────────
print("\n2. Alert generation")

try:
    r = requests.post(f"{API}/api/generate", json={"count": 10, "source": "HealthCheck"}, timeout=10)
    data = r.json()
    generated = data.get("generated", 0)
    check("POST /api/generate returns 200", r.status_code == 200)
    check(f"Generated count == 10 (got {generated})", generated == 10)
    alert_ids = data.get("alert_ids", [])
    check("Response contains 10 alert_ids", len(alert_ids) == 10, f"got {len(alert_ids)}")
except Exception as e:
    check("POST /api/generate", False, str(e))
    alert_ids = []

# ── 3. Wait for accounting_balanced ──────────────────────────────────────────
print("\n3. Pipeline accounting (30s timeout)")

balanced = False
deadline = time.time() + 30
last_stats = {}
while time.time() < deadline:
    try:
        r = requests.get(f"{API}/api/stats", timeout=5)
        last_stats = r.json()
        if last_stats.get("accounting_balanced"):
            balanced = True
            break
    except Exception:
        pass
    time.sleep(2)

check("accounting_balanced == True within 30s", balanced,
      f"stored={last_stats.get('total_stored')}, failed={last_stats.get('total_failed')}, "
      f"dup={last_stats.get('total_duplicates')}, unaccounted={last_stats.get('unaccounted')}, "
      f"queue_depth={last_stats.get('queue_depth')}")

# ── 4. Alerts in Elasticsearch ────────────────────────────────────────────────
print("\n4. Elasticsearch alert storage")

try:
    r = requests.get(f"{API}/api/alerts", timeout=5)
    alerts = r.json()
    check("GET /api/alerts returns list", isinstance(alerts, list))
    check("At least 1 alert in ES", len(alerts) >= 1, f"got {len(alerts)}")
except Exception as e:
    check("GET /api/alerts", False, str(e))

# ── 5. Ledger state transitions ───────────────────────────────────────────────
print("\n5. Postgres ledger state transitions")

if alert_ids:
    sample_id = alert_ids[0]
    terminal = {"STORED", "FAILED", "DUPLICATE_DROPPED"}
    states = []
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = requests.get(f"{API}/api/ledger/{sample_id}", timeout=5)
            if r.status_code == 200:
                states = [row["state"] for row in r.json()]
                if set(states) & terminal:
                    break
        except Exception:
            pass
        time.sleep(1)
    check(f"Ledger for {sample_id[:8]}… has entries", len(states) >= 2, f"states: {states}")
    check("PRODUCED state present", "PRODUCED" in states)
    check("QUEUED state present", "QUEUED" in states)
    check("Terminal state reached", bool(set(states) & terminal), f"states: {states}")
    print(f"    State transitions: {' → '.join(states)}")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n=== Summary ===")
total = len(results)
passed = sum(results)
failed = total - passed

print(f"  Checks: {total}   Passed: {passed}   Failed: {failed}")
print()
if failed == 0:
    print(f"  Overall: {PASS}")
    sys.exit(0)
else:
    print(f"  Overall: {FAIL}")
    sys.exit(1)
