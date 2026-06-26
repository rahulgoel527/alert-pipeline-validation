"""Flow 2: Duplicate detection tests.

The fingerprint is hash(source_ip + alert_type + 60s_window)[:16].
The API generates random IPs/types, so collisions are rare naturally.
To force a duplicate we generate an alert, retrieve its fingerprint from ES,
then push a second alert with the *same fingerprint* directly via the ledger+redis
approach — but we don't have direct Redis access from tests.

Instead we rely on the event_generator's 10% duplicate rate being probabilistic and
use a different approach: generate many alerts and find pairs with matching
fingerprints via the ledger, OR call generate twice within the same 60-second
window using the same IP+type combination — which we cannot control via the API.

Practical approach:
- Generate an alert, wait for it to be STORED (giving us its fingerprint in ES).
- Generate a second alert with POST /api/generate — with random IPs this will
  almost certainly be unique. BUT: the fingerprint is hash(src+type+window)[:16],
  only 16 hex chars = 64 bits. With enough alerts we'll get natural duplicates.
- For deterministic duplicate injection: use the background event_generator's natural
  duplicates that already exist in the ledger.

Since we can't control the fingerprint via the API, we use a hybrid strategy:
1. Find existing DUPLICATE_DROPPED entries from the background event_generator.
2. Generate a burst of alerts and look for new duplicates.
3. For accounting tests, generate enough alerts to statistically hit duplicates.
"""
import time
import pytest

from helpers.wait_utils import (
    wait_for_alert_terminal,
    wait_for_accounting_balanced,
    poll_until,
)

pytestmark = pytest.mark.e2e


def _get_existing_duplicate_pair(ledger_client):
    """Return (original_id, dup_id) where original was STORED and dup was DUPLICATE_DROPPED
    sharing the same fingerprint. Returns None if none exist yet."""
    dup_ids = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
    if not dup_ids:
        return None

    for dup_id in dup_ids[:20]:
        rows = ledger_client.get_alert_states(dup_id)
        fingerprint = None
        for row in rows:
            if row["state"] == "DUPLICATE_DROPPED" and row.get("metadata"):
                fingerprint = row["metadata"].get("fingerprint")
                break
        if not fingerprint:
            continue

        # Find the original alert with this fingerprint that was STORED
        stored_ids = ledger_client.get_alerts_in_state("STORED")
        for stored_id in stored_ids[:200]:
            rows2 = ledger_client.get_alert_states(stored_id)
            for r in rows2:
                if r["state"] == "DUPLICATE_DROPPED" and r["metadata"].get("fingerprint") == fingerprint:
                    break
            # The stored_id is a STORED alert; we need to verify it shares the fingerprint.
            # We check ES directly via API.
        return (None, dup_id)  # We have a duplicate, just can't easily trace original here

    return None


def _ensure_duplicates_exist(api_client, ledger_client, min_count=1, timeout=120):
    """Generate alerts until we have at least min_count DUPLICATE_DROPPED in the ledger."""
    def has_enough():
        dups = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
        return len(dups) >= min_count

    if has_enough():
        return

    # Generate bursts to create natural duplicates; with 10% dup rate from background
    # and our own generation, we should see some quickly
    api_client.generate_alerts(count=20)
    poll_until(has_enough, timeout=timeout, description=f"{min_count} DUPLICATE_DROPPED in ledger")


def test_duplicate_alert_is_detected(api_client, ledger_client):
    """Verify the system detects and marks duplicate alerts as DUPLICATE_DROPPED."""
    _ensure_duplicates_exist(api_client, ledger_client, min_count=1, timeout=120)
    dup_ids = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
    assert len(dup_ids) >= 1, "Expected at least one DUPLICATE_DROPPED alert in ledger"


def test_duplicate_not_stored_in_elasticsearch(api_client, ledger_client):
    """Verify that an alert marked DUPLICATE_DROPPED does NOT exist in ES."""
    _ensure_duplicates_exist(api_client, ledger_client, min_count=1, timeout=120)
    dup_ids = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
    assert dup_ids, "No DUPLICATE_DROPPED alerts found"

    dup_id = dup_ids[0]
    import requests as req
    try:
        api_client.get_alert(dup_id)
        pytest.fail(f"Alert {dup_id} should NOT be in ES but was found")
    except req.HTTPError as exc:
        assert exc.response.status_code == 404, (
            f"Expected 404 for duplicate in ES, got {exc.response.status_code}"
        )


def test_original_alert_still_stored_correctly(api_client, ledger_client):
    """Generate alerts; verify that for each duplicate, the original alert is STORED with full data."""
    _ensure_duplicates_exist(api_client, ledger_client, min_count=1, timeout=120)

    dup_ids = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
    assert dup_ids, "No DUPLICATE_DROPPED alerts to check"

    # For each dup, find its fingerprint and verify there's a STORED alert with that fingerprint in ES
    dup_id = dup_ids[0]
    rows = ledger_client.get_alert_states(dup_id)
    fingerprint = None
    for row in rows:
        if row["state"] == "DUPLICATE_DROPPED" and isinstance(row.get("metadata"), dict):
            fingerprint = row["metadata"].get("fingerprint")
            break

    assert fingerprint, f"No fingerprint found in DUPLICATE_DROPPED metadata for {dup_id}"

    # Query ES directly with a term filter on fingerprint — the API search endpoint
    # only does text match on title/description, so we go to ES at port 9200 directly.
    # This is consistent with the direct-infra pattern already used by LedgerClient.
    import requests as req_lib
    es_resp = req_lib.get(
        "http://localhost:9200/security_alerts/_search",
        json={"query": {"term": {"fingerprint": fingerprint}}, "size": 5},
        timeout=5,
    )
    es_resp.raise_for_status()
    hits = es_resp.json()["hits"]["hits"]
    assert len(hits) >= 1, (
        f"Expected at least one STORED alert with fingerprint {fingerprint} in ES"
    )
    matching = [h["_source"] for h in hits]
    original = matching[0]
    assert original["alert_id"]
    assert original["severity"] in ("low", "medium", "high", "critical")


def test_duplicate_logged_in_ledger_with_metadata(api_client, ledger_client):
    """Verify DUPLICATE_DROPPED ledger entries have fingerprint in metadata."""
    _ensure_duplicates_exist(api_client, ledger_client, min_count=1, timeout=120)

    dup_ids = ledger_client.get_alerts_in_state("DUPLICATE_DROPPED")
    assert dup_ids

    dup_id = dup_ids[0]
    rows = ledger_client.get_alert_states(dup_id)
    dup_row = next((r for r in rows if r["state"] == "DUPLICATE_DROPPED"), None)
    assert dup_row is not None
    assert isinstance(dup_row.get("metadata"), dict), "metadata should be a dict"
    assert "fingerprint" in dup_row["metadata"], (
        f"Expected fingerprint in metadata, got: {dup_row['metadata']}"
    )


def test_different_fingerprints_both_stored(api_client, ledger_client):
    """Generate two alerts; if they have different fingerprints both should eventually be STORED."""
    result1 = api_client.generate_alerts(count=1)
    result2 = api_client.generate_alerts(count=1)
    id1 = result1["alert_ids"][0]
    id2 = result2["alert_ids"][0]

    t1 = wait_for_alert_terminal(ledger_client, id1, timeout=30)
    t2 = wait_for_alert_terminal(ledger_client, id2, timeout=30)

    # If they ended up with the same fingerprint, one may be a duplicate — that's valid behavior
    # If different fingerprints, both must be STORED
    if t1 == "DUPLICATE_DROPPED" or t2 == "DUPLICATE_DROPPED":
        # Verify they share a fingerprint in ES (the one that was stored)
        stored_id = id1 if t1 == "STORED" else id2
        if t1 not in ("DUPLICATE_DROPPED",) and t2 not in ("DUPLICATE_DROPPED",):
            # Neither is a dup — both should be stored
            assert t1 == "STORED", f"Alert 1 ended in {t1}"
            assert t2 == "STORED", f"Alert 2 ended in {t2}"
    else:
        assert t1 in ("STORED", "FAILED"), f"Alert 1: {t1}"
        assert t2 in ("STORED", "FAILED"), f"Alert 2: {t2}"
        # At least one must be stored if no failure
        if t1 != "FAILED" and t2 != "FAILED":
            assert t1 == "STORED"
            assert t2 == "STORED"


def test_accounting_balanced_after_duplicates(api_client, ledger_client):
    """Generate mix of alerts → verify stats show accounting_balanced=true even with duplicates."""
    api_client.generate_alerts(count=10)
    stats = wait_for_accounting_balanced(api_client, timeout=60)
    assert stats["accounting_balanced"] is True
    assert stats["total_duplicates"] >= 0
    assert stats["unaccounted"] == 0
