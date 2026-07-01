import os
from contextlib import asynccontextmanager

from elasticsearch import NotFoundError
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from common import (
    QUEUE_KEY, get_pg_conn, get_es, get_redis, log,
    wait_for_postgres, wait_for_elasticsearch,
)
from common.stats import get_pipeline_stats

SERVICE = "dashboard"
ES_INDEX = os.environ["ES_INDEX"]

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_postgres(SERVICE)
    wait_for_elasticsearch(SERVICE)
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
