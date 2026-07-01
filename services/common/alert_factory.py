"""Alert construction — pure function, no side effects."""
import hashlib
import random
import uuid
from datetime import datetime, timezone

ALERT_TYPES = ["brute_force", "malware", "phishing", "port_scan", "data_exfiltration"]
SEVERITIES = ["low", "medium", "high", "critical"]
TITLES = {
    "brute_force": "Brute Force Login Attempt",
    "malware": "Malware Detected",
    "phishing": "Phishing Email Detected",
    "port_scan": "Port Scan Detected",
    "data_exfiltration": "Data Exfiltration Attempt",
}
DESCRIPTIONS = {
    "brute_force": "Multiple failed SSH logins from {src}",
    "malware": "Malicious process detected on {dst}",
    "phishing": "Suspicious email link clicked from {src}",
    "port_scan": "SYN scan from {src} targeting {dst}",
    "data_exfiltration": "Large outbound transfer from {src} to {dst}",
}


def build_alert(alert_type, severity, source, window, **kwargs):
    """Construct an alert dict. Pure — no I/O, no side effects.

    Args:
        alert_type: One of ALERT_TYPES.
        severity: One of SEVERITIES.
        source: Source label (e.g. "generator", "api").
        window: Time window (int) for fingerprint computation.
        **kwargs: Optional overrides — force_fingerprint, src_ip, dst_ip.
    """
    src = kwargs.get("src_ip", f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}")
    dst = kwargs.get("dst_ip", f"10.0.{random.randint(1, 254)}.{random.randint(1, 254)}")

    force_fingerprint = kwargs.get("force_fingerprint")
    if force_fingerprint:
        fingerprint = force_fingerprint
    else:
        fingerprint = hashlib.sha256(f"{src}{alert_type}{window}".encode()).hexdigest()[:16]

    alert_id = str(uuid.uuid4())
    title = TITLES[alert_type]
    if source and source != "unknown":
        title = f"[{source.upper()}] {title}"

    return {
        "alert_id": alert_id,
        "title": title,
        "description": DESCRIPTIONS[alert_type].format(src=src, dst=dst),
        "severity": severity,
        "source_ip": src,
        "dest_ip": dst,
        "alert_type": alert_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "source": source or "unknown",
        "metadata": {"source_service": "api"},
    }
