"""Readiness must exercise the real gateway pool and notification subscription."""

import pytest

pytestmark = pytest.mark.integration


def test_readiness_after_real_database_initialization(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True}
    assert client.get("/healthz").json()["notifications_connected"] is True
