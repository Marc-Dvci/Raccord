from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from raccord import mcp_gateway
from raccord.telemetry import LogLine, RemoteExporters, Sample, Span


def test_mcp_gateway_rejects_missing_and_wrong_bearer(monkeypatch):
    settings = SimpleNamespace(
        mcp_gateway_token="correct-high-entropy-value",
        mcp_upstream_url="http://127.0.0.1:8000/mcp",
    )
    monkeypatch.setattr(mcp_gateway, "get_settings", lambda: settings)
    client = TestClient(mcp_gateway.app)

    missing = client.post("/mcp", json={"jsonrpc": "2.0"})
    wrong = client.post(
        "/mcp",
        json={"jsonrpc": "2.0"},
        headers={"Authorization": "Bearer wrong"},
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401


def test_mcp_gateway_health_fails_closed_without_token(monkeypatch):
    settings = SimpleNamespace(mcp_gateway_token="")
    monkeypatch.setattr(mcp_gateway, "get_settings", lambda: settings)
    response = TestClient(mcp_gateway.app).get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"


def test_authenticated_otlp_exports_metrics_logs_and_traces(monkeypatch):
    settings = SimpleNamespace(
        loki_url="https://logs.example.invalid",
        otlp_endpoint="https://otlp.example.invalid/otlp",
        grafana_url="https://example.grafana.net",
        grafana_service_account_token="grafana-api-token",
        otlp_username="123456",
        otlp_auth_token="cloud-ingest-token",
    )
    monkeypatch.setattr("raccord.telemetry.get_settings", lambda: settings)
    requests = []

    class Response:
        status_code = 200

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr("raccord.telemetry.httpx.post", post)
    exporters = RemoteExporters()
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    assert exporters.probe()
    assert exporters.push_metrics([("raccord_test", {"scope": "demo"}, Sample(now, 1.0))])
    assert exporters.push_logs([LogLine(now, {"level": "info"}, "ready")])
    assert exporters.push_spans(
        [
            Span(
                span_id="01" * 8,
                trace_id="02" * 16,
                parent_id=None,
                name="reason",
                service="raccord-agent",
                start=now,
                duration_ms=12.0,
            )
        ]
    )

    assert [url.rsplit("/", 1)[-1] for url, _ in requests] == [
        "metrics",
        "metrics",
        "logs",
        "traces",
    ]
    expected_auth = base64.b64encode(b"123456:cloud-ingest-token").decode()
    for _, kwargs in requests:
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_auth}"
        assert kwargs["headers"]["Content-Type"] == "application/x-protobuf"
        assert b"cloud-ingest-token" not in kwargs["content"]
