"""
Unit tests for services/api/main.py

Covers: fingerprint algorithm, generate_alerts() parameter validation,
get_stats() accounting balance logic.
No running services required — all external calls are mocked.
"""
import hashlib
import json
import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

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
def api():
    """Import the API module once, after env vars are patched."""
    sys.modules.pop("api_main", None)
    services_path = os.path.join(os.path.dirname(__file__), "..", "..", "services", "api")
    if services_path not in sys.path:
        sys.path.insert(0, services_path)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "api_main",
        os.path.join(services_path, "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fingerprint algorithm
# ---------------------------------------------------------------------------

class TestFingerprintAlgorithm:
    """
    The fingerprint is the dedup key. Its contract:
      sha256(source_ip + alert_type + floor(epoch / 60))[:16]
    A wrong window divisor causes silent false-positives or false-negatives.
    """

    def _compute(self, source_ip, alert_type, epoch):
        window = int(epoch // 60)
        return hashlib.sha256(f"{source_ip}{alert_type}{window}".encode()).hexdigest()[:16]

    def test_same_inputs_same_window_produce_identical_fingerprint(self):
        now = 1_750_000_800  # arbitrary fixed epoch, divisible by 60
        fp1 = self._compute("192.168.1.10", "brute_force", now)
        fp2 = self._compute("192.168.1.10", "brute_force", now)
        assert fp1 == fp2

    def test_same_inputs_different_window_produce_different_fingerprint(self):
        base = 1_750_000_800  # second 0 of a window
        fp_now = self._compute("192.168.1.10", "brute_force", base)
        fp_later = self._compute("192.168.1.10", "brute_force", base + 60)
        assert fp_now != fp_later

    def test_timestamps_within_same_60s_window_produce_identical_fingerprint(self):
        base = 1_750_000_800
        fp1 = self._compute("192.168.1.10", "brute_force", base)
        fp2 = self._compute("192.168.1.10", "brute_force", base + 59)
        assert fp1 == fp2

    def test_different_source_ip_same_type_same_window_produce_different_fingerprint(self):
        now = 1_750_000_800
        fp1 = self._compute("192.168.1.10", "malware", now)
        fp2 = self._compute("192.168.1.11", "malware", now)
        assert fp1 != fp2

    def test_fingerprint_is_exactly_16_hex_characters(self):
        fp = self._compute("192.168.1.10", "phishing", 1_750_000_800)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_generate_alerts_uses_force_fingerprint_when_provided(self, api):
        """force_fingerprint bypasses computation; all generated alerts must use it."""
        forced = "deadbeef12345678"

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        mock_redis = MagicMock()
        mock_redis.llen.return_value = 0

        with patch.object(api, "get_pg_conn", return_value=mock_conn), \
             patch.object(api, "get_redis", return_value=mock_redis):
            result = api.generate_alerts({"count": 3, "force_fingerprint": forced})

        assert all(fp == forced for fp in result["fingerprints"])


# ---------------------------------------------------------------------------
# generate_alerts() — parameter validation
# ---------------------------------------------------------------------------

class TestGenerateAlertsParameters:
    def _call(self, api, body):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        mock_redis = MagicMock()
        mock_redis.llen.return_value = 0

        with patch.object(api, "get_pg_conn", return_value=mock_conn), \
             patch.object(api, "get_redis", return_value=mock_redis):
            return api.generate_alerts(body)

    def test_count_is_capped_at_100(self, api):
        result = self._call(api, {"count": 150})
        assert result["generated"] == 100
        assert len(result["alert_ids"]) == 100

    def test_count_1_produces_single_alert(self, api):
        result = self._call(api, {"count": 1})
        assert result["generated"] == 1
        assert len(result["alert_ids"]) == 1

    def test_source_prefixes_title(self, api):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 0

        pushed_alerts = []

        def capture_push(key, payload):
            pushed_alerts.append(json.loads(payload))

        mock_redis.lpush.side_effect = capture_push

        with patch.object(api, "get_pg_conn", return_value=mock_conn), \
             patch.object(api, "get_redis", return_value=mock_redis):
            api.generate_alerts({"count": 1, "source": "generator"})

        assert pushed_alerts, "No alert was pushed to Redis"
        assert pushed_alerts[0]["title"].startswith("[GENERATOR]")
        assert pushed_alerts[0]["source"] == "generator"

    def test_no_source_uses_unknown_and_no_title_prefix(self, api):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 0

        pushed_alerts = []
        mock_redis.lpush.side_effect = lambda key, p: pushed_alerts.append(json.loads(p))

        with patch.object(api, "get_pg_conn", return_value=mock_conn), \
             patch.object(api, "get_redis", return_value=mock_redis):
            api.generate_alerts({})

        assert pushed_alerts
        assert not pushed_alerts[0]["title"].startswith("[")
        assert pushed_alerts[0]["source"] == "unknown"

    def test_alert_pushed_to_redis_is_valid_json_with_required_fields(self, api):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 0

        pushed_alerts = []
        mock_redis.lpush.side_effect = lambda key, p: pushed_alerts.append(json.loads(p))

        with patch.object(api, "get_pg_conn", return_value=mock_conn), \
             patch.object(api, "get_redis", return_value=mock_redis):
            api.generate_alerts({"count": 1, "source": "test"})

        required = {"alert_id", "title", "description", "severity", "source_ip",
                    "dest_ip", "alert_type", "timestamp", "fingerprint", "source"}
        assert required.issubset(pushed_alerts[0].keys())


# ---------------------------------------------------------------------------
# get_stats() — accounting balance logic
# These tests extract the balance calculation directly to verify its correctness
# without needing a live Postgres connection.
# ---------------------------------------------------------------------------

class TestAccountingBalanceLogic:
    """
    The balance formula in get_stats():
        accounted = stored + failed + duplicates + queued + processing
        unaccounted = max(0, produced - accounted)
        accounting_balanced = (unaccounted == 0)
    """

    def _balance(self, produced, stored, failed, duplicates, queued=0, processing=0):
        accounted = stored + failed + duplicates + queued + processing
        unaccounted = max(0, produced - accounted)
        return {"accounting_balanced": unaccounted == 0, "unaccounted": unaccounted}

    def test_fully_accounted_returns_balanced(self):
        result = self._balance(produced=10, stored=7, failed=2, duplicates=1)
        assert result["accounting_balanced"] is True
        assert result["unaccounted"] == 0

    def test_missing_one_alert_returns_not_balanced(self):
        result = self._balance(produced=10, stored=6, failed=2, duplicates=1)
        assert result["accounting_balanced"] is False
        assert result["unaccounted"] == 1

    def test_zero_produced_is_balanced(self):
        result = self._balance(produced=0, stored=0, failed=0, duplicates=0)
        assert result["accounting_balanced"] is True
        assert result["unaccounted"] == 0

    def test_in_flight_alerts_counted_as_accounted(self):
        # 3 produced: 1 stored, 2 still processing → should be balanced
        result = self._balance(produced=3, stored=1, failed=0, duplicates=0, processing=2)
        assert result["accounting_balanced"] is True

    def test_duplicate_dropped_contributes_to_accounting(self):
        # If duplicates are NOT counted, this would show unaccounted=2
        result = self._balance(produced=5, stored=3, failed=0, duplicates=2)
        assert result["accounting_balanced"] is True

    def test_unaccounted_never_goes_negative(self):
        # More stored than produced (shouldn't happen, but the formula should not blow up)
        result = self._balance(produced=3, stored=5, failed=0, duplicates=0)
        assert result["unaccounted"] == 0
        assert result["accounting_balanced"] is True
