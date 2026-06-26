"""
Unit tests for services/event_processor/main.py

Covers: is_duplicate(), process_alert() failure injection paths, log_ledger().
No running services required — all external calls are mocked.
"""
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, call

pytestmark = pytest.mark.unit

# Patch env vars before the module is imported
_ENV_PATCH = {
    "REDIS_HOST": "localhost",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_DB": "alerts",
    "POSTGRES_USER": "alerts",
    "POSTGRES_PASSWORD": "secret",
    "ELASTICSEARCH_HOST": "localhost",
    "ES_INDEX": "security_alerts",
}


@pytest.fixture(autouse=True, scope="module")
def patch_env():
    with patch.dict(os.environ, _ENV_PATCH):
        yield


@pytest.fixture(scope="module")
def processor():
    """Import the processor module once, after env vars are patched."""
    # Remove cached module so we get a fresh import with patched env
    sys.modules.pop("services.event_processor.main", None)
    sys.modules.pop("event_processor_main", None)

    # Add the services directory to sys.path so we can import by directory name
    services_path = os.path.join(os.path.dirname(__file__), "..", "..", "services", "event_processor")
    if services_path not in sys.path:
        sys.path.insert(0, services_path)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "event_processor_main",
        os.path.join(services_path, "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# is_duplicate()
# ---------------------------------------------------------------------------

class TestIsDuplicate:
    def test_returns_true_when_fingerprint_found(self, processor, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{}]}}
        assert processor.is_duplicate(mock_es, "abc123") is True

    def test_returns_false_when_no_hits(self, processor, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_returns_false_on_index_not_found(self, processor, mock_es):
        from elasticsearch.exceptions import NotFoundError
        mock_es.search.side_effect = NotFoundError(
            message="index_not_found", meta=MagicMock(), body={}
        )
        # NotFoundError is caught; should not propagate and should return False
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_unexpected_exception_is_suppressed_returns_false(self, processor, mock_es):
        # The current implementation catches all exceptions in is_duplicate.
        # This test documents that behaviour and guards against accidentally
        # re-raising, which would cause the processor to crash on transient ES errors.
        mock_es.search.side_effect = ConnectionError("network blip")
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_passes_correct_fingerprint_to_es_query(self, processor, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        processor.is_duplicate(mock_es, "deadbeef12345678")
        _, kwargs = mock_es.search.call_args
        assert kwargs["query"]["term"]["fingerprint"] == "deadbeef12345678"


# ---------------------------------------------------------------------------
# process_alert() — failure injection paths
# ---------------------------------------------------------------------------

class TestProcessAlertFailureInjection:
    """
    process_alert() calls random.random() three times in sequence:
      1. < 0.02  → stuck (early return, no terminal state written yet)
      2. < 0.05  → FAILED written
      3. < 0.10  → slow (time.sleep called)
    We control random.random() via patch to test each branch deterministically.
    """

    def _make_conn_and_cursor(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_stuck_path_returns_without_terminal_state(self, processor, mock_es, sample_alert):
        """random < 0.02: alert stays in PROCESSING — no FAILED/STORED written."""
        conn, cursor = self._make_conn_and_cursor()

        with patch.object(processor, "get_pg_conn", return_value=conn), \
             patch("random.random", return_value=0.01):
            processor.process_alert(mock_es, sample_alert)

        # Only the PROCESSING ledger entry should be inserted (the first execute call)
        assert cursor.execute.call_count == 1
        first_call_args = cursor.execute.call_args_list[0][0]
        assert "PROCESSING" in first_call_args[1]
        # ES index must NOT be called
        mock_es.index.assert_not_called()

    def test_failure_path_writes_failed_with_error_metadata(self, processor, mock_es, sample_alert):
        """random in [0.02, 0.07): FAILED written with error key in metadata."""
        conn, cursor = self._make_conn_and_cursor()

        # First call (stuck check) returns 0.03 — above 0.02, so not stuck.
        # Second call (failure check) returns 0.03 — below 0.05, so FAILED.
        with patch.object(processor, "get_pg_conn", return_value=conn), \
             patch("random.random", side_effect=[0.03, 0.03]):
            processor.process_alert(mock_es, sample_alert)

        # Two inserts: PROCESSING + FAILED
        assert cursor.execute.call_count == 2
        failed_call_args = cursor.execute.call_args_list[1][0]
        assert "FAILED" in failed_call_args[1]
        metadata = json.loads(failed_call_args[1][3])
        assert "error" in metadata
        mock_es.index.assert_not_called()

    def test_slowness_path_calls_sleep(self, processor, mock_es, sample_alert):
        """random in [0.07, 0.17): time.sleep is called once."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        # 1st call: 0.05 (skip stuck), 2nd call: 0.10 (skip failure),
        # 3rd call: 0.08 (trigger slowness), 4th call: uniform for delay value
        with patch.object(processor, "get_pg_conn", return_value=conn), \
             patch("random.random", side_effect=[0.05, 0.10, 0.08]), \
             patch("random.uniform", return_value=3.5) as mock_sleep_val, \
             patch("time.sleep") as mock_sleep:
            processor.process_alert(mock_es, sample_alert)

        mock_sleep.assert_called_once_with(3.5)

    def test_happy_path_stores_alert_in_es(self, processor, mock_es, sample_alert):
        """random >= 0.17 on all checks, no duplicate: es.index called and STORED written."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        with patch.object(processor, "get_pg_conn", return_value=conn), \
             patch("random.random", return_value=0.99):
            processor.process_alert(mock_es, sample_alert)

        mock_es.index.assert_called_once()
        _, kwargs = mock_es.index.call_args
        assert kwargs["id"] == sample_alert["alert_id"]

        # Two inserts: PROCESSING + STORED
        assert cursor.execute.call_count == 2
        stored_call_args = cursor.execute.call_args_list[1][0]
        assert "STORED" in stored_call_args[1]

    def test_duplicate_path_drops_without_es_index(self, processor, mock_es, sample_alert):
        """Fingerprint already in ES: DUPLICATE_DROPPED written, es.index NOT called."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{}]}}

        with patch.object(processor, "get_pg_conn", return_value=conn), \
             patch("random.random", return_value=0.99):
            processor.process_alert(mock_es, sample_alert)

        mock_es.index.assert_not_called()
        assert cursor.execute.call_count == 2
        dup_call_args = cursor.execute.call_args_list[1][0]
        assert "DUPLICATE_DROPPED" in dup_call_args[1]
        metadata = json.loads(dup_call_args[1][3])
        assert metadata["fingerprint"] == sample_alert["fingerprint"]


# ---------------------------------------------------------------------------
# log_ledger()
# ---------------------------------------------------------------------------

class TestLogLedger:
    def test_writes_correct_state_and_service(self, processor, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "STORED")
        cursor.execute.assert_called_once()
        args = cursor.execute.call_args[0][1]
        assert args[0] == "alert-xyz"
        assert args[1] == "STORED"
        assert args[2] == "event_processor"

    def test_metadata_is_serialized_as_json_string(self, processor, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "FAILED", {"error": "boom"})
        args = cursor.execute.call_args[0][1]
        # The fourth parameter must be a JSON string, not a dict
        assert isinstance(args[3], str)
        assert json.loads(args[3]) == {"error": "boom"}

    def test_none_metadata_defaults_to_empty_dict(self, processor, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "PROCESSING", None)
        args = cursor.execute.call_args[0][1]
        assert json.loads(args[3]) == {}

    def test_metadata_omitted_defaults_to_empty_dict(self, processor, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "PROCESSING")
        args = cursor.execute.call_args[0][1]
        assert json.loads(args[3]) == {}
