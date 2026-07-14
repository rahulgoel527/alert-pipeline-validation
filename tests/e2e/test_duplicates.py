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
test_source = f"Test_{uuid.uuid4().hex[:8]}"

def _unique_fp():
    return uuid.uuid4().hex[:16]


@pytest.fixture
def _create_duplicate_pair(api_client, ledger_client):

    fp = _unique_fp()
    first = api_client.generate_alerts(count=1, force_fingerprint=fp)["alert_ids"][0]
    assert wait_for_alert_terminal(ledger_client, first, timeout=30,) == "STORED"

    def check():
        results = api_client.search_alerts(q=None, size=100)
        return any(a.get("fingerprint") == fp for a in results)
    poll_until(check, timeout=10, interval=0.5, description=f"fingerprint {fp} searchable in ES")

    second = api_client.generate_alerts(count=1, force_fingerprint=fp)["alert_ids"][0]
    assert wait_for_alert_terminal(ledger_client, second, timeout=30) == "DUPLICATE_DROPPED"

    return {
        "fingerprint": fp,
        "original_id": first,
        "duplicate_id": second,
    }

@pytest.fixture(scope="module")
def _create_two_unique_alerts(api_client, ledger_client):

    fp1 = _unique_fp()
    fp2 = _unique_fp()

    result_a = api_client.generate_alerts(count=1, force_fingerprint=fp1, payload={"source": test_source})
    result_b = api_client.generate_alerts(count=1, force_fingerprint=fp2, payload={"source": test_source})
    id_a = result_a["alert_ids"][0]
    id_b = result_b["alert_ids"][0]

    terminal_a = wait_for_alert_terminal(ledger_client, id_a, timeout=30)
    terminal_b = wait_for_alert_terminal(ledger_client, id_b, timeout=30)

    assert terminal_a == "STORED", f"Alert A should be STORED, got {terminal_a}"
    assert api_client.get_alert(id_a)["fingerprint"] == fp1

    assert terminal_b == "STORED", f"Alert B should be STORED, got {terminal_b}"
    assert api_client.get_alert(id_b)["fingerprint"] == fp2

    # Wait for both fingerprints to be ES-searchable so any consumer that sends a
    # duplicate won't race the ES refresh window and land a second STORED instead.
    for fp in (fp1, fp2):
        poll_until(
            lambda fp=fp: any(a.get("fingerprint") == fp for a in api_client.search_alerts(q=None, size=100)),
            timeout=10, interval=0.5, description=f"fingerprint {fp} searchable in ES",
        )

    return {
        "fingerprint1": fp1,
        "fingerprint2": fp2,
        "alert_id1": id_a,
        "alert_id2": id_b
    }


def test_duplicate_detected_with_same_fingerprint(_create_duplicate_pair):
    # The fixture itself is the first test here.
    pass

def test_duplicate_not_stored_in_elasticsearch(api_client, _create_duplicate_pair):
    """An alert marked DUPLICATE_DROPPED must NOT exist in Elasticsearch."""
    id_b = _create_duplicate_pair["duplicate_id"]
    try:
        api_client.get_alert(id_b)
        pytest.fail(f"Duplicate alert {id_b} should NOT be in ES but was found")
    except req.HTTPError as exc:
        assert exc.response.status_code == 404


def test_original_remains_stored_after_duplicate(api_client, _create_duplicate_pair):
    """After a duplicate is dropped, the original alert is still in ES with correct data."""
    fp, id_a = _create_duplicate_pair["fingerprint"], _create_duplicate_pair["original_id"]
    original = api_client.get_alert(id_a)
    assert original["alert_id"] == id_a
    assert original["fingerprint"] == fp


def test_duplicate_ledger_entry_has_fingerprint_metadata(ledger_client, _create_duplicate_pair):
    """The DUPLICATE_DROPPED ledger row must contain the fingerprint in metadata."""
    fp, id_b = _create_duplicate_pair["fingerprint"], _create_duplicate_pair["duplicate_id"]

    rows = ledger_client.get_alert_states(id_b)
    dup_row = next((r for r in rows if r["state"] == "DUPLICATE_DROPPED"), None)
    assert dup_row is not None, f"No DUPLICATE_DROPPED row for {id_b}, got: {[r['state'] for r in rows]}"
    assert isinstance(dup_row.get("metadata"), dict)
    assert dup_row["metadata"].get("fingerprint") == fp


def test_different_fingerprints_both_stored(_create_two_unique_alerts):
    # The fixture itself is the first test here.
    pass


def test_accounting_balanced_after_duplicates(api_client, _create_two_unique_alerts):
    """Inject 2 unique + 1 duplicate under a scoped source — verify balance holds for exactly those alerts."""
    fp1 = _create_two_unique_alerts["fingerprint1"]
    api_client.generate_alerts(count=1, force_fingerprint=fp1, payload={"source": test_source})  # duplicate of fp1
    stats = wait_for_accounting_balanced(api_client, timeout=60, source=test_source)

    assert stats["accounting_balanced"] is True
    assert stats["unaccounted"] == 0
    assert stats["total_produced"] == 3
    assert stats["total_duplicates"] >= 1
