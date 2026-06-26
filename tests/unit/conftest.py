"""
Shared fixtures for unit tests.
All tests here run without any running services — everything is mocked.
"""
import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_es():
    """Elasticsearch client mock with a configurable hit count."""
    es = MagicMock()
    # Default: no hits
    es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    return es


@pytest.fixture
def mock_pg_conn():
    """Psycopg2 connection mock whose cursor returns configurable rows."""
    conn = MagicMock()
    cursor = MagicMock()
    # Support `with conn:` and `with conn.cursor() as cur:`
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn, cursor


@pytest.fixture
def sample_alert():
    return {
        "alert_id": "test-alert-001",
        "title": "[TEST] Brute Force Login Attempt",
        "description": "Multiple failed SSH logins from 192.168.1.10",
        "severity": "high",
        "source_ip": "192.168.1.10",
        "dest_ip": "10.0.1.20",
        "alert_type": "brute_force",
        "timestamp": "2026-06-26T00:00:00+00:00",
        "fingerprint": "abc123def456abcd",
        "source": "test",
        "metadata": {"source_service": "api"},
    }
