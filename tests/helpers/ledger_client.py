from datetime import datetime

import psycopg2
import psycopg2.extras

TERMINAL_STATES = {"STORED", "FAILED", "DUPLICATE_DROPPED"}


class LedgerClient:
    def __init__(
        self,
        host="localhost",
        port=5432,
        dbname="alert_ledger",
        user="ledger",
        password="ledger123",
    ):
        self._dsn = dict(host=host, port=port, dbname=dbname, user=user, password=password)

    def _conn(self):
        return psycopg2.connect(**self._dsn)

    def get_alert_states(self, alert_id: str) -> list[dict]:
        """Return all ledger rows for alert_id ordered by id."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, alert_id, state, timestamp, source_service, metadata "
                    "FROM alert_ledger WHERE alert_id = %s ORDER BY id",
                    (alert_id,),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_current_state(self, alert_id: str) -> str:
        """Return the most recent state for alert_id, or None if not found."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM alert_ledger WHERE alert_id = %s ORDER BY id DESC LIMIT 1",
                    (alert_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def get_alerts_in_state(self, state: str) -> list[str]:
        """Return alert_ids whose most recent state equals state."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (alert_id) alert_id
                    FROM alert_ledger
                    ORDER BY alert_id, id DESC
                    """,
                )
                # Filter in Python — simpler than a subquery when using DISTINCT ON
                all_rows = cur.fetchall()

            # Re-query efficiently
            cur_conn = self._conn()
            try:
                with cur_conn.cursor() as cur2:
                    cur2.execute(
                        """
                        SELECT alert_id
                        FROM (
                            SELECT DISTINCT ON (alert_id) alert_id, state
                            FROM alert_ledger
                            ORDER BY alert_id, id DESC
                        ) latest
                        WHERE state = %s
                        """,
                        (state,),
                    )
                    return [r[0] for r in cur2.fetchall()]
            finally:
                cur_conn.close()

    def get_stuck_alerts(self, threshold_seconds=60) -> list[str]:
        """Return alert_ids stuck in PROCESSING longer than threshold_seconds."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (alert_id) alert_id, state, timestamp
                    FROM alert_ledger
                    ORDER BY alert_id, id DESC
                    """,
                )
                rows = cur.fetchall()

        now = datetime.utcnow()
        stuck = []
        for alert_id, state, ts in rows:
            if state == "PROCESSING":
                age = (now - ts).total_seconds()
                if age > threshold_seconds:
                    stuck.append(alert_id)
        return stuck

    def get_state_counts(self) -> dict:
        """Return counts of all alerts by their most recent state."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state, COUNT(*) FROM (
                        SELECT DISTINCT ON (alert_id) state
                        FROM alert_ledger
                        ORDER BY alert_id, id DESC
                    ) latest
                    GROUP BY state
                    """
                )
                return {row[0]: int(row[1]) for row in cur.fetchall()}

    def get_processing_latency(self, alert_id: str) -> float:
        """Return milliseconds between PROCESSING and STORED for alert_id, or None."""
        states = self.get_alert_states(alert_id)
        proc_ts = None
        stored_ts = None
        for row in states:
            if row["state"] == "PROCESSING":
                proc_ts = row["timestamp"]
            elif row["state"] == "STORED":
                stored_ts = row["timestamp"]
        if proc_ts and stored_ts:
            return (stored_ts - proc_ts).total_seconds() * 1000
        return None

    def get_alerts_produced_after(self, timestamp) -> list[str]:
        """Return alert_ids with a PRODUCED entry after timestamp."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT alert_id FROM alert_ledger "
                    "WHERE state = 'PRODUCED' AND timestamp > %s",
                    (timestamp,),
                )
                return [r[0] for r in cur.fetchall()]
