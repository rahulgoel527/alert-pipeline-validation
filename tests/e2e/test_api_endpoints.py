"""E2E tests for API endpoint behavior (health, list, search, single, ledger, generate, stats)."""
import requests
import pytest

from helpers.wait_utils import wait_for_alert_terminal, poll_until

pytestmark = pytest.mark.e2e


# ─── Health endpoint ──────────────────────────────────────────────────────────


def test_health_endpoint_reports_all_services(api_client):
    """Verify /api/health returns status ok with all services healthy."""
    health = api_client.health()
    assert health["status"] == "ok"
    assert "services" in health
    for svc in ("postgres", "elasticsearch", "redis"):
        assert svc in health["services"], f"Missing service: {svc}"
        assert health["services"][svc] is True, f"Service {svc} is not healthy"


# ─── List alerts ──────────────────────────────────────────────────────────────


def test_list_alerts_respects_size_param(api_client, ledger_client):
    """Generate alerts and verify size=2 returns at most 2 results."""
    result = api_client.generate_alerts(count=5)
    for aid in result["alert_ids"]:
        wait_for_alert_terminal(ledger_client, aid, timeout=30)

    alerts = api_client.get_alerts(limit=2)
    assert len(alerts) <= 2


def test_list_alerts_invalid_size_rejected(api_client):
    """size=0 and size=1001 should return 422 validation error."""
    session = api_client.session
    base = api_client.base_url

    resp = session.get(f"{base}/api/alerts", params={"size": 0}, timeout=5)
    assert resp.status_code == 422, f"Expected 422 for size=0, got {resp.status_code}"

    resp = session.get(f"{base}/api/alerts", params={"size": 1001}, timeout=5)
    assert resp.status_code == 422, f"Expected 422 for size=1001, got {resp.status_code}"


# ─── Search alerts ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def stored_alerts_batch(api_client, ledger_client):
    """Generate 20 alerts, wait for terminal state, and confirm ES is searchable."""
    result = api_client.generate_alerts(count=20)
    alert_ids = result["alert_ids"]
    for aid in alert_ids:
        wait_for_alert_terminal(ledger_client, aid, timeout=60)
    # Wait for ES refresh so search tests don't hit a stale index
    poll_until(
        lambda: len(api_client.get_alerts(limit=1)) > 0,
        timeout=10, interval=0.5,
        description="ES searchable after batch store",
    )
    return alert_ids


def test_search_alerts_by_severity(api_client, stored_alerts_batch):
    """Filter by severity — all returned alerts must match the filter."""
    # Get one alert to discover a severity that exists
    alerts = api_client.get_alerts(limit=10)
    assert len(alerts) > 0, "No alerts in ES to search"
    target_severity = alerts[0]["severity"]

    results = api_client.search_alerts(severity=target_severity)
    assert len(results) > 0, f"No results for severity={target_severity}"
    for alert in results:
        assert alert["severity"] == target_severity


def test_search_alerts_by_alert_type(api_client, stored_alerts_batch):
    """Filter by alert_type — all returned alerts must match."""
    alerts = api_client.get_alerts(limit=10)
    assert len(alerts) > 0
    target_type = alerts[0]["alert_type"]

    results = api_client.search_alerts(alert_type=target_type)
    assert len(results) > 0, f"No results for alert_type={target_type}"
    for alert in results:
        assert alert["alert_type"] == target_type


def test_search_alerts_by_source(api_client, stored_alerts_batch):
    """Filter by source field — all returned alerts must match."""
    results = api_client.search_alerts(source="[E2ETests]")
    assert len(results) > 0, "No results for source=[E2ETests]"
    for alert in results:
        assert alert["source"] == "[E2ETests]"


def test_search_alerts_combined_filters(api_client, stored_alerts_batch):
    """Multiple filters ANDed together — all returned alerts match all criteria."""
    alerts = api_client.get_alerts(limit=50)
    assert len(alerts) > 0
    # Pick a combination from an existing alert
    target = alerts[0]
    target_severity = target["severity"]
    target_type = target["alert_type"]

    results = api_client.search_alerts(severity=target_severity, alert_type=target_type)
    for alert in results:
        assert alert["severity"] == target_severity
        assert alert["alert_type"] == target_type


def test_search_alerts_no_results(api_client, stored_alerts_batch):
    """An impossible filter returns empty list."""
    results = api_client.search_alerts(severity="nonexistent_severity_xyz")
    assert results == []


def test_search_alerts_respects_size_param(api_client, stored_alerts_batch):
    """size=2 limits results to at most 2."""
    results = api_client.search_alerts(size=2)
    assert len(results) <= 2


# ─── Single alert ─────────────────────────────────────────────────────────────


def test_get_alert_404_for_nonexistent_id(api_client):
    """Returns 404 with 'Alert not found' for a nonexistent alert ID."""
    resp = api_client.session.get(
        f"{api_client.base_url}/api/alerts/nonexistent-alert-id-12345", timeout=5
    )
    assert resp.status_code == 404
    assert "Alert not found" in resp.json().get("detail", "")


# ─── Ledger endpoint ──────────────────────────────────────────────────────────


def test_ledger_endpoint_returns_alert_history(api_client, ledger_client):
    """Verify ledger response structure (id, alert_id, state, timestamp, source_service)."""
    result = api_client.generate_alerts(count=1)
    alert_id = result["alert_ids"][0]
    wait_for_alert_terminal(ledger_client, alert_id, timeout=30)

    ledger = api_client.get_ledger(alert_id)
    assert isinstance(ledger, list)
    assert len(ledger) >= 1
    for entry in ledger:
        assert "id" in entry
        assert "alert_id" in entry
        assert entry["alert_id"] == alert_id
        assert "state" in entry
        assert "timestamp" in entry
        assert "source_service" in entry


def test_ledger_endpoint_404_for_unknown_alert(api_client):
    """Returns 404 for an alert ID not in the ledger."""
    resp = api_client.session.get(
        f"{api_client.base_url}/api/ledger/nonexistent-alert-id-99999", timeout=5
    )
    assert resp.status_code == 404


def test_ledger_entries_ordered_by_id(api_client, ledger_client):
    """Ledger entries are in ascending id order."""
    result = api_client.generate_alerts(count=1)
    alert_id = result["alert_ids"][0]
    wait_for_alert_terminal(ledger_client, alert_id, timeout=30)

    ledger = api_client.get_ledger(alert_id)
    ids = [entry["id"] for entry in ledger]
    assert ids == sorted(ids), f"Ledger entries not in ascending id order: {ids}"


# ─── Generate endpoint ────────────────────────────────────────────────────────


def test_generate_with_force_fingerprint(api_client):
    """Verify returned fingerprint matches forced value."""
    forced_fp = "abcdef1234567890"
    result = api_client.generate_alerts(count=1, force_fingerprint=forced_fp)
    assert result["fingerprints"][0] == forced_fp


def test_generate_with_payload_override(api_client, ledger_client):
    """Payload overrides merge into the generated alert."""
    custom_ip = "10.99.99.99"
    result = api_client.generate_alerts(count=1, payload={"source_ip": custom_ip})
    alert_id = result["alert_ids"][0]

    terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
    if terminal != "STORED":
        pytest.skip(f"Alert hit fault injection ({terminal})")

    alert = api_client.get_alert(alert_id)
    assert alert["source_ip"] == custom_ip


def test_generate_count_clamped_to_bounds(api_client):
    """count=0 gives 1 alert, count=200 gives 100 alerts."""
    session = api_client.session
    base = api_client.base_url

    # count=0 → clamped to 1
    resp = session.post(f"{base}/api/generate", json={"count": 0, "payload": {"source": "[E2ETests]"}}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    assert data["generated"] == 1
    assert len(data["alert_ids"]) == 1

    # count=200 → clamped to 100
    resp = session.post(f"{base}/api/generate", json={"count": 200, "payload": {"source": "[E2ETests]"}}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    assert data["generated"] == 100
    assert len(data["alert_ids"]) == 100


def test_generate_with_no_body(api_client):
    """No body defaults to count=1."""
    session = api_client.session
    resp = session.post(f"{api_client.base_url}/api/generate", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    assert data["generated"] == 1
    assert len(data["alert_ids"]) == 1


def test_generate_response_includes_fingerprints(api_client):
    """Fingerprints list has correct count and each is a 16-char hex string."""
    count = 5
    result = api_client.generate_alerts(count=count)
    assert len(result["fingerprints"]) == count
    for fp in result["fingerprints"]:
        assert isinstance(fp, str)
        assert len(fp) == 16
        # Verify it's valid hex
        int(fp, 16)


# ─── Stats endpoint ───────────────────────────────────────────────────────────


def test_stats_response_schema(api_client):
    """Verify all expected fields exist with correct types."""
    stats = api_client.get_stats()

    # Integer fields
    int_fields = [
        "total_produced", "currently_queued", "currently_processing",
        "total_stored", "total_failed", "total_duplicates",
        "unaccounted", "queue_depth",
    ]
    for field in int_fields:
        assert field in stats, f"Missing field: {field}"
        assert isinstance(stats[field], int), f"{field} should be int, got {type(stats[field])}"

    # Boolean field
    assert "accounting_balanced" in stats
    assert isinstance(stats["accounting_balanced"], bool)

    # Float field
    assert "avg_processing_latency_ms" in stats
    assert isinstance(stats["avg_processing_latency_ms"], (int, float))

    # String or None field
    assert "last_event_at" in stats
    assert stats["last_event_at"] is None or isinstance(stats["last_event_at"], str)
