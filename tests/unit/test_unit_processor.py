"""
Unit tests for services/event_processor/main.py

Covers: is_duplicate(), process_alert() paths, log_ledger().
No running services required — all external calls are mocked.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

import event_processor.main as processor

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# is_duplicate()
# ---------------------------------------------------------------------------

class TestIsDuplicate:
    def test_returns_true_when_fingerprint_found(self, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{}]}}
        assert processor.is_duplicate(mock_es, "abc123") is True

    def test_returns_false_when_no_hits(self, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_returns_false_on_index_not_found(self, mock_es):
        from elasticsearch.exceptions import NotFoundError
        mock_es.search.side_effect = NotFoundError(
            message="index_not_found", meta=MagicMock(), body={}
        )
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_unexpected_exception_is_suppressed_returns_false(self, mock_es):
        mock_es.search.side_effect = ConnectionError("network blip")
        assert processor.is_duplicate(mock_es, "abc123") is False

    def test_passes_correct_fingerprint_to_es_query(self, mock_es):
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        processor.is_duplicate(mock_es, "deadbeef12345678")
        _, kwargs = mock_es.search.call_args
        assert kwargs["query"]["term"]["fingerprint"] == "deadbeef12345678"


# ---------------------------------------------------------------------------
# process_alert() — processing paths
# ---------------------------------------------------------------------------

class TestProcessAlert:
    """
    process_alert() flow:
      1. Write PROCESSING to ledger
      2. Check for duplicate fingerprint in ES
      3. If duplicate → DUPLICATE_DROPPED
      4. If unique → es.index → STORED
      5. If es.index throws → FAILED with error metadata
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

    def test_unique_alert_is_stored_in_es(self, mock_es, sample_alert):
        """No duplicate: es.index called and STORED written to ledger."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        processor.process_alert(sample_alert, mock_es, conn)

        mock_es.index.assert_called_once()
        _, kwargs = mock_es.index.call_args
        assert kwargs["id"] == sample_alert["alert_id"]

        assert cursor.execute.call_count == 2
        stored_call_args = cursor.execute.call_args_list[1][0]
        assert "STORED" in stored_call_args[1]

    def test_duplicate_alert_is_dropped(self, mock_es, sample_alert):
        """Fingerprint already in ES: DUPLICATE_DROPPED written, es.index NOT called."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{}]}}

        processor.process_alert(sample_alert, mock_es, conn)

        mock_es.index.assert_not_called()
        assert cursor.execute.call_count == 2
        dup_call_args = cursor.execute.call_args_list[1][0]
        assert "DUPLICATE_DROPPED" in dup_call_args[1]
        metadata = json.loads(dup_call_args[1][3])
        assert metadata["fingerprint"] == sample_alert["fingerprint"]

    def test_es_index_failure_writes_failed_with_error(self, mock_es, sample_alert):
        """es.index raises → FAILED written with error in metadata."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es.index.side_effect = Exception("mapper_parsing_exception")

        processor.process_alert(sample_alert, mock_es, conn)

        assert cursor.execute.call_count == 2
        failed_call_args = cursor.execute.call_args_list[1][0]
        assert "FAILED" in failed_call_args[1]
        metadata = json.loads(failed_call_args[1][3])
        assert "mapper_parsing_exception" in metadata["error"]

    def test_processing_state_always_written_first(self, mock_es, sample_alert):
        """First ledger write is always PROCESSING."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        processor.process_alert(sample_alert, mock_es, conn)

        first_call_args = cursor.execute.call_args_list[0][0]
        assert "PROCESSING" in first_call_args[1]

    def test_alert_id_extracted_from_alert_dict(self, mock_es, sample_alert):
        """The alert_id used in ledger comes from the alert dict."""
        conn, cursor = self._make_conn_and_cursor()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        processor.process_alert(sample_alert, mock_es, conn)

        first_call_args = cursor.execute.call_args_list[0][0]
        assert first_call_args[1][0] == sample_alert["alert_id"]


# ---------------------------------------------------------------------------
# log_ledger()
# ---------------------------------------------------------------------------

class TestLogLedger:
    def test_writes_correct_state_and_service(self, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "STORED")
        cursor.execute.assert_called_once()
        args = cursor.execute.call_args[0][1]
        assert args[0] == "alert-xyz"
        assert args[1] == "STORED"
        assert args[2] == "event_processor"

    def test_metadata_is_serialized_as_json_string(self, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "FAILED", {"error": "boom"})
        args = cursor.execute.call_args[0][1]
        assert isinstance(args[3], str)
        assert json.loads(args[3]) == {"error": "boom"}

    def test_none_metadata_defaults_to_empty_dict(self, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "PROCESSING", None)
        args = cursor.execute.call_args[0][1]
        assert json.loads(args[3]) == {}

    def test_metadata_omitted_defaults_to_empty_dict(self, mock_pg_conn):
        conn, cursor = mock_pg_conn
        processor.log_ledger(conn, "alert-xyz", "PROCESSING")
        args = cursor.execute.call_args[0][1]
        assert json.loads(args[3]) == {}
