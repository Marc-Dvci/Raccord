"""Grafana Agent Observability and OpenTelemetry bootstrap.

The integration is lazy: local tests and the credential-free simulator import no
Grafana SDK. A cloud deployment that sets ``AGENTO11Y_ENDPOINT`` gets official
Google ADK callbacks, metadata-only content capture, secret redaction, and OTLP
traces/metrics sent to the same Grafana Cloud tenant as the operational data.
"""

from __future__ import annotations

import atexit
import base64
import logging
from functools import lru_cache
from typing import Any

from . import __version__
from .config import get_settings

LOG = logging.getLogger(__name__)
_shutdown_registered = False


def _otlp_headers(username: str, token: str) -> dict[str, str]:
    if not username or not token:
        return {}
    encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


@lru_cache(maxsize=1)
def configure_otel() -> bool:
    """Install OTLP span and metric providers once when cloud auth is present."""
    settings = get_settings()
    if not (settings.otlp_endpoint and settings.otlp_username and settings.otlp_auth_token):
        return False
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False

    headers = _otlp_headers(settings.otlp_username, settings.otlp_auth_token)
    endpoint = settings.otlp_endpoint.rstrip("/")
    resource = Resource.create(
        {
            "service.name": "raccord-agent",
            "service.version": __version__,
            "deployment.environment.name": "hackathon",
        }
    )

    # These setters intentionally run before Client() creates its tracer/meter.
    # A process creates the integration once, so duplicate-provider warnings are
    # a sign of external instrumentation and are avoided by the cache above.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
        )
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers),
        export_interval_millis=15_000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
    return True


@lru_cache(maxsize=1)
def get_agento11y_client():
    settings = get_settings()
    if not settings.agent_observability_enabled:
        return None
    try:
        from agento11y import (
            Client,
            ClientConfig,
            ContentCaptureMode,
            SecretRedactionOptions,
            create_secret_redaction_sanitizer,
        )
    except ImportError as exc:
        raise RuntimeError(
            "AGENTO11Y_ENDPOINT is set but agento11y packages are not installed"
        ) from exc

    configure_otel()
    client = Client(
        ClientConfig(
            agent_name="raccord-reasoning",
            agent_version=__version__,
            content_capture=ContentCaptureMode.METADATA_ONLY,
            generation_sanitizer=create_secret_redaction_sanitizer(
                SecretRedactionOptions(
                    redact_input_messages=True,
                    redact_email_addresses=True,
                )
            ),
            tags={"product": "raccord", "track": "grafana", "runtime": "google-adk"},
        )
    )

    global _shutdown_registered
    if not _shutdown_registered:
        atexit.register(client.shutdown)
        _shutdown_registered = True
    return client


def with_google_adk_observability(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge official Agent Observability callbacks into an ADK agent config."""
    client = get_agento11y_client()
    if client is None:
        return dict(config or {})
    from agento11y_google_adk import with_agento11y_google_adk_callbacks

    return with_agento11y_google_adk_callbacks(
        config,
        client=client,
        provider_resolver="auto",
    )


def require_agent_observability() -> None:
    """Fail readiness when observability was selected but cannot initialize."""
    if get_settings().agent_observability_enabled:
        get_agento11y_client()
