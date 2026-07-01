import json
import os
import random
import time
from contextlib import asynccontextmanager

from elasticsearch import NotFoundError
from fastapi import Body, FastAPI, HTTPException, Query

from common import (
    QUEUE_KEY, get_pg_conn, get_es, get_redis, log,
    wait_for_postgres, wait_for_elasticsearch,
)
from common.stats import get_pipeline_stats
from common.alert_factory import (
    ALERT_TYPES, SEVERITIES, build_alert,
)

SERVICE = "api"
ES_INDEX = os.environ["ES_INDEX"]

ES_MAPPINGS = {
    "properties": {
        "alert_id":    {"type": "keyword"},
        "title":       {"type": "text"},
        "description": {"type": "text"},
        "severity":    {"type": "keyword"},
        "source_ip":   {"type": "ip"},
        "dest_ip":     {"type": "ip"},
        "alert_type":  {"type": "keyword"},
        "timestamp":   {"type": "date"},
        "fingerprint": {"type": "keyword"},
        "source":      {"type": "keyword"},
        "metadata":    {"type": "object", "enabled": False},
    }
}


def init_postgres_schema():
    conn = get_pg_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS alert_ledger (
                        id              SERIAL PRIMARY KEY,
                        alert_id        VARCHAR(64) NOT NULL,
                        state           VARCHAR(30) NOT NULL,
                        timestamp       TIMESTAMP DEFAULT NOW(),
                        source_service  VARCHAR(30) NOT NULL,
                        metadata        JSONB DEFAULT '{}'
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_id ON alert_ledger(alert_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_state ON alert_ledger(state)")
                cur.execute("TRUNCATE TABLE alert_ledger RESTART IDENTITY")
        log(SERVICE, "Postgres schema ready — ledger cleared")
    finally:
        conn.close()


def init_es_index(es):
    try:
        if es.indices.exists(index=ES_INDEX):
            es.indices.delete(index=ES_INDEX)
            log(SERVICE, f"ES index '{ES_INDEX}' dropped")
        es.indices.create(index=ES_INDEX, mappings=ES_MAPPINGS)
        log(SERVICE, f"ES index '{ES_INDEX}' created fresh")
    except Exception as exc:
        log(SERVICE, f"FATAL: failed to initialise ES index '{ES_INDEX}': {exc}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_postgres(SERVICE)
    init_postgres_schema()

    es = wait_for_elasticsearch(SERVICE)
    init_es_index(es)

    r = get_redis()
    r.delete(QUEUE_KEY)
    log(SERVICE, f"Redis queue '{QUEUE_KEY}' flushed")

    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
def health():
    services = {}
    try:
        conn = get_pg_conn(); conn.close(); services["postgres"] = True
    except Exception:
        services["postgres"] = False
    try:
        es = get_es(); es.cluster.health(); services["elasticsearch"] = True
    except Exception:
        services["elasticsearch"] = False
    try:
        r = get_redis(); r.ping(); services["redis"] = True
    except Exception:
        services["redis"] = False
    return {"status": "ok", "services": services}


@app.get("/api/alerts")
def list_alerts(size: int = Query(100, ge=1, le=1000)):
    es = get_es()
    try:
        resp = es.search(index=ES_INDEX, query={"match_all": {}}, size=size,
                         sort=[{"timestamp": {"order": "desc"}}])
        return [hit["_source"] for hit in resp["hits"]["hits"]]
    except NotFoundError:
        return []


@app.get("/api/alerts/search")
def search_alerts(
    q: str = Query(None),
    severity: str = Query(None),
    alert_type: str = Query(None),
    source: str = Query(None),
    size: int = Query(100, ge=1, le=1000),
):
    es = get_es()
    filters = []
    if severity:
        filters.append({"term": {"severity": severity}})
    if alert_type:
        filters.append({"term": {"alert_type": alert_type}})
    if source:
        filters.append({"term": {"source": source}})
    if q:
        query = {
            "bool": {
                "must": [{"multi_match": {"query": q, "fields": ["title", "description"]}}],
                "filter": filters,
            }
        }
    elif filters:
        query = {"bool": {"filter": filters}}
    else:
        query = {"match_all": {}}
    try:
        resp = es.search(index=ES_INDEX, query=query, size=size,
                         sort=[{"timestamp": {"order": "desc"}}])
        return [hit["_source"] for hit in resp["hits"]["hits"]]
    except NotFoundError:
        return []


@app.get("/api/alerts/{alert_id}")
def get_alert(alert_id: str):
    es = get_es()
    try:
        doc = es.get(index=ES_INDEX, id=alert_id)
        return doc["_source"]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Alert not found")


@app.get("/api/stats")
def get_stats():
    conn = get_pg_conn()
    try:
        stats = get_pipeline_stats(conn)
    finally:
        conn.close()

    try:
        queue_depth = get_redis().llen(QUEUE_KEY)
    except Exception:
        queue_depth = -1

    stats["queue_depth"] = queue_depth
    return stats


@app.get("/api/ledger/{alert_id}")
def get_ledger(alert_id: str):
    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, alert_id, state, timestamp, source_service, metadata FROM alert_ledger WHERE alert_id=%s ORDER BY id",
                (alert_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail="Alert not found in ledger")
    return [
        {
            "id": r[0],
            "alert_id": r[1],
            "state": r[2],
            "timestamp": r[3].isoformat(),
            "source_service": r[4],
            "metadata": r[5],
        }
        for r in rows
    ]


@app.post("/api/generate")
def generate_alerts(body: dict = Body(default=None)):
    count = 1
    force_fingerprint = None
    payload_override = None
    if body:
        count = max(1, min(int(body.get("count", 1)), 100))
        force_fingerprint = body.get("force_fingerprint")
        payload_override = body.get("payload")

    source = None
    if payload_override and "source" in payload_override:
        source = payload_override["source"]

    r = get_redis()
    if r.llen(QUEUE_KEY) >= 200:
        raise HTTPException(status_code=429, detail="Queue at capacity, try again later")
    conn = get_pg_conn()
    alert_ids = []
    fingerprints = []

    try:
        for _ in range(count):
            alert_type = random.choice(ALERT_TYPES)
            severity = random.choice(SEVERITIES)
            window = int(time.time() // 60)

            alert = build_alert(
                alert_type, severity, source, window,
                force_fingerprint=force_fingerprint,
            )

            if payload_override:
                alert.update(payload_override)

            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) VALUES (%s, %s, %s, %s)",
                        (alert["alert_id"], "PRODUCED", SERVICE, json.dumps({}))
                    )
                    cur.execute(
                        "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) VALUES (%s, %s, %s, %s)",
                        (alert["alert_id"], "QUEUED", SERVICE, json.dumps({}))
                    )
            r.lpush(QUEUE_KEY, json.dumps(alert))
            alert_ids.append(alert["alert_id"])
            fingerprints.append(alert["fingerprint"])
    finally:
        conn.close()

    log(SERVICE, f"Generated {count} alert(s) via API")
    return {"generated": count, "alert_ids": alert_ids, "fingerprints": fingerprints}
