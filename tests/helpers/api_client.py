import requests


class AlertPipelineAPIClient:
    def __init__(self, base_url="http://localhost:8000", source='[E2ETests]'):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.source = source

    def health(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/health", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def get_alerts(self, limit=100) -> list:
        resp = self.session.get(f"{self.base_url}/api/alerts", params={"size": limit}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_alert(self, alert_id: str) -> dict:
        resp = self.session.get(f"{self.base_url}/api/alerts/{alert_id}", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def search_alerts(self, **kwargs) -> list:
        resp = self.session.get(f"{self.base_url}/api/alerts/search", params=kwargs, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_stats(self, source=None) -> dict:
        params = {"source": source} if source is not None else {}
        resp = self.session.get(f"{self.base_url}/api/stats", params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def get_ledger(self, alert_id: str) -> list:
        resp = self.session.get(f"{self.base_url}/api/ledger/{alert_id}", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def generate_alerts(self, count: int = 1, force_fingerprint=None, payload=None, **kwargs) -> dict:
        merged_payload = {"source": self.source}
        if payload:
            merged_payload.update(payload)

        body = {"count": count, "payload": merged_payload}
        if force_fingerprint:
            body["force_fingerprint"] = force_fingerprint
        body.update(kwargs)

        resp = self.session.post(
            f"{self.base_url}/api/generate",
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
