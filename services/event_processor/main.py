import json
import os
import threading
import time

from elasticsearch.exceptions import NotFoundError

from common import (
    QUEUE_KEY, get_pg_conn, get_es, get_redis, log,
    wait_for_postgres, wait_for_redis, wait_for_elasticsearch,
)

SERVICE = "event_processor"
ES_INDEX = os.environ["ES_INDEX"]

STUCK_TIMEOUT_MINUTES = int(os.environ.get("STUCK_TIMEOUT_MINUTES", "60"))
REAPER_INTERVAL_SECONDS = int(os.environ.get("REAPER_INTERVAL_SECONDS", "300"))

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


def process_alert(alert, es, pg_conn):
    """Process a single alert through the pipeline.

    Args:
        alert: Alert dict from the queue.
        es: Elasticsearch client.
        pg_conn: Postgres connection (caller owns lifecycle).
    """
    alert_id = alert.get("alert_id", "unknown")
    fingerprint = alert.get("fingerprint", "")

    log_ledger(pg_conn, alert_id, "PROCESSING")
    log(SERVICE, f"Processing alert {alert_id} ({alert.get('alert_type')}, {alert.get('severity')})")

    if is_duplicate(es, fingerprint):
        log_ledger(pg_conn, alert_id, "DUPLICATE_DROPPED", {"fingerprint": fingerprint})
        log(SERVICE, f"Duplicate dropped alert {alert_id} (fingerprint={fingerprint})")
        return

    try:
        es.index(index=ES_INDEX, id=alert_id, document=alert)
        log_ledger(pg_conn, alert_id, "STORED")
        log(SERVICE, f"Stored alert {alert_id}")
    except Exception as exc:
        log_ledger(pg_conn, alert_id, "FAILED", {"error": str(exc)})
        log(SERVICE, f"FAILED to store alert {alert_id}: {exc}")


def reaper_loop():
    """Background thread: finds alerts stuck in PROCESSING beyond STUCK_TIMEOUT_MINUTES
    and writes FAILED to the ledger so accounting can balance."""
    log(SERVICE, f"Reaper started — will fail PROCESSING alerts older than {STUCK_TIMEOUT_MINUTES}m, "
        f"checking every {REAPER_INTERVAL_SECONDS}s")
    while True:
        time.sleep(REAPER_INTERVAL_SECONDS)
        try:
            conn = get_pg_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
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

                log(SERVICE, f"Reaper: found {len(stuck)} stuck alert(s), marking FAILED")
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
                        log(SERVICE, f"Reaper: marked {alert_id} as FAILED "
                            f"(stuck {round(stuck_minutes, 1)}m)")
                finally:
                    conn2.close()
            finally:
                conn.close()
        except Exception as exc:
            log(SERVICE, f"Reaper error: {exc}")


def main():
    wait_for_postgres(SERVICE)
    r = wait_for_redis(SERVICE)
    es = wait_for_elasticsearch(SERVICE)

    reaper = threading.Thread(target=reaper_loop, daemon=True)
    reaper.start()

    log(SERVICE, "Processor started, polling Redis...")
    while True:
        result = r.blpop(QUEUE_KEY, timeout=5)
        if result is None:
            continue
        _, raw = result
        alert_id = "unknown"
        try:
            alert = json.loads(raw)
            alert_id = alert.get("alert_id", "unknown")
            conn = get_pg_conn()
            try:
                process_alert(alert, es, conn)
            finally:
                conn.close()
        except Exception as exc:
            log(SERVICE, f"Unhandled error processing alert {alert_id}: {exc}")
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
