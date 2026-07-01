"""Pipeline statistics — one deep module behind a small interface."""


def get_pipeline_stats(pg_conn):
    """Return pipeline stats dict from the ledger. Caller owns the connection lifecycle."""
    with pg_conn:
        with pg_conn.cursor() as cur:
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
