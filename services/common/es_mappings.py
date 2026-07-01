ES_MAPPINGS = {
    "properties": {
        "alert_id":    {"type": "keyword"},
        "title":       {"type": "text"},
        "description": {"type": "text"},
        "severity":    {"type": "keyword"},
        "source_ip":   {"type": "ip"},
        "dest_ip":     {"type": "ip"},
        "alert_type":  {"type": "keyword"},
        "timestamp":   {"type": "date"},
        "fingerprint": {"type": "keyword"},
        "source":      {"type": "keyword"},
        "metadata":    {"type": "object", "enabled": False},
    }
}
