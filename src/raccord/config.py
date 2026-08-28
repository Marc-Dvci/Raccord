"""Runtime configuration.

Everything has a working local default. The full demo runs with no credentials.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_repo_root() / ".env"),
        env_prefix="RACCORD_",
        extra="ignore",
        case_sensitive=False,
    )

    # Reasoning plane -------------------------------------------------------
    reasoning_mode: Literal["offline", "gemini"] = "offline"
    # Gemini 3.7 Flash is the current GA agentic workhorse on Google's global
    # endpoint. It supersedes the preview-only 3.1 Pro default and avoids
    # unsupported 3.x sampling parameters by relying on the model's MEDIUM
    # thinking-level default. Keep it configurable for lifecycle migrations.
    gemini_model: str = "gemini-3.7-flash"
    gemini_location: str = "global"
    agent_engine_resource: str = ""
    agent_engine_location: str = "us-central1"
    # A configured Gemini deployment must never masquerade as a successful
    # deterministic run. Local/offline mode is explicit; cloud failures are
    # surfaced unless an operator deliberately enables degraded operation.
    reasoning_fail_open: bool = False

    # Grafana ---------------------------------------------------------------
    grafana_url: str = "http://localhost:3000"
    grafana_service_account_token: str = ""
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    pyroscope_url: str = "http://localhost:4040"
    otlp_endpoint: str = "http://localhost:4318"
    # Grafana Cloud's OTLP gateway uses HTTP Basic authentication. Keep these
    # separate from the Grafana service-account token: they grant telemetry
    # ingestion, not Grafana API access.
    otlp_username: str = ""
    otlp_auth_token: str = ""

    # Push probe findings, component logs, media-path spans and change
    # annotations into a real Grafana stack. Off by default: the offline demo
    # and the 1,000-scenario benchmark must not depend on a Grafana being there,
    # nor pay for a failed HTTP call on every tick.
    export_telemetry: bool = False

    # Grafana MCP -----------------------------------------------------------
    mcp_transport: Literal["stub", "stdio", "http"] = "stub"
    mcp_stdio_command: str = "docker"
    mcp_stdio_args: str = (
        "run,--rm,-i,-e,GRAFANA_URL,-e,GRAFANA_SERVICE_ACCOUNT_TOKEN,"
        "grafana/mcp-grafana:1.0.0,-t,stdio"
    )
    mcp_http_url: str = "https://mcp.grafana.com/mcp"
    # Authentication presented to the MCP HTTP endpoint. This is deliberately
    # not the Grafana service-account token: the hosted endpoint expects OAuth,
    # while Raccord's unattended deployment uses its own authenticated gateway
    # in front of the official open-source server.
    mcp_bearer_token: str = ""
    # Used only by the small Cloud Run auth proxy that fronts the official MCP
    # sidecar. The application itself consumes mcp_bearer_token above.
    mcp_gateway_token: str = ""
    mcp_upstream_url: str = "http://127.0.0.1:8000/mcp"
    mcp_upstream_health_url: str = "http://127.0.0.1:8000/healthz"
    # How the *MCP server* should reach Grafana, which is not always how *we*
    # reach it: the official server usually runs in a container, where our
    # `http://localhost:3000` is the container itself. Sent as X-Grafana-URL
    # only when set; left empty, the server uses its own configuration, which
    # is what docker-compose already gives it. Grafana Cloud needs it set.
    mcp_grafana_url: str = ""

    # Security --------------------------------------------------------------
    approval_signing_key: str = ""
    approval_ttl_seconds: int = 300
    demo_mode: bool = True
    trusted_identity_header: str = "X-Goog-Authenticated-User-Email"
    operator_role_bindings_json: str = "{}"

    # Optional Google Cloud evidence plane. These are deliberately blank in
    # the credential-free demo and populated by Terraform in deployment.
    evidence_bucket: str = ""
    probe_findings_topic: str = ""
    analytics_dataset: str = ""
    analytics_table_name: str = "incident_outcomes"
    google_cloud_project: str = Field(
        default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    )
    cloud_persistence_strict: bool = False

    # Grafana Agent Observability is configured with its native AGENTO11Y_*
    # environment variables. This flag is exposed in state/readiness without
    # ever returning endpoint credentials to the browser.
    agent_observability_enabled: bool = Field(
        default_factory=lambda: bool(os.environ.get("AGENTO11Y_ENDPOINT"))
    )

    # Storage / serving -----------------------------------------------------
    data_dir: Path = Field(default_factory=lambda: _repo_root() / "var")
    api_port: int = 8080

    # ----------------------------------------------------------------------
    @property
    def mcp_stdio_argv(self) -> list[str]:
        return [a for a in (x.strip() for x in self.mcp_stdio_args.split(",")) if a]

    @property
    def gemini_available(self) -> bool:
        if self.reasoning_mode != "gemini":
            return False
        return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT"))

    @property
    def cloud_evidence_enabled(self) -> bool:
        return bool(self.evidence_bucket or self.probe_findings_topic or self.analytics_dataset)

    @property
    def analytics_table(self) -> str:
        parts = [self.google_cloud_project, self.analytics_dataset, self.analytics_table_name]
        if not all(parts):
            return ""
        return ".".join(parts)

    def ensure_dirs(self) -> None:
        for sub in ("", "evidence", "reports", "bench", "audit"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    def signing_key(self) -> bytes:
        """Stable HMAC key for approval tokens; generated on first use."""
        if self.approval_signing_key:
            return self.approval_signing_key.encode()
        self.ensure_dirs()
        key_file = self.data_dir / "approval_signing.key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32), encoding="utf-8")
        return key_file.read_text(encoding="utf-8").strip().encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
