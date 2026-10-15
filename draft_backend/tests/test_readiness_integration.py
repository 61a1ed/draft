"""Readiness exercises the query pool without needing any open browser streams."""

import pytest

pytestmark = pytest.mark.integration


def test_readiness_after_real_database_initialization(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True}
    assert client.get("/healthz").json() == {"status": "ok"}
