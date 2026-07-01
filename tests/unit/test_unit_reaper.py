"""
Unit tests for the stuck-alert reaper in services/event_processor/main.py

Covers: reaper_loop() writes FAILED entries for stuck alerts, does nothing when none.
No running services required — Postgres and time.sleep are mocked.
"""
import json
import pytest
from unittest.mock import MagicMock, call, patch

import event_processor.main as processor

pytestmark = pytest.mark.unit


def _make_reaper_conn(stuck_rows):
    """Build a mock Postgres connection whose cursor returns stuck_rows from fetchall."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = stuck_rows
    conn.cursor.return_value = cursor
    return conn, cursor


def test_reaper_writes_failed_entry_for_stuck_alert():
    """reaper_loop() marks each stuck PROCESSING alert as FAILED with correct metadata."""
    conn, cursor = _make_reaper_conn([("alert-X", 90.5)])

    sleep_calls = [0]
    def sleep_once(seconds):
        sleep_calls[0] += 1
        if sleep_calls[0] > 1:
            raise KeyboardInterrupt

    with patch.object(processor, "get_pg_conn", return_value=conn), \
         patch("event_processor.main.time.sleep", side_effect=sleep_once):
        try:
            processor.reaper_loop()
        except KeyboardInterrupt:
            pass

    # cursor.execute is called twice: once for SELECT, once for the INSERT inside log_ledger
    # Find the INSERT call
    insert_calls = [
        c for c in cursor.execute.call_args_list
        if c.args and "INSERT" in str(c.args[0])
    ]
    assert len(insert_calls) == 1, f"Expected 1 INSERT, got {len(insert_calls)}"
    args = insert_calls[0].args[1]
    assert args[0] == "alert-X"
    assert args[1] == "FAILED"
    assert args[2] == "event_processor"
    metadata = json.loads(args[3])
    assert metadata["reason"] == "stuck_timeout"
    assert isinstance(metadata["stuck_duration_minutes"], (int, float))


def test_reaper_does_nothing_when_no_stuck_alerts():
    """reaper_loop() skips INSERT when fetchall returns no stuck alerts."""
    conn, cursor = _make_reaper_conn([])  # empty = no stuck alerts

    sleep_calls = [0]
    def sleep_once(seconds):
        sleep_calls[0] += 1
        if sleep_calls[0] > 1:
            raise KeyboardInterrupt

    with patch.object(processor, "get_pg_conn", return_value=conn), \
         patch("event_processor.main.time.sleep", side_effect=sleep_once):
        try:
            processor.reaper_loop()
        except KeyboardInterrupt:
            pass

    insert_calls = [
        c for c in cursor.execute.call_args_list
        if c.args and "INSERT" in str(c.args[0])
    ]
    assert len(insert_calls) == 0, "No INSERT should occur when no stuck alerts found"


def test_reaper_sql_has_required_clauses():
    """reaper_loop() SQL must filter PROCESSING state, use DISTINCT ON, and parameterise timeout."""
    import inspect
    source = inspect.getsource(processor.reaper_loop)
    assert "state = 'PROCESSING'" in source, "SQL must filter on PROCESSING state"
    assert "DISTINCT ON" in source, "SQL must use DISTINCT ON to get latest state per alert"
    assert "make_interval" in source, "SQL must use make_interval for parameterised timeout"
