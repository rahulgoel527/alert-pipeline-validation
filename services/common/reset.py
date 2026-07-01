import os

from common.infra import get_pg_conn, get_es, get_redis, QUEUE_KEY, log
from common.es_mappings import ES_MAPPINGS


def reset_pipeline(service: str):
    es_index = os.environ["ES_INDEX"]

    conn = get_pg_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE alert_ledger RESTART IDENTITY")
        log(service, "Postgres ledger truncated")
    finally:
        conn.close()

    get_redis().delete(QUEUE_KEY)
    log(service, f"Redis queue '{QUEUE_KEY}' flushed")

    es = get_es()
    if es.indices.exists(index=es_index):
        es.indices.delete(index=es_index)
    es.indices.create(index=es_index, mappings=ES_MAPPINGS)
    es.cluster.health(index=es_index, wait_for_status='yellow', timeout='10s')
    log(service, f"ES index '{es_index}' reset and ready")
