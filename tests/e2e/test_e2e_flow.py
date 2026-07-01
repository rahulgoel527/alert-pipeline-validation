"""Flow 1: End-to-end pipeline validation tests."""
import ipaddress
import pytest

from helpers.wait_utils import (
    wait_for_alert_terminal,
)

pytestmark = pytest.mark.e2e


def test_alert_generation_produces_to_queue(api_client, ledger_client):
    """Generate an alert via API → verify it appears in ledger with PRODUCED state."""
    result = api_client.generate_alerts(count=1)
    assert result["generated"] == 1
    alert_id = result["alert_ids"][0]

    states = ledger_client.get_alert_states(alert_id)
    state_names = [s["state"] for s in states]
    assert "PRODUCED" in state_names, f"Expected PRODUCED in {state_names}"


def test_alert_flows_through_complete_pipeline(api_client, ledger_client):
    """Generate alert → poll until STORED → verify exists in ES with correct data."""
    result = api_client.generate_alerts(count=1)
    alert_id = result["alert_ids"][0]

    terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
    assert terminal == "STORED", f"Alert should be STORED, got {terminal}"

    alert = api_client.get_alert(alert_id)
    assert alert["alert_id"] == alert_id
    assert alert["severity"] in ("low", "medium", "high", "critical")
    assert alert["alert_type"] in (
        "brute_force", "malware", "phishing", "port_scan", "data_exfiltration"
    )


def test_alert_lifecycle_states_are_complete(api_client, ledger_client):
    """Generate alert → wait for terminal state → verify ledger has all states in order:
    PRODUCED → QUEUED → PROCESSING → STORED"""
    result = api_client.generate_alerts(count=1)
    alert_id = result["alert_ids"][0]

    terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
    assert terminal == "STORED", f"Alert should be STORED, got {terminal}"

    states = [s["state"] for s in ledger_client.get_alert_states(alert_id)]
    for expected in ("PRODUCED", "QUEUED", "PROCESSING", "STORED"):
        assert expected in states, f"Missing {expected} in lifecycle {states}"

    # Deduplicate states preserving first-occurrence order before checking ordering
    seen = set()
    unique_states = []
    for s in states:
        if s not in seen:
            seen.add(s)
            unique_states.append(s)

    # Verify ordering: PRODUCED before QUEUED before PROCESSING before STORED
    idx = {s: unique_states.index(s) for s in ("PRODUCED", "QUEUED", "PROCESSING", "STORED")}
    assert idx["PRODUCED"] < idx["QUEUED"] < idx["PROCESSING"] < idx["STORED"], (
        f"States out of order: {states}"
    )


def test_alert_data_integrity(api_client, ledger_client):
    """Generate alert → retrieve from ES → verify all fields are present and valid."""
    result = api_client.generate_alerts(count=1)
    alert_id = result["alert_ids"][0]

    terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
    assert terminal == "STORED", f"Alert should be STORED, got {terminal}"

    alert = api_client.get_alert(alert_id)
    assert alert["alert_id"] == alert_id
    assert isinstance(alert["title"], str) and alert["title"]
    assert isinstance(alert["description"], str) and alert["description"]
    assert alert["severity"] in ("low", "medium", "high", "critical")
    assert alert["alert_type"] in (
        "brute_force", "malware", "phishing", "port_scan", "data_exfiltration"
    )
    ipaddress.ip_address(alert["source_ip"])
    ipaddress.ip_address(alert["dest_ip"])
    assert isinstance(alert["fingerprint"], str) and len(alert["fingerprint"]) == 16
    assert "timestamp" in alert


def test_multiple_alerts_all_processed(api_client, ledger_client):
    """Generate 10 alerts → wait → verify all 10 reach terminal state."""
    count = 10
    result = api_client.generate_alerts(count=count)
    alert_ids = result["alert_ids"]
    assert len(alert_ids) == count

    for alert_id in alert_ids:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED"), (
            f"Alert {alert_id} stuck in {terminal}"
        )


def test_pipeline_stats_are_accurate(api_client, ledger_client):
    """Generate known number of alerts → wait → verify /api/stats counts match ledger."""
    count = 5
    before = api_client.get_stats()
    result = api_client.generate_alerts(count=count)
    alert_ids = result["alert_ids"]

    for alert_id in alert_ids:
        wait_for_alert_terminal(ledger_client, alert_id, timeout=60)

    after = api_client.get_stats()

    # total_produced must have grown by at least count (background may also add)
    assert after["total_produced"] >= before["total_produced"] + count

    # The terminal counts from our generated alerts must match their actual states
    terminal_counts = {}
    for alert_id in alert_ids:
        state = ledger_client.get_current_state(alert_id)
        terminal_counts[state] = terminal_counts.get(state, 0) + 1

    # Stats must reflect at least as many of each terminal state as we generated
    assert after["total_stored"] >= terminal_counts.get("STORED", 0)
    assert after["total_failed"] >= terminal_counts.get("FAILED", 0)
    assert after["total_duplicates"] >= terminal_counts.get("DUPLICATE_DROPPED", 0)
