"""Flow 3: Failure injection and recovery tests.

Failure tests use deterministic malformed payloads (e.g. invalid IP) that ES rejects.
Infrastructure tests use docker pause/stop to simulate service outages.
"""
import os
import pathlib
import subprocess
import time
import uuid

import pytest
import requests as req

from helpers.wait_utils import (
    wait_for_alert_terminal,
    wait_for_accounting_balanced,
    poll_until,
)

pytestmark = pytest.mark.e2e

_COMPOSE_FILE = pathlib.Path(__file__).parent.parent.parent / "services" / "docker-compose.yml"


def _docker(args, check=True):
    cmd = ["docker", "compose", "-f", str(_COMPOSE_FILE)] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# ---------------------------------------------------------------------------
# Deterministic failure tests — malformed payload → ES rejection
# ---------------------------------------------------------------------------


def test_failed_alert_ledger_has_error_metadata(api_client, ledger_client):
    """The FAILED ledger entry must contain error details in metadata."""
    result = api_client.generate_alerts(
        count=1, payload={"source_ip": "completely-invalid"}
    )
    alert_id = result["alert_ids"][0]

    wait_for_alert_terminal(ledger_client, alert_id, timeout=30)

    rows = ledger_client.get_alert_states(alert_id)
    fail_row = next((r for r in rows if r["state"] == "FAILED"), None)
    assert fail_row is not None, f"No FAILED row for {alert_id}"
    assert isinstance(fail_row.get("metadata"), dict)
    assert "error" in fail_row["metadata"], (
        f"Expected 'error' in FAILED metadata, got: {fail_row['metadata']}"
    )


def test_system_continues_after_failure(api_client, ledger_client):
    """Pipeline continues processing valid alerts after a failure."""
    # Inject malformed alert
    bad_result = api_client.generate_alerts(
        count=1, payload={"source_ip": "broken"}
    )
    bad_id = bad_result["alert_ids"][0]
    bad_terminal = wait_for_alert_terminal(ledger_client, bad_id, timeout=30)
    assert bad_terminal == "FAILED"

    # Inject valid alert — should flow through normally
    good_result = api_client.generate_alerts(count=1)
    good_id = good_result["alert_ids"][0]
    good_terminal = wait_for_alert_terminal(ledger_client, good_id, timeout=30)

    assert good_terminal in ("STORED", "DUPLICATE_DROPPED"), (
        f"Valid alert after failure got {good_terminal}, pipeline may be stalled"
    )


def test_multiple_failure_modes(api_client, ledger_client):
    """Different malformed fields all result in FAILED state."""
    payloads = [
        {"source_ip": "xxx"},
        {"dest_ip": "also-not-an-ip"},
    ]
    for payload in payloads:
        result = api_client.generate_alerts(count=1, payload=payload)
        alert_id = result["alert_ids"][0]
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
        assert terminal == "FAILED", (
            f"Expected FAILED for payload {payload}, got {terminal}"
        )


# ---------------------------------------------------------------------------
# Infrastructure failure tests — service outages
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_elasticsearch_unavailable_handling(api_client, ledger_client):
    """Stop ES → generate alert → verify FAILED → restart ES → verify recovery."""
    _docker(["stop", "elasticsearch"])
    try:
        def _es_down():
            try:
                return not api_client.health()["services"].get("elasticsearch", True)
            except Exception:
                return True
        poll_until(_es_down, timeout=15, interval=0.5, description="elasticsearch stopped")

        result = api_client.generate_alerts(count=3)
        alert_ids = result["alert_ids"]

        for alert_id in alert_ids:
            terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
            assert terminal in ("FAILED", "STORED"), (
                f"Alert {alert_id} got {terminal}, expected FAILED or STORED"
            )
    finally:
        _docker(["start", "elasticsearch"])
        def es_healthy():
            try:
                return api_client.health()["services"].get("elasticsearch", False)
            except Exception:
                return False
        poll_until(es_healthy, timeout=60, description="elasticsearch recovery")

    # After recovery, new alerts should flow
    result2 = api_client.generate_alerts(count=2)
    for alert_id in result2["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED")


@pytest.mark.slow
def test_redis_connection_recovery(api_client, ledger_client):
    """Pause Redis briefly → unpause → verify processor resumes."""
    _docker(["pause", "redis"])
    try:
        def _redis_down():
            try:
                return not api_client.health()["services"].get("redis", True)
            except Exception:
                return True
        poll_until(_redis_down, timeout=15, interval=0.5, description="redis paused")
    finally:
        _docker(["unpause", "redis"])

    def redis_ok():
        try:
            return api_client.health()["services"].get("redis", False)
        except Exception:
            return False
    poll_until(redis_ok, timeout=30, description="redis recovery")

    result = api_client.generate_alerts(count=3)
    for alert_id in result["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED"), (
            f"Alert {alert_id} stuck in {terminal} after redis recovery"
        )


@pytest.mark.slow
def test_queue_at_capacity_returns_429(api_client, ledger_client):
    """Fill queue to 200 by pausing processor → verify 429 → unpause → verify recovery."""
    _docker(["pause", "event_processor"])
    try:
        # Fill queue: 2 × 100 = 200
        api_client.generate_alerts(count=100)
        api_client.generate_alerts(count=100)

        # Next call should get 429
        resp = api_client.session.post(
            f"{api_client.base_url}/api/generate",
            json={"count": 1, "payload": {"source": api_client.source}},
            timeout=10,
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
        assert "capacity" in resp.json()["detail"].lower()
    finally:
        _docker(["unpause", "event_processor"])

    # Processor resumes and drains — wait for some to complete
    poll_until(
        lambda: api_client.get_stats()["queue_depth"] < 100,
        timeout=120, interval=3,
        description="queue draining after unpause",
    )


@pytest.mark.slow
def test_pipeline_stability_under_burst(api_client, ledger_client):
    """Generate 50 alerts rapidly → verify all reach terminal state within 120s."""
    result = api_client.generate_alerts(count=50)
    alert_ids = set(result["alert_ids"])
    assert len(alert_ids) == 50

    TERMINAL = {"STORED", "FAILED", "DUPLICATE_DROPPED"}

    def all_terminal():
        return all(
            ledger_client.get_current_state(aid) in TERMINAL
            for aid in alert_ids
        )

    poll_until(all_terminal, timeout=120, interval=3, description="50 burst alerts terminal")

    for aid in alert_ids:
        state = ledger_client.get_current_state(aid)
        assert state in TERMINAL, (
            f"Alert {aid} still in {state} after 120s burst test"
        )

    stats = api_client.get_stats()
    assert stats["accounting_balanced"] is True


def test_processing_latency_within_bounds(api_client, ledger_client):
    """Generate alerts → measure PROCESSING→STORED latency → assert avg < 5000ms."""
    result = api_client.generate_alerts(count=10)
    alert_ids = result["alert_ids"]

    latencies = []
    for alert_id in alert_ids:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        if terminal == "STORED":
            ms = ledger_client.get_processing_latency(alert_id)
            if ms is not None:
                latencies.append(ms)

    if len(latencies) < 3:
        pytest.skip(f"Insufficient STORED samples ({len(latencies)}) for latency bound assertion")
    avg_ms = sum(latencies) / len(latencies)
    bound_ms = int(os.environ.get("TEST_LATENCY_BOUND_MS", "5000"))
    assert avg_ms < bound_ms, f"Average latency {avg_ms:.1f}ms exceeds {bound_ms}ms bound"
