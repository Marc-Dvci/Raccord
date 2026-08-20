"""MCP client interface, capability resolution and transports."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..telemetry import TelemetryPlane
from .adapters import AdapterContext, AdapterError, adapter_for


class MCPUnavailable(RuntimeError):
    """The configured MCP server could not be reached or lacks a capability."""


class MCPCallError(RuntimeError):
    """A tool call failed on the server side."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    """A thing Raccord needs to do, and the server tool names that provide it.

    The Grafana MCP server's tool names have changed across releases and differ
    slightly between the open-source server and the hosted Cloud endpoint. The
    agent asks for a capability; this table resolves it against whatever the
    connected server actually advertises.
    """

    key: str
    candidates: tuple[str, ...]
    required: bool = True
    purpose: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "list_datasources",
        ("list_datasources",),
        True,
        "enumerate the metric, log, trace and profile datasources",
    ),
    # Current open-source builds expose one `get_datasource`; older ones split it
    # by lookup key. Same semantics, same arguments, so both are candidates.
    Capability(
        "get_datasource",
        ("get_datasource", "get_datasource_by_uid", "get_datasource_by_name"),
        False,
    ),
    # Recent servers fold rule listing and retrieval into one dispatch tool.
    # Its arguments and responses differ from the dedicated tools, which is why
    # a name alone is not enough - see adapters.py.
    Capability(
        "list_alert_rules",
        ("list_alert_rules", "alerting_manage_rules"),
        True,
        "read the firing accessibility alert",
    ),
    Capability(
        "get_alert_rule",
        ("get_alert_rule_by_uid", "alerting_manage_rules"),
        True,
        "read the rule definition and labels behind the alert",
    ),
    Capability(
        "query_prometheus",
        ("query_prometheus",),
        True,
        "query SLI series: drift, availability, sessions, budget burn",
    ),
    Capability("list_prometheus_metric_names", ("list_prometheus_metric_names",), False),
    Capability("list_prometheus_label_values", ("list_prometheus_label_values",), False),
    Capability(
        "query_loki_logs",
        ("query_loki_logs",),
        True,
        "read encoder, clock, packager and player logs",
    ),
    Capability("query_loki_stats", ("query_loki_stats",), False),
    # `grafana_api_request` is last on purpose: a native trace tool is preferred
    # wherever one exists. Current open-source builds of the official server
    # expose none, and the generic API tool reaches Tempo through Grafana's
    # datasource proxy - still over MCP, still audited (see adapters.py).
    Capability(
        "query_tempo_traces",
        ("query_tempo_traces", "find_traces", "search_traces", "grafana_api_request"),
        True,
        "follow the media path trace through the affected pool",
    ),
    Capability("get_trace", ("get_trace_by_id", "get_trace"), False),
    Capability(
        "fetch_pyroscope_profile",
        ("fetch_pyroscope_profile",),
        False,
        "profile the probe fleet during the incident",
    ),
    Capability(
        "search_dashboards", ("search_dashboards",), True, "find the dashboard a human should open"
    ),
    Capability("get_dashboard", ("get_dashboard_by_uid",), False),
    Capability(
        "generate_deeplink",
        ("generate_deeplink",),
        False,
        "produce a human-reviewable Grafana link",
    ),
    Capability(
        "find_annotations",
        ("find_annotations", "get_annotations"),
        False,
        "read deployment and configuration annotations",
    ),
    Capability(
        "create_annotation",
        ("create_annotation", "add_annotation"),
        True,
        "write the approved action and the recovery onto the timeline",
    ),
    Capability("list_incidents", ("list_incidents",), False),
    Capability("create_incident", ("create_incident",), False),
    Capability(
        "add_activity_to_incident",
        ("add_activity_to_incident",),
        False,
        "append the approved action to the Grafana incident timeline",
    ),
)

CAPABILITY_BY_KEY = {c.key: c for c in CAPABILITIES}


def _field(obj: Any, *names: str) -> Any:
    """First attribute of `obj` that exists, by any of its historical names.

    The MCP Python SDK moved from camelCase to snake_case between 1.x and 2.x.
    Both generations are installed in the wild and the wire protocol is
    unchanged, so read whichever name the local SDK happens to use.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


class GrafanaMCPClient(ABC):
    """Common behaviour: capability resolution, translation, telemetry, guard rails."""

    transport: str = "abstract"
    # The in-process server implements the canonical argument and response
    # shapes, so it needs no translation. Real servers do.
    adapt: bool = False

    def __init__(self, telemetry: TelemetryPlane | None = None) -> None:
        self.telemetry = telemetry or TelemetryPlane()
        self._tools: dict[str, ToolInfo] = {}
        self._resolved: dict[str, str] = {}
        self.call_log: list[dict[str, Any]] = []
        self.context = AdapterContext()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        tools = await self.list_tools()
        self._tools = {t.name: t for t in tools}
        self._resolved = {}
        missing: list[str] = []
        for cap in CAPABILITIES:
            for candidate in cap.candidates:
                if candidate in self._tools:
                    self._resolved[cap.key] = candidate
                    break
            else:
                if cap.required:
                    missing.append(cap.key)
        if missing:
            raise MCPUnavailable(
                "the connected Grafana MCP server does not expose required capabilities: "
                + ", ".join(missing)
                + f" (server advertises {len(self._tools)} tools)"
            )
        if self.adapt:
            await self._discover_datasources()

    async def _discover_datasources(self) -> None:
        """Learn this Grafana's datasource UIDs; adapters need them per query.

        Discovered rather than assumed: a Grafana someone else provisioned will
        not have named its datasources the way ours does.
        """
        self.context.grafana_url = get_settings().grafana_url
        found: dict[str, str] = {}
        try:
            listed = await self.call("list_datasources")
        except (MCPCallError, MCPUnavailable):
            self.context.datasources = {}
            return
        if isinstance(listed, str):
            import json as _json

            try:
                listed = _json.loads(listed)
            except _json.JSONDecodeError:
                listed = []
        # The stub answers with a bare list; the official server wraps it.
        if isinstance(listed, dict):
            listed = listed.get("datasources") or listed.get("result") or []
        for ds in listed if isinstance(listed, list) else []:
            if not isinstance(ds, dict):
                continue
            kind, uid = ds.get("type"), ds.get("uid")
            # First of each type wins, matching what an operator would pick.
            if kind and uid and kind not in found:
                found[kind] = uid
        self.context.datasources = found

    async def aclose(self) -> None:
        return None

    # -- transport -------------------------------------------------------
    @abstractmethod
    async def list_tools(self) -> list[ToolInfo]: ...

    @abstractmethod
    async def _invoke(self, tool: str, arguments: dict[str, Any]) -> Any: ...

    # -- public API --------------------------------------------------------
    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def tool_for(self, capability: str) -> str:
        if capability not in self._resolved:
            raise MCPUnavailable(f"capability '{capability}' is not available on this server")
        return self._resolved[capability]

    def has(self, capability: str) -> bool:
        return capability in self._resolved

    async def call(self, capability: str, **arguments: Any) -> Any:
        tool = self.tool_for(capability)
        # A server whose tool already speaks the canonical shape gets no
        # adapter and is called unchanged; the stub is always in that case.
        adapter = adapter_for(capability, tool) if self.adapt else None
        try:
            sent = adapter.request(dict(arguments), self.context) if adapter else arguments
        except AdapterError as exc:
            raise MCPUnavailable(
                f"capability '{capability}' cannot be expressed against "
                f"'{tool}' on this server: {exc}"
            ) from exc

        started = time.perf_counter()
        ok = True
        try:
            result = await self._invoke(tool, sent)
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            ok = False
            self._record(tool, sent, started, ok, 0)
            raise MCPCallError(f"{tool} failed: {exc}") from exc
        size = len(str(result))
        self._record(tool, sent, started, ok, size)

        if adapter:
            try:
                return adapter.response(result, dict(arguments))
            except AdapterError as exc:
                raise MCPCallError(f"{tool} returned an unusable shape: {exc}") from exc
        return result

    def _record(self, tool: str, arguments: dict, started: float, ok: bool, size: int) -> None:
        duration_ms = (time.perf_counter() - started) * 1000.0
        entry = {
            "transport": self.transport,
            "tool": tool,
            "arguments": arguments,
            "duration_ms": round(duration_ms, 2),
            "ok": ok,
            "result_bytes": size,
        }
        self.call_log.append(entry)
        self.telemetry.record_mcp_call(tool, arguments, duration_ms, ok, size)

    def record_external_call(
        self,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: float,
        ok: bool,
        result_size: int,
        transport: str = "adk-mcp",
    ) -> None:
        """Merge an MCP call made by ADK into the canonical audit timeline.

        ADK owns its MCP session when Gemini requests additional evidence. The
        operational UI must nevertheless show that call beside the deterministic
        evidence agent's calls, so callbacks use this explicit public bridge.
        """
        entry = {
            "transport": transport,
            "tool": tool,
            "arguments": arguments,
            "duration_ms": round(duration_ms, 2),
            "ok": ok,
            "result_bytes": result_size,
        }
        self.call_log.append(entry)
        self.telemetry.record_mcp_call(tool, arguments, duration_ms, ok, result_size)


# ---------------------------------------------------------------------------
# Remote transports (require the mcp SDK and a real Grafana)
# ---------------------------------------------------------------------------


class RemoteGrafanaMCP(GrafanaMCPClient):
    """Talks to the official grafana/mcp-grafana server via the MCP Python SDK."""

    adapt = True

    def __init__(self, transport: str, telemetry: TelemetryPlane | None = None) -> None:
        super().__init__(telemetry)
        self.transport = transport
        self._session = None
        self._exit_stack = None

    async def connect(self) -> None:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise MCPUnavailable(
                "the 'mcp' package is required for stdio/http transports: pip install -e '.[cloud]'"
            ) from exc

        settings = get_settings()
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        if self.transport == "stdio":
            env = dict(os.environ)
            env.setdefault("GRAFANA_URL", settings.grafana_url)
            env.setdefault("GRAFANA_SERVICE_ACCOUNT_TOKEN", settings.grafana_service_account_token)
            params = StdioServerParameters(
                command=settings.mcp_stdio_command,
                args=settings.mcp_stdio_argv,
                env=env,
            )
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        else:
            import mcp.client.streamable_http as sh

            headers = {}
            if settings.mcp_grafana_url:
                headers["X-Grafana-URL"] = settings.mcp_grafana_url
            if settings.mcp_bearer_token:
                headers["Authorization"] = f"Bearer {settings.mcp_bearer_token}"
            # The MCP Python SDK renamed this factory and changed how headers
            # are supplied between 1.x and 2.x, and both are in the wild. The
            # tool surface we consume is identical either way, so adapt rather
            # than pin a version and make somebody's working install wrong.
            if hasattr(sh, "streamablehttp_client"):  # SDK 1.x
                ctx = sh.streamablehttp_client(settings.mcp_http_url, headers=headers)
            else:  # SDK 2.x
                http_client = await self._exit_stack.enter_async_context(
                    sh.create_mcp_http_client(headers=headers)
                )
                ctx = sh.streamable_http_client(settings.mcp_http_url, http_client=http_client)
            # 1.x yields (read, write, get_session_id); 2.x yields (read, write).
            streams = await self._exit_stack.enter_async_context(ctx)
            read, write = streams[0], streams[1]

        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        await super().connect()

    async def aclose(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def list_tools(self) -> list[ToolInfo]:
        if self._session is None:
            raise MCPUnavailable("not connected")
        result = await self._session.list_tools()
        return [
            ToolInfo(t.name, t.description or "", _field(t, "inputSchema", "input_schema") or {})
            for t in result.tools
        ]

    async def _invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise MCPUnavailable("not connected")
        result = await self._session.call_tool(tool, arguments)
        if _field(result, "isError", "is_error"):
            raise MCPCallError(str(result.content))
        payload = []
        for item in result.content:
            payload.append(getattr(item, "text", None) or getattr(item, "data", None))
        return payload[0] if len(payload) == 1 else payload


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_client(
    telemetry: TelemetryPlane,
    sim=None,
    transport: str | None = None,
) -> GrafanaMCPClient:
    from .stub import StubGrafanaMCP

    transport = transport or get_settings().mcp_transport
    if transport == "stub":
        return StubGrafanaMCP(telemetry, sim)
    return RemoteGrafanaMCP(transport, telemetry)
