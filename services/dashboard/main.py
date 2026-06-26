import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg2
import redis as redis_lib
from elasticsearch import Elasticsearch, NotFoundError
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

SERVICE = "dashboard"
POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
ELASTICSEARCH_HOST = os.environ["ELASTICSEARCH_HOST"]
ES_INDEX = os.environ["ES_INDEX"]
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
QUEUE_KEY = "alert_queue"

templates = Jinja2Templates(directory="templates")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{SERVICE}] {msg}", flush=True)


def get_pg_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST, dbname=POSTGRES_DB,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD
    )


def get_es():
    return Elasticsearch(f"http://{ELASTICSEARCH_HOST}:9200")


def get_redis():
    return redis_lib.Redis(host=REDIS_HOST, decode_responses=True)


def wait_for_postgres():
    while True:
        try:
            conn = get_pg_conn(); conn.close(); log("Postgres ready"); return
        except psycopg2.OperationalError:
            log("Waiting for Postgres..."); time.sleep(1)


def wait_for_elasticsearch():
    while True:
        try:
            es = get_es()
            if es.cluster.health()["status"] in ("green", "yellow"):
                log("Elasticsearch ready"); return
        except Exception:
            pass
        log("Waiting for Elasticsearch..."); time.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_postgres()
    wait_for_elasticsearch()
    yield


app = FastAPI(lifespan=lifespan)


_EMPTY_STATS = {
    "total_produced": 0,
    "currently_queued": 0,
    "currently_processing": 0,
    "total_stored": 0,
    "total_failed": 0,
    "total_duplicates": 0,
    "unaccounted": 0,
    "accounting_balanced": True,
    "last_event_at": None,
    "avg_processing_latency_ms": 0.0,
    "queue_depth": 0,
}


def fetch_stats():
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
                    SELECT
                        COUNT(*) FILTER (WHERE state = 'QUEUED')            AS currently_queued,
                        COUNT(*) FILTER (WHERE state = 'PROCESSING')        AS currently_processing,
                        COUNT(*) FILTER (WHERE state = 'STORED')            AS total_stored,
                        COUNT(*) FILTER (WHERE state = 'FAILED')            AS total_failed,
                        COUNT(*) FILTER (WHERE state = 'DUPLICATE_DROPPED') AS total_duplicates
                    FROM latest
                """)
                row = cur.fetchone()
                currently_queued, currently_processing, total_stored, total_failed, total_duplicates = (
                    int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4])
                )

                cur.execute(
                    "SELECT COUNT(DISTINCT alert_id) FROM alert_ledger WHERE state = 'PRODUCED'"
                )
                total_produced = int(cur.fetchone()[0])

                accounted = total_stored + total_failed + total_duplicates + currently_queued + currently_processing
                unaccounted = max(0, total_produced - accounted)

                cur.execute("SELECT MAX(timestamp) FROM alert_ledger")
                last_ts = cur.fetchone()[0]
                last_event_at = last_ts.isoformat() + "Z" if last_ts else None

                cur.execute("""
                    SELECT AVG(EXTRACT(EPOCH FROM (s.timestamp - p.timestamp)) * 1000)
                    FROM alert_ledger p
                    JOIN alert_ledger s ON p.alert_id = s.alert_id
                    WHERE p.state = 'PROCESSING' AND s.state = 'STORED'
                """)
                avg_ms = cur.fetchone()[0]
    except psycopg2.Error:
        return dict(_EMPTY_STATS)
    finally:
        conn.close()

    try:
        queue_depth = get_redis().llen(QUEUE_KEY)
    except Exception:
        queue_depth = -1

    return {
        "total_produced": total_produced,
        "currently_queued": currently_queued,
        "currently_processing": currently_processing,
        "total_stored": total_stored,
        "total_failed": total_failed,
        "total_duplicates": total_duplicates,
        "unaccounted": unaccounted,
        "accounting_balanced": (unaccounted == 0),
        "last_event_at": last_event_at,
        "avg_processing_latency_ms": round(float(avg_ms), 1) if avg_ms else 0.0,
        "queue_depth": queue_depth,
    }


def fetch_recent_alerts(limit=20):
    es = get_es()
    try:
        resp = es.search(
            index=ES_INDEX,
            query={"match_all": {}},
            size=limit,
            sort=[{"timestamp": {"order": "desc"}}],
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]
    except NotFoundError:
        return []
    except Exception:
        return []


def fetch_stuck_alerts(threshold_seconds=60):
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (alert_id) alert_id, state, timestamp
                    FROM alert_ledger ORDER BY alert_id, id DESC
                )
                SELECT alert_id, timestamp FROM latest
                WHERE state = 'PROCESSING'
                  AND NOW() - timestamp > make_interval(secs => %s)
            """, (threshold_seconds,))
            rows = cur.fetchall()
            return [{"alert_id": r[0], "since": r[1].isoformat()} for r in rows]
    except psycopg2.Error:
        return []
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats():
    return fetch_stats()


@app.get("/api/stuck")
def api_stuck():
    return fetch_stuck_alerts()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = fetch_stats()
    recent = fetch_recent_alerts()
    stuck = fetch_stuck_alerts()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "recent_alerts": recent,
            "stuck_alerts": stuck,
        },
    )
