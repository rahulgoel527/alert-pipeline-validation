import os
import random
import time

import requests

from common import log

SERVICE = "event_generator"
API_HOST = os.environ.get("API_HOST", "api")
API_URL = f"http://{API_HOST}:8000"


def wait_for_services():
    session = requests.Session()
    while True:
        try:
            resp = session.get(f"{API_URL}/api/health", timeout=3)
            if resp.ok:
                not_ready = [svc for svc, up in resp.json().get("services", {}).items() if not up]
                if not not_ready:
                    log(SERVICE, "All services ready")
                    break
                log(SERVICE, f"Services not ready yet: {not_ready}")
            else:
                log(SERVICE, f"API health returned {resp.status_code}, waiting...")
        except requests.RequestException as e:
            log(SERVICE, f"API not reachable yet ({e}), waiting...")
        time.sleep(2)


def generate_alerts(count=1, force_fingerprint=None):
    body = {"count": count, "payload": {"source": "generator"}}
    if force_fingerprint:
        body["force_fingerprint"] = force_fingerprint

    try:
        resp = requests.post(f"{API_URL}/api/generate", json=body, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(SERVICE, f"generate failed: {e}")
        return None

    data = resp.json()
    alert_ids = data.get("alert_ids", [])
    fingerprints = data.get("fingerprints", [])
    dup_note = " (forced duplicate)" if force_fingerprint else ""
    log(SERVICE, f"Generated {len(alert_ids)} alert(s){dup_note}: {', '.join(alert_ids)}")

    return fingerprints[0] if fingerprints else None


def main():
    wait_for_services()

    # Seed: 5 alerts on startup
    last_fp = None
    for _ in range(5):
        fp = generate_alerts(count=1)
        if fp:
            last_fp = fp

    # Continuous: 1 alert every 10 seconds, 10% chance forced duplicate
    while True:
        time.sleep(10)
        is_dup = random.random() < 0.10
        fp = generate_alerts(
            count=1,
            force_fingerprint=last_fp if (is_dup and last_fp) else None,
        )
        if fp:
            last_fp = fp


if __name__ == "__main__":
    main()
