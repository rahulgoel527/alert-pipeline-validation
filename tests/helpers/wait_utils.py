import time

TERMINAL_STATES = {"STORED", "FAILED", "DUPLICATE_DROPPED"}


def poll_until(condition_fn, timeout=30, interval=2, description="condition"):
    """Poll condition_fn every interval seconds until True or timeout. Raises TimeoutError."""
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            if condition_fn():
                return
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    detail = f": {last_exc}" if last_exc else ""
    raise TimeoutError(f"Timed out waiting for {description} after {timeout}s{detail}")


def wait_for_alert_terminal(ledger_client, alert_id, timeout=30):
    """Wait until alert reaches a terminal state. Returns the terminal state string."""
    result = {}

    def check():
        state = ledger_client.get_current_state(alert_id)
        if state in TERMINAL_STATES:
            result["state"] = state
            return True
        return False

    poll_until(check, timeout=timeout, description=f"alert {alert_id} terminal state")
    return result["state"]


def wait_for_accounting_balanced(api_client, timeout=30):
    """Wait until /api/stats reports accounting_balanced=true. Returns stats dict."""
    result = {}

    def check():
        stats = api_client.get_stats()
        if stats.get("accounting_balanced"):
            result["stats"] = stats
            return True
        return False

    poll_until(check, timeout=timeout, description="accounting_balanced=true")
    return result["stats"]


def wait_for_new_alerts_processed(api_client, baseline_produced, expected_new, timeout=60):
    """Wait until expected_new alerts beyond baseline have all reached terminal states.

    A terminal alert is one that is in STORED, FAILED, or DUPLICATE_DROPPED.
    We verify by checking: total_stored + total_failed + total_duplicates >= baseline terminal + expected_new.
    Returns current stats dict when condition is met.
    """
    result = {}

    def check():
        stats = api_client.get_stats()
        current_produced = stats["total_produced"]
        if current_produced < baseline_produced + expected_new:
            return False
        current_terminal = (
            stats["total_stored"] + stats["total_failed"] + stats["total_duplicates"]
        )
        # We need at least expected_new more terminals than we had at baseline
        if current_terminal >= result.get("baseline_terminal", 0) + expected_new:
            result["stats"] = stats
            return True
        return False

    # Capture baseline terminal count before waiting
    baseline_stats = api_client.get_stats()
    result["baseline_terminal"] = (
        baseline_stats["total_stored"]
        + baseline_stats["total_failed"]
        + baseline_stats["total_duplicates"]
    )

    poll_until(check, timeout=timeout, description=f"{expected_new} new alerts processed")
    return result["stats"]
