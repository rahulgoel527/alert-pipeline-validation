"""Infrastructure utilities — connection factories, wait loops, logging."""
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

QUEUE_KEY = "alert_queue"


def _env(key, default=None):
    if default is not None:
        return os.environ.get(key, default)
    return os.environ[key]


def log(service, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{service}] {msg}", flush=True)


def get_pg_conn():
    import psycopg2
    return psycopg2.connect(
        host=_env("POSTGRES_HOST"),
        dbname=_env("POSTGRES_DB"),
        user=_env("POSTGRES_USER"),
        password=_env("POSTGRES_PASSWORD"),
    )


def get_es():
    from elasticsearch import Elasticsearch
    return Elasticsearch(f"http://{_env('ELASTICSEARCH_HOST')}:9200")


def get_redis():
    import redis as redis_lib
    return redis_lib.Redis(host=_env("REDIS_HOST", "redis"), decode_responses=True)


def wait_for_postgres(service):
    import psycopg2
    while True:
        try:
            conn = get_pg_conn()
            conn.close()
            log(service, "Postgres ready")
            return
        except psycopg2.OperationalError:
            log(service, "Waiting for Postgres...")
            time.sleep(1)


def wait_for_elasticsearch(service):
    while True:
        try:
            es = get_es()
            health = es.cluster.health()
            if health["status"] in ("green", "yellow"):
                log(service, "Elasticsearch ready")
                return es
        except Exception:
            pass
        log(service, "Waiting for Elasticsearch...")
        time.sleep(2)


def wait_for_redis(service):
    r = get_redis()
    while True:
        try:
            r.ping()
            log(service, "Redis ready")
            return r
        except Exception:
            log(service, "Waiting for Redis...")
            time.sleep(1)
