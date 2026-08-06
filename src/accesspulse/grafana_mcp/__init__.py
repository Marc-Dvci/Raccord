"""Grafana MCP integration.

AccessPulse investigates through the Model Context Protocol, not through direct
datasource calls. Three transports share one interface:

* ``stub``  - an in-process server implementing the same tool surface against the
  local telemetry plane. Runs with no credentials, so the whole demo is
  reproducible on a laptop and in CI.
* ``stdio`` - the official ``grafana/mcp-grafana`` server (binary or
  ``mcp/grafana`` container) over stdio, authenticated with a Grafana service
  account token. This is the unattended production path.
* ``http``  - the hosted Grafana Cloud MCP endpoint over streamable HTTP with
  OAuth 2.1, for interactive operation.

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
