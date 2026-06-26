import json
import os
import random
import threading
import time
from datetime import datetime

import psycopg2
import redis
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError

SERVICE = "event_processor"
REDIS_HOST = os.environ["REDIS_HOST"]
POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
ELASTICSEARCH_HOST = os.environ["ELASTICSEARCH_HOST"]
ES_INDEX = os.environ["ES_INDEX"]
QUEUE_KEY = "alert_queue"

# Stuck-alert reaper: how long before a PROCESSING alert is declared failed
STUCK_TIMEOUT_MINUTES = int(os.environ.get("STUCK_TIMEOUT_MINUTES", "60"))
# How often the reaper checks for stuck alerts
REAPER_INTERVAL_SECONDS = int(os.environ.get("REAPER_INTERVAL_SECONDS", "300"))


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{SERVICE}] {msg}", flush=True)


def wait_for_postgres():
    while True:
        try:
            conn = psycopg2.connect(
                host=POSTGRES_HOST, dbname=POSTGRES_DB,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD
            )
            conn.close()
            log("Postgres ready")
            return
        except psycopg2.OperationalError:
            log("Waiting for Postgres...")
            time.sleep(1)


def wait_for_redis():
    r = redis.Redis(host=REDIS_HOST, decode_responses=True)
    while True:
        try:
            r.ping()
            log("Redis ready")
            return r
        except redis.ConnectionError:
            log("Waiting for Redis...")
            time.sleep(1)


def wait_for_elasticsearch():
    es = Elasticsearch(f"http://{ELASTICSEARCH_HOST}:9200")
    while True:
        try:
            health = es.cluster.health()
            if health["status"] in ("green", "yellow"):
                log("Elasticsearch ready")
                return es
        except Exception:
            pass
        log("Waiting for Elasticsearch...")
        time.sleep(2)


def get_pg_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST, dbname=POSTGRES_DB,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD
    )


def log_ledger(conn, alert_id, state, metadata=None):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) VALUES (%s, %s, %s, %s)",
                (alert_id, state, SERVICE, json.dumps(metadata or {}))
            )


def is_duplicate(es, fingerprint):
    try:
        resp = es.search(
            index=ES_INDEX,
            query={"term": {"fingerprint": fingerprint}},
            size=1,
        )
        return resp["hits"]["total"]["value"] > 0
    except NotFoundError:
        return False
    except Exception:
        return False


def process_alert(es, alert):
    alert_id = alert.get("alert_id", "unknown")
    fingerprint = alert.get("fingerprint", "")

    conn = get_pg_conn()
    try:
        log_ledger(conn, alert_id, "PROCESSING")
        log(f"Processing alert {alert_id} ({alert.get('alert_type')}, {alert.get('severity')})")

        # Simulate stuck/hung worker (2%) — intentional scenario for testing stuck-alert detection.
        # The reaper thread will transition this to FAILED after STUCK_TIMEOUT_MINUTES.
        if random.random() < 0.02:
            log(f"STUCK alert {alert_id} — simulated hung worker (will timeout in {STUCK_TIMEOUT_MINUTES}m)")
            return

        # Simulate occasional failure (5%)
        if random.random() < 0.05:
            error_msg = "Simulated processing failure"
            log_ledger(conn, alert_id, "FAILED", {"error": error_msg})
            log(f"FAILED alert {alert_id}: {error_msg}")
            return

        # Simulate occasional slowness (10%)
        if random.random() < 0.10:
            delay = random.uniform(2.0, 10.0)
            log(f"Slow processing alert {alert_id} (delay {delay:.1f}s)")
            time.sleep(delay)

        # Deduplication check
        if is_duplicate(es, fingerprint):
            log_ledger(conn, alert_id, "DUPLICATE_DROPPED", {"fingerprint": fingerprint})
            log(f"Duplicate dropped alert {alert_id} (fingerprint={fingerprint})")
            return

        # Write to Elasticsearch
        try:
            es.index(index=ES_INDEX, id=alert_id, document=alert)
            log_ledger(conn, alert_id, "STORED")
            log(f"Stored alert {alert_id}")
        except Exception as exc:
            log_ledger(conn, alert_id, "FAILED", {"error": str(exc)})
            log(f"FAILED to store alert {alert_id}: {exc}")

    finally:
        conn.close()


def reaper_loop():
    """Background thread: finds alerts stuck in PROCESSING beyond STUCK_TIMEOUT_MINUTES
    and writes FAILED to the ledger so accounting can balance."""
    log(f"Reaper started — will fail PROCESSING alerts older than {STUCK_TIMEOUT_MINUTES}m, "
        f"checking every {REAPER_INTERVAL_SECONDS}s")
    while True:
        time.sleep(REAPER_INTERVAL_SECONDS)
        try:
            conn = get_pg_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        # Find alert_ids whose latest ledger state is still PROCESSING
                        # and that PROCESSING row is older than the timeout threshold.
                        cur.execute("""
                            WITH latest AS (
                                SELECT DISTINCT ON (alert_id) alert_id, state, timestamp
                                FROM alert_ledger
                                ORDER BY alert_id, id DESC
                            )
                            SELECT alert_id,
                                   EXTRACT(EPOCH FROM (NOW() - timestamp)) / 60 AS stuck_minutes
                            FROM latest
                            WHERE state = 'PROCESSING'
                              AND NOW() - timestamp > make_interval(mins => %s)
                        """, (STUCK_TIMEOUT_MINUTES,))
                        stuck = cur.fetchall()

                if not stuck:
                    continue

                log(f"Reaper: found {len(stuck)} stuck alert(s), marking FAILED")
                conn2 = get_pg_conn()
                try:
                    for alert_id, stuck_minutes in stuck:
                        with conn2:
                            with conn2.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) "
                                    "VALUES (%s, %s, %s, %s)",
                                    (alert_id, "FAILED", SERVICE,
                                     json.dumps({"reason": "stuck_timeout",
                                                 "stuck_duration_minutes": round(stuck_minutes, 1)}))
                                )
                        log(f"Reaper: marked {alert_id} as FAILED "
                            f"(stuck {round(stuck_minutes, 1)}m)")
                finally:
                    conn2.close()
            finally:
                conn.close()
        except Exception as exc:
            log(f"Reaper error: {exc}")


def main():
    wait_for_postgres()
    r = wait_for_redis()
    es = wait_for_elasticsearch()

    reaper = threading.Thread(target=reaper_loop, daemon=True)
    reaper.start()

    log("Processor started, polling Redis...")
    while True:
        result = r.blpop(QUEUE_KEY, timeout=5)
        if result is None:
            continue
        _, raw = result
        alert_id = "unknown"
        try:
            alert = json.loads(raw)
            alert_id = alert.get("alert_id", "unknown")
            process_alert(es, alert)
        except Exception as exc:
            log(f"Unhandled error processing alert {alert_id}: {exc}")
            if alert_id != "unknown":
                try:
                    conn = get_pg_conn()
                    try:
                        log_ledger(conn, alert_id, "FAILED", {"error": str(exc)})
                    finally:
                        conn.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
