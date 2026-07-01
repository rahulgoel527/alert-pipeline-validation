import os
import time
import urllib.request
import urllib.error
from contextlib import asynccontextmanager

from elasticsearch import NotFoundError
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from common import (
    QUEUE_KEY, get_pg_conn, get_es, get_redis, log,
    wait_for_postgres, wait_for_elasticsearch, wait_for_redis,
    reset_pipeline,
)
from common.stats import get_pipeline_stats

SERVICE = "dashboard"
ES_INDEX = os.environ["ES_INDEX"]

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_postgres(SERVICE)
    wait_for_elasticsearch(SERVICE)
    wait_for_redis(SERVICE)
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
        stats = get_pipeline_stats(conn)
    except Exception:
        return dict(_EMPTY_STATS)
    finally:
        conn.close()

    try:
        queue_depth = get_redis().llen(QUEUE_KEY)
    except Exception:
        queue_depth = -1

    stats["queue_depth"] = queue_depth
    return stats


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
    except Exception:
        return []
    finally:
        conn.close()


@app.post("/dashboard/reset")
def dashboard_reset():
    try:
        reset_pipeline(SERVICE)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"reset": True}


def check_processor_status(redis_client):
    raw = redis_client.get("processor:heartbeat")
    if raw is None:
        return {"status": "not_started", "last_seen_s": None}
    try:
        ts = int(raw)
    except (ValueError, TypeError):
        return {"status": "unknown", "last_seen_s": None}
    last_seen_s = int(time.time()) - ts
    if last_seen_s < 30:
        status = "healthy"
    elif last_seen_s <= 120:
        status = "stalled"
    else:
        status = "down"
    return {"status": status, "last_seen_s": last_seen_s}


@app.get("/dashboard/health")
def dashboard_health():
    result = {}

    # Redis + processor
    try:
        r = get_redis()
        r.ping()
        result["redis"] = {"status": "healthy"}
        result["processor"] = check_processor_status(r)
    except Exception:
        result["redis"] = {"status": "down"}
        result["processor"] = {"status": "unknown", "last_seen_s": None}

    # Postgres
    try:
        conn = get_pg_conn()
        conn.close()
        result["postgres"] = {"status": "healthy"}
    except Exception:
        result["postgres"] = {"status": "down"}

    # Elasticsearch
    try:
        es = get_es()
        es.cluster.health()
        result["elasticsearch"] = {"status": "healthy"}
    except Exception:
        result["elasticsearch"] = {"status": "down"}

    # API service (internal Docker network)
    try:
        start = time.time()
        req = urllib.request.Request(
            "http://api:8000/api/health",
            headers={"User-Agent": "dashboard-health-check"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            latency_ms = round((time.time() - start) * 1000)
            result["api"] = {"status": "healthy", "latency_ms": latency_ms}
    except Exception:
        result["api"] = {"status": "unreachable", "latency_ms": None}

    return result


@app.get("/dashboard/alerts")
def dashboard_alerts(size: int = 20):
    return fetch_recent_alerts(limit=min(size, 100))


@app.get("/dashboard/stats")
def dashboard_stats():
    return fetch_stats()


@app.get("/dashboard/stuck")
def dashboard_stuck():
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
