import json
import os
import threading
import time

from elasticsearch.exceptions import NotFoundError

from common import (
    QUEUE_KEY, get_pg_conn, log,
    wait_for_postgres, wait_for_redis, wait_for_elasticsearch,
)

SERVICE = "event_processor"
ES_INDEX = os.environ["ES_INDEX"]

STUCK_TIMEOUT_MINUTES = int(os.environ.get("STUCK_TIMEOUT_MINUTES", "60"))
REAPER_INTERVAL_SECONDS = int(os.environ.get("REAPER_INTERVAL_SECONDS", "300"))
MAX_ES_RETRIES = int(os.environ.get("MAX_ES_RETRIES", "3"))

def log_ledger(pg_conn, alert_id, state, metadata=None):
    with pg_conn:
        with pg_conn.cursor() as cur:
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

    except Exception as exc:
        log(SERVICE, f"is_duplicate check failed for fingerprint {fingerprint}: {exc}")
        return False

def process_alert(alert, es, pg_conn):
    """Process a single alert through the pipeline.

    Args:
        alert: Alert dict from the queue.
        es: Elasticsearch client.
        pg_conn: Postgres connection.
    """
    alert_id = alert.get("alert_id", "unknown")
    fingerprint = alert.get("fingerprint", "")

    log_ledger(pg_conn, alert_id, "PROCESSING")
    log(SERVICE, f"Processing alert {alert_id} ({alert.get('alert_type')}, {alert.get('severity')})")

    if is_duplicate(es, fingerprint):
        log_ledger(pg_conn, alert_id, "DUPLICATE_DROPPED", {"fingerprint": fingerprint})
        log(SERVICE, f"Duplicate dropped alert {alert_id} (fingerprint={fingerprint})")
        return

    last_exc = None
    processing_start = time.time()
    for attempt in range(MAX_ES_RETRIES):
        try:
            es.index(index=ES_INDEX, id=alert_id, document=alert)
            duration_ms = int((time.time() - processing_start) * 1000)
            log_ledger(pg_conn, alert_id, "STORED", {"processing_duration_ms": duration_ms})
            log(SERVICE, f"Stored alert {alert_id} (attempt {attempt + 1}, {duration_ms}ms)")
            return
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_ES_RETRIES - 1:
                backoff = 0.5 * (2 ** attempt)
                log(SERVICE, f"ES write failed for {alert_id} (attempt {attempt + 1}/{MAX_ES_RETRIES}), retrying in {backoff}s: {exc}")
                time.sleep(backoff)

    log_ledger(pg_conn, alert_id, "FAILED", {"error": str(last_exc), "attempt_count": MAX_ES_RETRIES})
    log(SERVICE, f"FAILED to store alert {alert_id} after {MAX_ES_RETRIES} attempts: {last_exc}")

def reaper_loop():
    """Background thread: finds alerts stuck in PROCESSING beyond STUCK_TIMEOUT_MINUTES
    and writes FAILED to the ledger so accounting can balance."""
    log(SERVICE, f"Reaper started — will fail PROCESSING alerts older than {STUCK_TIMEOUT_MINUTES}m, "
        f"checking every {REAPER_INTERVAL_SECONDS}s")
    while True:
        time.sleep(REAPER_INTERVAL_SECONDS)
        pg_conn = None
        try:
            pg_conn = get_pg_conn()
            with pg_conn.cursor() as pg_cursor:
                pg_cursor.execute("""
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
                stuck = pg_cursor.fetchall()

                if not stuck:
                    continue
                log(SERVICE, f"Reaper: found {len(stuck)} stuck alert(s), marking FAILED")
                for alert_id, stuck_minutes in stuck:
                    log_ledger(
                        pg_conn,
                        alert_id,
                        "FAILED",
                        {
                            "reason": "stuck_timeout",
                            "stuck_duration_minutes": round(stuck_minutes, 1),
                        },
                    )
                    log(SERVICE, f"Reaper: marked {alert_id} as FAILED (stuck {round(stuck_minutes, 1)}m)")

        except Exception as exc:
            log(SERVICE, f"Reaper error: {exc}")
        finally:
            if pg_conn is not None:
                pg_conn.close()

def main():
    wait_for_postgres(SERVICE)
    redis_client = wait_for_redis(SERVICE)
    es_client = wait_for_elasticsearch(SERVICE)
    reaper = threading.Thread(target=reaper_loop, daemon=True)
    reaper.start()
    log(SERVICE, "Processor started, polling Redis...")
    redis_client.set("processor:heartbeat", int(time.time()), ex=150)
    while True:
        result = redis_client.blpop(QUEUE_KEY, timeout=5)
        redis_client.set("processor:heartbeat", int(time.time()), ex=150)
        if result is None:
            continue
        _, raw = result
        alert_id = "unknown"
        pg_conn = get_pg_conn()
        try:
            alert = json.loads(raw)
            alert_id = alert.get("alert_id", "unknown")
            process_alert(alert, es_client, pg_conn)
        except Exception as exc:
            log(SERVICE, f"Unhandled error processing alert {alert_id}: {exc}")
            if alert_id != "unknown":
                log_ledger(pg_conn, alert_id, "FAILED", {"error": str(exc)})
        finally:
            pg_conn.close()

if __name__ == "__main__":
    main()
