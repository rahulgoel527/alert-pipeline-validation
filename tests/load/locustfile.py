"""Load test for the alert pipeline."""
from locust import HttpUser, task, between


class AlertPipelineUser(HttpUser):
    host = "http://localhost:8000"
    wait_time = between(0.1, 0.5)

    @task(10)
    def generate_alert(self):
        """Primary load — burst alert generation."""
        self.client.post("/api/generate", json={"count": 1, "source": "[Loadtest]"})

    @task(3)
    def check_stats(self):
        """Monitoring during burst."""
        self.client.get("/api/stats")

    @task(2)
    def search_alerts(self):
        """Investigation simulation."""
        self.client.get("/api/alerts/search?severity=high")

    @task(1)
    def get_alert_detail(self):
        """Detail lookup — fetch list then drill into first result."""
        response = self.client.get("/api/alerts")
        if response.status_code == 200:
            alerts = response.json()
            if alerts:
                self.client.get(f"/api/alerts/{alerts[0]['alert_id']}")
