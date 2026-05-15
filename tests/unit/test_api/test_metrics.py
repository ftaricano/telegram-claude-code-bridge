"""Metrics endpoint tests for JAR-163."""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus


def test_metrics_returns_durability_aggregates():
    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = "gh-secret"
    settings.webhook_api_secret = "api-secret"
    repo = MagicMock()
    repo.collect_metrics = AsyncMock(
        return_value={
            "queue_depth_current": 2,
            "queue_depth_max": 7,
            "dropped_count_last_1h": 1,
            "dropped_count_last_24h": 3,
            "worker_lag_ms_max": 150,
            "checkpoints_pending": 4,
            "checkpoints_failed": 1,
        }
    )
    storage = MagicMock()
    storage.durability = repo
    app = create_api_app(EventBus(), settings, storage=storage)
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "queue_depth_current": 2,
        "queue_depth_max": 7,
        "dropped_count_last_1h": 1,
        "dropped_count_last_24h": 3,
        "worker_lag_ms_max": 150,
        "checkpoints_pending": 4,
        "checkpoints_failed": 1,
    }
