"""Grafana MCP integration.

Raccord investigates through the Model Context Protocol, not through direct
datasource calls. Three transports share one interface:

* ``stub``  - an in-process server implementing the same tool surface against the
  local telemetry plane. Runs with no credentials, so the whole demo is
  reproducible on a laptop and in CI.
* ``stdio`` - the official ``grafana/mcp-grafana`` server (binary or container)
  over stdio, authenticated with a Grafana service-account token.
* ``http``  - streamable HTTP. The unattended cloud path uses the official
  server behind Raccord's authenticated gateway; Grafana Cloud's hosted endpoint
  uses interactive OAuth 2.1.

Whichever transport is configured, the agent code is identical: it discovers the
server's real tool list, resolves the capability names it needs against that
list, and refuses to run the hero investigation if a required capability is
missing. Nothing is faked when a real server is present.
"""

from __future__ import annotations

from .client import (
    CAPABILITIES,
    Capability,
    GrafanaMCPClient,
    MCPCallError,
    MCPUnavailable,
    ToolInfo,
    build_client,
)
from .stub import StubGrafanaMCP

__all__ = [
    "CAPABILITIES",
    "Capability",
    "GrafanaMCPClient",
    "MCPCallError",
    "MCPUnavailable",
    "StubGrafanaMCP",
    "ToolInfo",
    "build_client",
]
