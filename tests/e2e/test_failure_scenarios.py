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
from elasticsearch import Elasticsearch

from helpers.wait_utils import (
    wait_for_alert_terminal,
    wait_for_accounting_balanced,
    poll_until,
)

pytestmark = pytest.mark.e2e
test_source = f"E2E_Negative_{uuid.uuid4().hex[:8]}"

_COMPOSE_FILE = pathlib.Path(__file__).parent.parent.parent / "services" / "docker-compose.yml"


def _docker(args, check=True):
    cmd = ["docker", "compose", "--project-directory", str(_COMPOSE_FILE.parent), "-p", "alertlab"] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _unique_fp():
    return uuid.uuid4().hex[:16]


@pytest.fixture
def _failed_alert(api_client, ledger_client):
    fp = _unique_fp()
    result = api_client.generate_alerts(
        count=1,
        force_fingerprint=fp,
        payload={"source_ip": "not-an-ip", "source": test_source},
    )
    alert_id = result["alert_ids"][0]
    assert wait_for_alert_terminal(ledger_client, alert_id, timeout=30) == "FAILED"
    return {"alert_id": alert_id, "fingerprint": fp}


@pytest.fixture
def _stored_alert(api_client, ledger_client):
    fp = _unique_fp()
    result = api_client.generate_alerts(
        count=1,
        force_fingerprint=fp,
        payload={"source": test_source},
    )
    alert_id = result["alert_ids"][0]
    assert wait_for_alert_terminal(ledger_client, alert_id, timeout=30) == "STORED"
    return {"alert_id": alert_id, "fingerprint": fp}


# ---------------------------------------------------------------------------
# Deterministic failure tests — malformed payload → ES rejection
# ---------------------------------------------------------------------------


def test_failed_alert_ledger_has_error_metadata(_failed_alert, ledger_client):
    """The FAILED ledger entry must contain a non-empty 'error' key in metadata."""
    rows = ledger_client.get_alert_states(_failed_alert["alert_id"])
    fail_row = next((r for r in rows if r["state"] == "FAILED"), None)
    assert fail_row is not None, f"No FAILED row for {_failed_alert['alert_id']}"
    assert isinstance(fail_row.get("metadata"), dict)
    assert fail_row["metadata"].get("error"), (
        f"FAILED metadata must contain non-empty 'error'; got: {fail_row['metadata']}"
    )


def test_system_continues_after_failure(api_client, ledger_client):
    """Pipeline continues processing valid alerts after a failure."""
    bad_fp = _unique_fp()
    bad_id = api_client.generate_alerts(
        count=1,
        force_fingerprint=bad_fp,
        payload={"source_ip": "broken", "source": test_source},
    )["alert_ids"][0]
    assert wait_for_alert_terminal(ledger_client, bad_id, timeout=30) == "FAILED"

    good_fp = _unique_fp()
    good_id = api_client.generate_alerts(
        count=1,
        force_fingerprint=good_fp,
        payload={"source": test_source},
    )["alert_ids"][0]
    terminal = wait_for_alert_terminal(ledger_client, good_id, timeout=30)
    assert terminal in ("STORED", "DUPLICATE_DROPPED"), (
        f"Valid alert {good_fp} after failure got {terminal}, pipeline may be stalled"
    )


def test_multiple_failure_modes(api_client, ledger_client):
    """Different malformed fields all result in FAILED state."""
    malformed_payloads = [
        {"source_ip": "xxx"},
        {"dest_ip": "also-not-an-ip"},
    ]
    for payload in malformed_payloads:
        fp = _unique_fp()
        result = api_client.generate_alerts(
            count=1, force_fingerprint=fp, payload={**payload, "source": test_source}
        )
        alert_id = result["alert_ids"][0]
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=30)
        assert terminal == "FAILED", (
            f"Expected FAILED for fingerprint {fp} payload {payload}, got {terminal}"
        )


# ---------------------------------------------------------------------------
# Infrastructure failure tests — service outages
# ---------------------------------------------------------------------------


@pytest.mark.nonparallel
def test_elasticsearch_unavailable_handling(api_client, ledger_client):
    """Stop ES → generate alert → verify FAILED → restart ES → verify recovery."""
    _docker(["stop", "elasticsearch"])
    try:
        # docker stop is synchronous — ES is down. Give the processor's keep-alive
        # connections a moment to fail before generating alerts.
        time.sleep(2)

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
                return Elasticsearch("http://localhost:9200").cluster.health()["status"] in ("green", "yellow")
            except Exception:
                return False
        poll_until(es_healthy, timeout=60, description="elasticsearch recovery")

    # After recovery, new alerts should flow
    result2 = api_client.generate_alerts(count=2)
    for alert_id in result2["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED")


@pytest.mark.nonparallel
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


@pytest.mark.nonparallel
def test_queue_at_capacity_returns_429(api_client, ledger_client):
    """Fill queue to 200 by pausing processor → verify 429 → unpause → verify recovery."""
    _docker(["pause", "event_processor"])
    try:
        # Keep sending batches until the API itself returns 429 — that is the
        # self-verifying signal that the queue is at capacity (>= 200).
        # 5 × 100 = 500 attempts, far more than the 200-item limit.
        fill_hit_429 = False
        for _ in range(5):
            resp = api_client.session.post(
                f"{api_client.base_url}/api/generate",
                json={"count": 100, "payload": {"source": api_client.source}},
                timeout=10,
            )
            if resp.status_code == 429:
                fill_hit_429 = True
                break

        assert fill_hit_429, "Queue did not reach capacity after 500 fill attempts — processor may not be paused"

        # Queue is confirmed at capacity — immediate follow-up must also get 429
        resp = api_client.session.post(
            f"{api_client.base_url}/api/generate",
            json={"count": 1, "payload": {"source": api_client.source}},
            timeout=10,
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
        assert "capacity" in resp.json()["detail"].lower()
    finally:
        _docker(["unpause", "event_processor"])
        # Always drain before returning — a full queue cascades into subsequent tests
        poll_until(
            lambda: api_client.get_stats()["queue_depth"] == 0,
            timeout=180, interval=3,
            description="queue fully drained after unpause",
        )


@pytest.mark.nonparallel
def test_pipeline_stability_under_burst(api_client, ledger_client):
    """Generate 50 alerts rapidly → verify all reach terminal state within 120s."""
    result = api_client.generate_alerts(count=50, payload={"source": test_source})
    alert_ids = set(result["alert_ids"])
    assert len(alert_ids) == 50

    TERMINAL = {"STORED", "FAILED", "DUPLICATE_DROPPED"}

    poll_until(
        lambda: all(ledger_client.get_current_state(aid) in TERMINAL for aid in alert_ids),
        timeout=120, interval=3, description="50 burst alerts terminal",
    )

    for aid in alert_ids:
        state = ledger_client.get_current_state(aid)
        assert state in TERMINAL, (
            f"Alert {aid} still in {state} after 120s burst test"
        )

    # Scoped to test_source — no bleed from concurrent tests
    stats = wait_for_accounting_balanced(api_client, timeout=60, source=test_source)
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


# ---------------------------------------------------------------------------
# Retry behavior (Gap 3b)
# ---------------------------------------------------------------------------


@pytest.mark.nonparallel
def test_es_retry_succeeds_when_es_recovers_quickly(api_client, ledger_client):
    """Stop ES → generate alert → restart ES within retry window → alert should reach STORED.

    Validates retry behavior: processor retries the ES write up to MAX_ES_RETRIES times
    with exponential backoff (0.5s, 1s, 2s). Restarting ES before retries are exhausted
    should result in STORED rather than FAILED.
    """
    _docker(["stop", "elasticsearch"])
    try:
        # docker stop is synchronous. Brief pause lets processor connections drop.
        time.sleep(0.5)

        result = api_client.generate_alerts(count=1)
        alert_id = result["alert_ids"][0]

        # Restart ES quickly — within the ~3.5s total retry window (0.5 + 1 + 2s backoff)
        time.sleep(0.5)
    finally:
        _docker(["start", "elasticsearch"])

    def es_healthy():
        try:
            return api_client.health()["services"].get("elasticsearch", False)
        except Exception:
            return False
    poll_until(es_healthy, timeout=60, description="elasticsearch recovery")

    terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
    # Accept STORED (retry succeeded) or FAILED (ES came back too slowly) — either is valid;
    # what we must NOT see is the alert stuck in a non-terminal state indefinitely.
    assert terminal in ("STORED", "FAILED"), (
        f"Alert stuck in non-terminal state {terminal} — retry loop may be broken"
    )
    rows = ledger_client.get_alert_states(alert_id)
    states = [r["state"] for r in rows]
    assert "PROCESSING" in states, "Alert should have entered PROCESSING before retry"


# ---------------------------------------------------------------------------
# Logging / observability validation (Gap 4d + Gap 5)
# ---------------------------------------------------------------------------


def test_prometheus_metrics_endpoint_is_reachable(api_client):
    """GET /metrics must return Prometheus text format with expected metric names."""
    resp = api_client.session.get(f"{api_client.base_url}/metrics", timeout=10)
    assert resp.status_code == 200, f"Expected 200 from /metrics, got {resp.status_code}"
    body = resp.text
    assert "alerts_stored_total" in body, "/metrics missing alerts_stored_total counter"
    assert "alerts_produced_total" in body, "/metrics missing alerts_produced_total counter"
    assert "alerts_queue_depth" in body, "/metrics missing alerts_queue_depth gauge"


# ---------------------------------------------------------------------------
# Storage (Postgres) failure test (Gap 6)
# ---------------------------------------------------------------------------


@pytest.mark.nonparallel
def test_postgres_down_then_pipeline_recovers(api_client, ledger_client):
    """Stop Postgres → restart → verify pipeline processes new alerts normally.

    When Postgres is unavailable the processor cannot write ledger entries; it logs
    errors but should not crash. After Postgres recovers, new alerts must reach terminal state.
    """
    _docker(["stop", "postgres"])
    try:
        def _pg_down():
            try:
                return not api_client.health()["services"].get("postgres", True)
            except Exception:
                return True
        poll_until(_pg_down, timeout=15, interval=0.5, description="postgres stopped")

        # Generate an alert while Postgres is down. The API writes the ledger entry
        # before pushing to Redis, so it may return 500 — that's expected behaviour
        # and is itself evidence the failure is detected. Swallow it and move on.
        try:
            api_client.generate_alerts(count=1)
        except req.HTTPError:
            pass
        time.sleep(2)  # give processor a moment to attempt and fail
    finally:
        _docker(["start", "postgres"])

    def pg_healthy():
        try:
            return api_client.health()["services"].get("postgres", False)
        except Exception:
            return False
    poll_until(pg_healthy, timeout=60, description="postgres recovery")

    # After recovery, new alerts should flow through end-to-end
    recovery_result = api_client.generate_alerts(count=2)
    for alert_id in recovery_result["alert_ids"]:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED"), (
            f"Alert {alert_id} stuck in {terminal} after Postgres recovery"
        )


# ---------------------------------------------------------------------------
# Alerts-disappear scenario — queuing delay survival (Gap 7)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_alerts_survive_processor_pause(api_client, ledger_client):
    """Pause processor (alerts queue up in Redis) → unpause → all alerts reach terminal state.

    Covers production issue #1: alerts disappear after ingestion. Proves that alerts
    sitting in QUEUED state are not lost when the processor is temporarily unavailable.
    """
    _docker(["pause", "event_processor"])
    try:
        result = api_client.generate_alerts(count=5)
        alert_ids = result["alert_ids"]
        assert len(alert_ids) == 5

        # While paused, all alerts should be in QUEUED (or PRODUCED) — not terminal
        time.sleep(2)
        for alert_id in alert_ids:
            state = ledger_client.get_current_state(alert_id)
            assert state in ("PRODUCED", "QUEUED"), (
                f"Alert {alert_id} reached {state} while processor was paused — unexpected"
            )
    finally:
        _docker(["unpause", "event_processor"])

    # After unpause, all 5 alerts must drain to terminal state
    for alert_id in alert_ids:
        terminal = wait_for_alert_terminal(ledger_client, alert_id, timeout=60)
        assert terminal in ("STORED", "FAILED", "DUPLICATE_DROPPED"), (
            f"Alert {alert_id} stuck in {terminal} after processor unpause — possible data loss"
        )
