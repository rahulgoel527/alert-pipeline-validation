import pytest

from helpers.api_client import AlertPipelineAPIClient
from helpers.ledger_client import LedgerClient
from helpers.wait_utils import wait_for_alert_terminal, wait_for_new_alerts_processed


@pytest.fixture(scope="session")
def api_client():
    """Returns configured API client; exits immediately if any lab service is not ready."""
    client = AlertPipelineAPIClient("http://localhost:8000")
    try:
        health = client.health()
    except Exception as exc:
        pytest.exit(
            f"API not reachable at localhost:8000 — run `docker compose -f services/docker-compose.yml up --build -d` first: {exc}",
            returncode=1,
        )
    not_ready = [svc for svc, up in health.get("services", {}).items() if not up]
    if not_ready:
        pytest.exit(
            f"Lab services not ready — run `docker compose -f services/docker-compose.yml up --build -d`: {not_ready}",
            returncode=1,
        )
    return client


@pytest.fixture(scope="session")
def ledger_client():
    """Returns configured Postgres client; exits immediately if Postgres is unreachable."""
    client = LedgerClient(
        host="localhost",
        port=5432,
        dbname="alert_ledger",
        user="ledger",
        password="ledger123",
    )
    try:
        client.get_state_counts()
    except Exception as exc:
        pytest.exit(
            f"Cannot connect to Postgres ledger — is the lab running?: {exc}",
            returncode=1,
        )
    return client


@pytest.fixture
def baseline_stats(api_client):
    """Captures current stats before test runs to isolate test-generated alert noise."""
    return api_client.get_stats()


@pytest.fixture
def generate_and_wait(api_client, ledger_client):
    """Factory: generates N alerts via API, waits for all to reach terminal state.
    Returns list of alert_ids."""
    def _generate(count=1, timeout=60):
        result = api_client.generate_alerts(count=count)
        alert_ids = result["alert_ids"]
        for alert_id in alert_ids:
            wait_for_alert_terminal(ledger_client, alert_id, timeout=timeout)
        return alert_ids

    return _generate
