"""Flow 2: Duplicate detection tests — deterministic via force_fingerprint.

Each test injects alerts with a unique fingerprint so tests are fully independent.
No shared state, no ordering dependency between tests.
"""
import uuid

import pytest
import requests as req

from helpers.wait_utils import (
    wait_for_alert_terminal,
    wait_for_accounting_balanced,
    poll_until,
)

pytestmark = pytest.mark.e2e


def _unique_fp():
    return uuid.uuid4().hex[:16]


def _wait_for_es_searchable(api_client, fingerprint, timeout=10):
    """Poll until the fingerprint is findable via ES search (handles refresh delay)."""
    def check():
        results = api_client.search_alerts(q=None, size=100)
        return any(a.get("fingerprint") == fingerprint for a in results)
    poll_until(check, timeout=timeout, interval=0.5, description=f"fingerprint {fingerprint} searchable in ES")


def test_duplicate_detected_with_same_fingerprint(api_client, ledger_client):
    """Inject two alerts with the same fingerprint — second must be DUPLICATE_DROPPED."""
    fp = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_a = result_a["alert_ids"][0]
    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    assert terminal_a == "STORED", f"First alert should be STORED, got {terminal_a}"
    _wait_for_es_searchable(api_client, fp)

    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_b = result_b["alert_ids"][0]
    terminal_b = wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    assert terminal_b == "DUPLICATE_DROPPED", (
        f"Expected DUPLICATE_DROPPED for second alert, got {terminal_b}"
    )


def test_duplicate_not_stored_in_elasticsearch(api_client, ledger_client):
    """An alert marked DUPLICATE_DROPPED must NOT exist in Elasticsearch."""
    fp = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_a = result_a["alert_ids"][0]
    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    assert terminal_a == "STORED", f"First alert should be STORED, got {terminal_a}"
    _wait_for_es_searchable(api_client, fp)

    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_b = result_b["alert_ids"][0]
    wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    try:
        api_client.get_alert(id_b)
        pytest.fail(f"Duplicate alert {id_b} should NOT be in ES but was found")
    except req.HTTPError as exc:
        assert exc.response.status_code == 404


def test_original_remains_stored_after_duplicate(api_client, ledger_client):
    """After a duplicate is dropped, the original alert is still in ES with correct data."""
    fp = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_a = result_a["alert_ids"][0]
    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    assert terminal_a == "STORED", f"First alert should be STORED, got {terminal_a}"
    _wait_for_es_searchable(api_client, fp)

    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_b = result_b["alert_ids"][0]
    wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    original = api_client.get_alert(id_a)
    assert original["alert_id"] == id_a
    assert original["fingerprint"] == fp
    assert original["severity"] in ("low", "medium", "high", "critical")


def test_duplicate_ledger_entry_has_fingerprint_metadata(api_client, ledger_client):
    """The DUPLICATE_DROPPED ledger row must contain the fingerprint in metadata."""
    fp = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_a = result_a["alert_ids"][0]
    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    assert terminal_a == "STORED", f"First alert should be STORED, got {terminal_a}"
    _wait_for_es_searchable(api_client, fp)

    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp)
    id_b = result_b["alert_ids"][0]
    wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    rows = ledger_client.get_alert_states(id_b)
    dup_row = next((r for r in rows if r["state"] == "DUPLICATE_DROPPED"), None)
    assert dup_row is not None, f"No DUPLICATE_DROPPED row for {id_b}, got: {[r['state'] for r in rows]}"
    assert isinstance(dup_row.get("metadata"), dict)
    assert dup_row["metadata"].get("fingerprint") == fp


def test_different_fingerprints_both_stored(api_client, ledger_client):
    """Two alerts with different fingerprints must both reach STORED."""
    fp1 = _unique_fp()
    fp2 = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp1)
    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp2)
    id_a = result_a["alert_ids"][0]
    id_b = result_b["alert_ids"][0]

    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    terminal_b = wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    assert terminal_a == "STORED", f"Alert A should be STORED, got {terminal_a}"
    assert terminal_b == "STORED", f"Alert B should be STORED, got {terminal_b}"

    assert api_client.get_alert(id_a)["fingerprint"] == fp1
    assert api_client.get_alert(id_b)["fingerprint"] == fp2


def test_accounting_balanced_after_duplicates(api_client, ledger_client):
    """Inject 2 unique + 1 duplicate — verify stats show accounting_balanced=true."""
    fp_unique_1 = _unique_fp()
    fp_unique_2 = _unique_fp()

    api_client.generate_alerts(count=1, force_fingerprint=fp_unique_1)
    api_client.generate_alerts(count=1, force_fingerprint=fp_unique_2)
    # Third alert duplicates the first
    api_client.generate_alerts(count=1, force_fingerprint=fp_unique_1)

    stats = wait_for_accounting_balanced(api_client, timeout=60)
    assert stats["accounting_balanced"] is True
    assert stats["unaccounted"] == 0
