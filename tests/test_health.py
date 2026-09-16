from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["env"] == "test"


def test_health_probe_skips_request_log(client: TestClient, caplog) -> None:
    with caplog.at_level("INFO"):
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert "http.request.start" not in caplog.text
    assert "http.request.end" not in caplog.text
