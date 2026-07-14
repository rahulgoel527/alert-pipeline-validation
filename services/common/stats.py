"""Pipeline statistics — one deep module behind a small interface."""


def get_pipeline_stats(pg_conn, source=None):
    """Return pipeline stats dict from the ledger. Caller owns the connection lifecycle.

    source: if given, scope all counts to alerts whose PRODUCED row has metadata->>'source' = source.
    """
    with pg_conn:
        with pg_conn.cursor() as cur:
            cur.execute("""
                WITH produced_ids AS (
                    SELECT DISTINCT alert_id
                    FROM alert_ledger
                    WHERE state = 'PRODUCED'
                      AND (%(source)s IS NULL OR metadata->>'source' = %(source)s)
                ),
                latest AS (
                    SELECT DISTINCT ON (l.alert_id) l.alert_id, l.state, l.timestamp
                    FROM alert_ledger l
                    JOIN produced_ids p ON l.alert_id = p.alert_id
                    ORDER BY l.alert_id, l.id DESC
                )
                SELECT
                    COUNT(*) FILTER (WHERE l.state = 'QUEUED')            AS currently_queued,
                    COUNT(*) FILTER (WHERE l.state = 'PROCESSING')        AS currently_processing,
                    COUNT(*) FILTER (WHERE l.state = 'STORED')            AS total_stored,
                    COUNT(*) FILTER (WHERE l.state = 'FAILED')            AS total_failed,
                    COUNT(*) FILTER (WHERE l.state = 'DUPLICATE_DROPPED') AS total_duplicates,
                    (SELECT COUNT(*) FROM produced_ids)                   AS total_produced,
                    (SELECT MAX(al.timestamp) FROM alert_ledger al
                     JOIN produced_ids pi ON al.alert_id = pi.alert_id)   AS last_event_at,
                    (SELECT AVG(EXTRACT(EPOCH FROM (s.timestamp - p.timestamp)) * 1000)
                     FROM alert_ledger p
                     JOIN alert_ledger s ON p.alert_id = s.alert_id
                     JOIN produced_ids pi ON p.alert_id = pi.alert_id
                     WHERE p.state = 'PROCESSING' AND s.state = 'STORED') AS avg_latency_ms
                FROM latest l
            """, {"source": source})
            row = cur.fetchone()
            currently_queued      = int(row[0])
            currently_processing  = int(row[1])
            total_stored          = int(row[2])
            total_failed          = int(row[3])
            total_duplicates      = int(row[4])
            total_produced        = int(row[5])
            last_ts               = row[6]
            avg_ms                = row[7]

    last_event_at = last_ts.isoformat() + "Z" if last_ts else None
    accounted = total_stored + total_failed + total_duplicates + currently_queued + currently_processing
    unaccounted = max(0, total_produced - accounted)

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
    }
