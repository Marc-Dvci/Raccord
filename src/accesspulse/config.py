"""Runtime configuration.

Everything has a working local default. The full demo runs with no credentials.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_repo_root() / ".env"),
        env_prefix="AP_",
        extra="ignore",
        case_sensitive=False,
    )

    # Reasoning plane -------------------------------------------------------
    reasoning_mode: str = "offline"  # offline | gemini
    gemini_model: str = "gemini-2.5-pro"

    # Grafana ---------------------------------------------------------------
    grafana_url: str = "http://localhost:3000"
    grafana_service_account_token: str = ""
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    pyroscope_url: str = "http://localhost:4040"
    otlp_endpoint: str = "http://localhost:4318"

    # Grafana MCP -----------------------------------------------------------
    mcp_transport: str = "stub"  # stub | stdio | http
    mcp_stdio_command: str = "docker"
    mcp_stdio_args: str = (
        "run,--rm,-i,-e,GRAFANA_URL,-e,GRAFANA_SERVICE_ACCOUNT_TOKEN,mcp/grafana,-t,stdio"
    )
    mcp_http_url: str = "https://mcp.grafana.com/mcp"

    # Security --------------------------------------------------------------
    approval_signing_key: str = ""
    approval_ttl_seconds: int = 300

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
