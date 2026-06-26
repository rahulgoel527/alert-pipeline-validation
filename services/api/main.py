import json
import os
import time
import uuid
import hashlib
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg2
import redis as redis_lib
from elasticsearch import Elasticsearch, NotFoundError
from fastapi import Body, FastAPI, HTTPException, Query

SERVICE = "api"
POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
ELASTICSEARCH_HOST = os.environ["ELASTICSEARCH_HOST"]
ES_INDEX = os.environ["ES_INDEX"]
REDIS_HOST = os.environ["REDIS_HOST"]
QUEUE_KEY = "alert_queue"

ALERT_TYPES = ["brute_force", "malware", "phishing", "port_scan", "data_exfiltration"]
SEVERITIES = ["low", "medium", "high", "critical"]
TITLES = {
    "brute_force": "Brute Force Login Attempt",
    "malware": "Malware Detected",
    "phishing": "Phishing Email Detected",
    "port_scan": "Port Scan Detected",
    "data_exfiltration": "Data Exfiltration Attempt",
}
DESCRIPTIONS = {
    "brute_force": "Multiple failed SSH logins from {src}",
    "malware": "Malicious process detected on {dst}",
    "phishing": "Suspicious email link clicked from {src}",
    "port_scan": "SYN scan from {src} targeting {dst}",
    "data_exfiltration": "Large outbound transfer from {src} to {dst}",
}


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
            conn = get_pg_conn()
            conn.close()
            log("Postgres ready")
            return
        except psycopg2.OperationalError:
            log("Waiting for Postgres...")
            time.sleep(1)


def wait_for_elasticsearch():
    while True:
        try:
            es = get_es()
            health = es.cluster.health()
            if health["status"] in ("green", "yellow"):
                log("Elasticsearch ready")
                return es
        except Exception:
            pass
        log("Waiting for Elasticsearch...")
        time.sleep(2)


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
        log("Postgres schema ready — ledger cleared")
    finally:
        conn.close()


def init_es_index(es):
    try:
        if es.indices.exists(index=ES_INDEX):
            es.indices.delete(index=ES_INDEX)
            log(f"ES index '{ES_INDEX}' dropped")
        es.indices.create(index=ES_INDEX, mappings=ES_MAPPINGS)
        log(f"ES index '{ES_INDEX}' created fresh")
    except Exception as exc:
        log(f"FATAL: failed to initialise ES index '{ES_INDEX}': {exc}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_postgres()
    init_postgres_schema()

    es = wait_for_elasticsearch()
    init_es_index(es)

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
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    WITH latest AS (
                        SELECT DISTINCT ON (alert_id) alert_id, state, timestamp
                        FROM alert_ledger
                        ORDER BY alert_id, id DESC
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE state = 'QUEUED')          AS currently_queued,
                        COUNT(*) FILTER (WHERE state = 'PROCESSING')      AS currently_processing,
                        COUNT(*) FILTER (WHERE state = 'STORED')          AS total_stored,
                        COUNT(*) FILTER (WHERE state = 'FAILED')          AS total_failed,
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
    source = None
    if body:
        if "count" in body:
            count = max(1, min(int(body["count"]), 100))
        if "force_fingerprint" in body:
            force_fingerprint = str(body["force_fingerprint"])
        if "source" in body:
            source = str(body["source"])

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
            src = f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}"
            dst = f"10.0.{random.randint(1, 254)}.{random.randint(1, 254)}"

            if force_fingerprint:
                fingerprint = force_fingerprint
            else:
                window = int(time.time() // 60)
                fingerprint = hashlib.sha256(f"{src}{alert_type}{window}".encode()).hexdigest()[:16]

            alert_id = str(uuid.uuid4())
            title = TITLES[alert_type]
            if source:
                title = f"[{source.upper()}] {title}"

            alert = {
                "alert_id": alert_id,
                "title": title,
                "description": DESCRIPTIONS[alert_type].format(src=src, dst=dst),
                "severity": severity,
                "source_ip": src,
                "dest_ip": dst,
                "alert_type": alert_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "fingerprint": fingerprint,
                "source": source or "unknown",
                "metadata": {"source_service": SERVICE},
            }

            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) VALUES (%s, %s, %s, %s)",
                        (alert_id, "PRODUCED", SERVICE, json.dumps({}))
                    )
                    cur.execute(
                        "INSERT INTO alert_ledger (alert_id, state, source_service, metadata) VALUES (%s, %s, %s, %s)",
                        (alert_id, "QUEUED", SERVICE, json.dumps({}))
                    )
            r.lpush(QUEUE_KEY, json.dumps(alert))
            alert_ids.append(alert_id)
            fingerprints.append(fingerprint)
    finally:
        conn.close()

    log(f"Generated {count} alert(s) via API")
    return {"generated": count, "alert_ids": alert_ids, "fingerprints": fingerprints}
