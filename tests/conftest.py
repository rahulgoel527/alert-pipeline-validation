import os
import pytest

from helpers.api_client import AlertPipelineAPIClient
from helpers.ledger_client import LedgerClient
from helpers.wait_utils import wait_for_alert_terminal


@pytest.fixture(scope="session")
def api_client():
    """Returns configured API client; exits immediately if any lab service is not ready."""
    base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
    client = AlertPipelineAPIClient(base_url)
    try:
        health = client.health()
    except Exception as exc:
        pytest.exit(
            f"API not reachable at {base_url} — run `cd services/ && docker compose -p alertlab --profile pipeline up --build -d` first: {exc}",
            returncode=1,
        )
    not_ready = [svc for svc, up in health.get("services", {}).items() if not up]
    if not_ready:
        pytest.exit(
            f"Lab services not ready — run `cd services/ && docker compose -p alertlab --profile pipeline up --build -d`: {not_ready}",
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



