"""Flow 3: Failure injection and recovery tests."""
import subprocess
import time
import pytest
import requests as req

from helpers.wait_utils import (
    wait_for_alert_terminal,
    wait_for_accounting_balanced,
    poll_until,
)

pytestmark = pytest.mark.failures

COMPOSE_PROJECT = "alert-pipeline-lab"


def _docker(args, check=True):
    cmd = ["docker", "compose"] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check,
                          cwd="/Users/rg-psl/projects/tuskira-ai/alert-pipeline-lab")


def _ensure_failures_exist(ledger_client, min_count=1, api_client=None, timeout=120):
    """Ensure at least min_count FAILED alerts exist; generate more if needed."""
    def has_enough():
        return len(ledger_client.get_alerts_in_state("FAILED")) >= min_count

    if has_enough():
        return

    if api_client:
        # Generate a burst; 5% failure rate means ~1 failure per 20 alerts
        api_client.generate_alerts(count=40)
    poll_until(has_enough, timeout=timeout, description=f"{min_count} FAILED alerts in ledger")


def test_failed_alert_logged_in_ledger(api_client, ledger_client):
    """Verify that FAILED alerts exist in ledger with error metadata.

    With a 5% failure rate, generating ~40 alerts should yield failures.
    """
    _ensure_failures_exist(ledger_client, min_count=1, api_client=api_client, timeout=120)

    failed_ids = ledger_client.get_alerts_in_state("FAILED")
    assert len(failed_ids) >= 1, "Expected at least one FAILED alert"

    # Verify the FAILED ledger row has error metadata
    fail_id = failed_ids[0]
    rows = ledger_client.get_alert_states(fail_id)
    fail_row = next((r for r in rows if r["state"] == "FAILED"), None)
    assert fail_row is not None
    assert isinstance(fail_row.get("metadata"), dict)
    assert "error" in fail_row["metadata"], (
        f"Expected 'error' key in FAILED metadata, got: {fail_row['metadata']}"
    )


def test_failed_alert_not_in_elasticsearch(api_client, ledger_client):
    """Verify that alerts with FAILED state do NOT appear in ES."""
    _ensure_failures_exist(ledger_client, min_count=1, api_client=api_client, timeout=120)

    failed_ids = ledger_client.get_alerts_in_state("FAILED")
    assert failed_ids

    for fail_id in failed_ids[:5]:
        try:
            api_client.get_alert(fail_id)
            pytest.fail(f"Alert {fail_id} is FAILED but found in ES")
        except req.HTTPError as exc:
            assert exc.response.status_code == 404, (
                f"Expected 404 for FAILED alert in ES, got {exc.response.status_code}"
            )


def test_system_continues_after_failure(api_client, ledger_client):
    """Verify that pipeline continues processing alerts even after some fail."""
    _ensure_failures_exist(ledger_client, min_count=1, api_client=api_client, timeout=120)

    # Generate more alerts after failures exist
    result = api_client.generate_alerts(count=5)
    alert_ids = result["alert_ids"]

    # All new alerts should reach terminal state (pipeline is still alive).
    # allow_stuck=True handles the 2% hung-worker simulation.
    for alert_id in alert_ids:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30, allow_stuck=True)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED", "PROCESSING"), (
            f"Alert {alert_id} in unexpected state {terminal}"
        )
    # At least some must have completed (not all stuck)
    terminals = [ledger_client.get_current_state(aid) for aid in alert_ids]
    assert any(t in ("STORED", "FAILED", "DUPLICATE_DROPPED") for t in terminals), (
        "Pipeline appears stalled — no alerts reached terminal state"
    )


@pytest.mark.slow
def test_elasticsearch_unavailable_handling(api_client, ledger_client):
    """Stop ES → generate alert → verify event_processor logs FAILED → restart ES → verify recovery."""
    # Stop elasticsearch
    _docker(["stop", "elasticsearch"])
    try:
        # Wait a moment for ES to actually go down
        time.sleep(3)

        result = api_client.generate_alerts(count=3)
        alert_ids = result["alert_ids"]

        # Alerts should fail (ES unavailable) within reasonable time
        for alert_id in alert_ids:
            terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
            assert terminal in ("FAILED", "STORED"), (
                f"Alert {alert_id} got {terminal}, expected FAILED or STORED"
            )
    finally:
        # Always restart ES
        _docker(["start", "elasticsearch"])
        # Wait for ES and API to recover
        def es_healthy():
            try:
                health = api_client.health()
                return health["services"].get("elasticsearch", False)
            except Exception:
                return False

        poll_until(es_healthy, timeout=60, description="elasticsearch recovery")

    # After recovery, new alerts should flow through
    result2 = api_client.generate_alerts(count=2)
    for alert_id in result2["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED")


@pytest.mark.slow
def test_redis_connection_recovery(api_client, ledger_client):
    """Pause Redis briefly → unpause → verify event_processor resumes without manual intervention."""
    _docker(["pause", "redis"])
    try:
        time.sleep(3)
    finally:
        _docker(["unpause", "redis"])

    # Wait for Redis to be responsive again
    def redis_ok():
        try:
            health = api_client.health()
            return health["services"].get("redis", False)
        except Exception:
            return False

    poll_until(redis_ok, timeout=30, description="redis recovery")

    # Processor should resume — new alerts must flow
    result = api_client.generate_alerts(count=3)
    for alert_id in result["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED"), (
            f"Alert {alert_id} stuck in {terminal} after redis recovery"
        )


@pytest.mark.slow
def test_pipeline_stability_under_burst(api_client, ledger_client):
    """Generate 50 alerts rapidly → verify all reach terminal state within 120s.

    Checks all alerts in bulk (not serially) to stay within the time bound.
    The 2% stuck-alert simulation means ~1 alert in 50 may stay PROCESSING;
    we allow that and verify accounting is balanced for the non-stuck ones.
    """
    result = api_client.generate_alerts(count=50)
    alert_ids = set(result["alert_ids"])
    assert len(alert_ids) == 50

    TERMINAL = {"STORED", "FAILED", "DUPLICATE_DROPPED"}
    stuck = set()

    def all_terminal():
        pending = set()
        for aid in alert_ids - stuck:
            state = ledger_client.get_current_state(aid)
            if state not in TERMINAL:
                pending.add(aid)
        # Accept up to 2 stuck in PROCESSING (2% of 50 = ~1)
        if len(pending) <= 2:
            return True
        return False

    poll_until(all_terminal, timeout=120, interval=3, description="50 burst alerts terminal")

    # Verify any remaining non-terminal are in PROCESSING (stuck workers) not unknown state
    for aid in alert_ids:
        state = ledger_client.get_current_state(aid)
        assert state in TERMINAL | {"PROCESSING"}, (
            f"Alert {aid} in unexpected state {state}"
        )

    stats = api_client.get_stats()
    assert stats["accounting_balanced"] is True


def test_processing_latency_within_bounds(api_client, ledger_client):
    """Generate alerts → measure PROCESSING→STORED latency → assert avg < 5000ms."""
    result = api_client.generate_alerts(count=10)
    alert_ids = result["alert_ids"]

    latencies = []
    for alert_id in alert_ids:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60, allow_stuck=True)
        if terminal == "STORED":
            ms = ledger_client.get_processing_latency(alert_id)
            if ms is not None:
                latencies.append(ms)

    assert latencies, "No STORED alerts to measure latency (all failed or duplicated)"
    avg_ms = sum(latencies) / len(latencies)
    assert avg_ms < 5000, f"Average latency {avg_ms:.1f}ms exceeds 5000ms bound"
