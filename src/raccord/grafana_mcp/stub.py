"""In-process Grafana MCP server.

Implements the same tool names, argument shapes and response shapes as the
official ``grafana/mcp-grafana`` server, backed by the local telemetry plane
(Prometheus-shaped metric store, Loki-shaped log store, Tempo-shaped trace
store, Pyroscope-shaped profiles, and the Grafana annotation/incident mirror).

Its purpose is not to avoid Grafana - the same agent code runs unchanged against
a real server over stdio or the hosted Cloud endpoint. Its purpose is that the
project can be cloned, run, benchmarked over a thousand scenarios and reviewed by
a judge with no account, no token and no network, and still exercise the exact
MCP call chain.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..contracts import utcnow
from ..telemetry import TelemetryPlane
from .client import GrafanaMCPClient, MCPCallError, ToolInfo

DATASOURCES = [
    {"uid": "raccord-prom", "name": "Raccord Metrics", "type": "prometheus", "isDefault": True},
    {"uid": "raccord-loki", "name": "Raccord Logs", "type": "loki"},
    {"uid": "raccord-tempo", "name": "Raccord Traces", "type": "tempo"},
    {"uid": "raccord-pyro", "name": "Raccord Profiles", "type": "grafana-pyroscope-datasource"},
]


def _build_alert_rules() -> list[dict]:
    """Mirror the rules provisioned into Grafana, from the same SLO catalogue.

    `tools/generate_grafana_assets.py` writes these rules into
    `observability/grafana/provisioning/alerting/`. The stub derives its view
    from the identical source so that a query answered here and a query answered
    by a real Grafana return rules with the same uid, labels and objective.
    """
    from ..contracts import SLOTier
    from ..slo import ALL_SLOS, Comparator

    tier = SLOTier.TIER_0_GLOBAL_LIVE
    rules = []
    for s in ALL_SLOS:
        thr = s.threshold(tier)
        grouping = "by (language, territory, platform, player_version)"
        op = ">" if s.comparator is Comparator.LOWER_IS_BETTER else "<"
        agg = "max" if s.comparator is Comparator.LOWER_IS_BETTER else "min"
        rules.append(
            {
                "uid": f"raccord-{s.slo_id.replace('.', '-')}",
                "title": f"{s.name} outside objective",
                "folderUID": "raccord",
                "ruleGroup": "accessibility-slo",
                "condition": "C",
                "for": "30s" if s.hard_gate else "2m",
                "labels": {
                    "severity": "sev1" if s.hard_gate else "sev2",
                    "feature": s.feature.value,
                    "slo": s.slo_id,
                    "team": "accessibility-operations",
                },
                "annotations": {
                    "summary": f"{s.name}: {s.description}",
                    "runbook_url": f"https://runbooks.raccord.local/{s.slo_id}",
                    "slo_objective": str(thr),
                    "unit": s.unit,
                },
                "query": f"{agg} {grouping} ({s.sli_metric}) {op} {thr}",
            }
        )
    return rules


ALERT_RULES = _build_alert_rules()


def _parse_time(value: Any, default: datetime) -> datetime:
    if value in (None, "", "now"):
        return default
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.startswith("now-"):
        amount = s[4:]
        unit = amount[-1]
        n = float(amount[:-1])
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return default - delta
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return default


def _parse_selector(expr: str) -> tuple[str, dict[str, str]]:
    """Minimal PromQL/LogQL selector parser: name{label="value", ...}."""
    expr = expr.strip()
    name = expr
    labels: dict[str, str] = {}
    if "{" in expr:
        name, rest = expr.split("{", 1)
        rest = rest.rsplit("}", 1)[0]
        for part in rest.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            labels[k.strip().strip('"')] = v.strip().strip('"')
    return name.strip(), labels


class StubGrafanaMCP(GrafanaMCPClient):
    transport = "stub"

    def __init__(self, telemetry: TelemetryPlane, sim=None) -> None:
        super().__init__(telemetry)
        self.sim = sim
        self._handlers = {
            "list_datasources": self._list_datasources,
            "get_datasource_by_uid": self._get_datasource,
            "list_alert_rules": self._list_alert_rules,
            "get_alert_rule_by_uid": self._get_alert_rule,
            "query_prometheus": self._query_prometheus,
            "list_prometheus_metric_names": self._list_metric_names,
            "list_prometheus_label_names": self._list_metric_label_names,
            "list_prometheus_label_values": self._list_metric_label_values,
            "query_loki_logs": self._query_loki,
            "query_loki_stats": self._loki_stats,
            "list_loki_label_names": self._loki_label_names,
            "list_loki_label_values": self._loki_label_values,
            "query_tempo_traces": self._query_traces,
            "get_trace_by_id": self._get_trace,
            "list_pyroscope_profile_types": self._profile_types,
            "fetch_pyroscope_profile": self._fetch_profile,
            "search_dashboards": self._search_dashboards,
            "get_dashboard_by_uid": self._get_dashboard,
            "generate_deeplink": self._generate_deeplink,
            "find_annotations": self._find_annotations,
            "create_annotation": self._create_annotation,
            "list_incidents": self._list_incidents,
            "create_incident": self._create_incident,
            "get_incident": self._get_incident,
            "add_activity_to_incident": self._add_activity,
            "list_teams": self._list_teams,
            "list_contact_points": self._list_contact_points,
        }

    # -- transport ---------------------------------------------------------
    async def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(name, (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "")
            for name, fn in sorted(self._handlers.items())
        ]

    async def _invoke(self, tool: str, arguments: dict[str, Any]) -> Any:
        handler = self._handlers.get(tool)
        if handler is None:
            raise MCPCallError(f"unknown tool: {tool}")
        return handler(**arguments)

    # -- datasources -------------------------------------------------------
    def _list_datasources(self, **_: Any) -> list[dict]:
        """List the configured Grafana datasources."""
        return DATASOURCES

    def _get_datasource(self, uid: str, **_: Any) -> dict:
        """Fetch one datasource by uid."""
        for d in DATASOURCES:
            if d["uid"] == uid:
                return d
        raise MCPCallError(f"datasource {uid} not found")

    # -- alerting ----------------------------------------------------------
    def _list_alert_rules(
        self, label_selectors: dict | None = None, limit: int = 100, **_: Any
    ) -> list[dict]:
        """List Grafana alert rules and their current state."""
        firing = {a.rule_uid: a for a in getattr(self, "_firing", [])}
        out = []
        for rule in ALERT_RULES:
            if label_selectors and any(
                rule["labels"].get(k) != v for k, v in label_selectors.items()
            ):
                continue
            alert = firing.get(rule["uid"])
            out.append(
                {
                    **rule,
                    "state": "firing" if alert else "normal",
                    "activeAt": alert.fired_at.isoformat() if alert else None,
                    "value": alert.value if alert else None,
                    "alertLabels": dict(alert.labels) if alert else {},
                    "alertAnnotations": dict(alert.annotations) if alert else {},
                }
            )
        return out[:limit]

    def _get_alert_rule(self, uid: str, **_: Any) -> dict:
        """Fetch one alert rule definition by uid."""
        for rule in self._list_alert_rules():
            if rule["uid"] == uid:
                return rule
        raise MCPCallError(f"alert rule {uid} not found")

    def set_firing(self, alerts: list) -> None:
        """Publish the currently firing alerts into the server's view."""
        self._firing = list(alerts)

    # -- metrics -----------------------------------------------------------
    def _query_prometheus(
        self,
        expr: str,
        start: Any = None,
        end: Any = None,
        queryType: str = "range",
        aggregation: str = "last",
        **_: Any,
    ) -> dict:
        """Run a PromQL selector against the metrics datasource."""
        now = utcnow()
        t_end = _parse_time(end, now)
        t_start = _parse_time(start, t_end - timedelta(minutes=15))
        metric, matchers = _parse_selector(expr)
        results = self.telemetry.metrics.query(metric, matchers, t_start, t_end, aggregation)
        return {
            "status": "success",
            "queryType": queryType,
            "expr": expr,
            "range": {"start": t_start.isoformat(), "end": t_end.isoformat()},
            "resultCount": len(results),
            "result": results[:40],
        }

    def _list_metric_names(self, **_: Any) -> list[str]:
        """List available Prometheus metric names."""
        return self.telemetry.metrics.series_names()

    def _list_metric_label_names(self, **_: Any) -> list[str]:
        """List label names present on the metrics."""
        return self.telemetry.metrics.label_names()

    def _list_metric_label_values(self, label: str, **_: Any) -> list[str]:
        """List values for one metric label."""
        return self.telemetry.metrics.label_values(label)

    # -- logs --------------------------------------------------------------
    def _query_loki(
        self, expr: str, start: Any = None, end: Any = None, limit: int = 50, **_: Any
    ) -> dict:
        """Run a LogQL query against the logs datasource."""
        now = utcnow()
        t_end = _parse_time(end, now)
        t_start = _parse_time(start, t_end - timedelta(minutes=30))
        contains = None
        selector_part = expr
        if "|=" in expr:
            selector_part, filter_part = expr.split("|=", 1)
            contains = filter_part.strip().strip('"').strip("`")
        _, labels = _parse_selector(selector_part.strip())
        lines = self.telemetry.logs.query(labels, contains, t_start, t_end, limit)
        return {
            "status": "success",
            "expr": expr,
            "range": {"start": t_start.isoformat(), "end": t_end.isoformat()},
            "resultCount": len(lines),
            "result": [
                {"timestamp": entry.ts.isoformat(), "labels": entry.labels, "line": entry.line}
                for entry in lines
            ],
        }

    def _loki_stats(self, expr: str, **_: Any) -> dict:
        """Return stream/entry/byte counts for a LogQL selector."""
        _, labels = _parse_selector(expr)
        return self.telemetry.logs.stats(labels)

    def _loki_label_names(self, **_: Any) -> list[str]:
        """List log label names."""
        return self.telemetry.logs.label_names()

    def _loki_label_values(self, label: str, **_: Any) -> list[str]:
        """List values for one log label."""
        return self.telemetry.logs.label_values(label)

    # -- traces ------------------------------------------------------------
    def _query_traces(
        self,
        service: str | None = None,
        query: str | None = None,
        minDuration: float | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = 20,
        **_: Any,
    ) -> dict:
        """Search Tempo for traces matching a service and TraceQL-style filter."""
        now = utcnow()
        t_end = _parse_time(end, now)
        t_start = _parse_time(start, t_end - timedelta(minutes=30))
        attributes: dict[str, str] = {}
        name_contains = None
        if query:
            _, attributes = _parse_selector(query)
            if "{" not in query:
                name_contains = query
        spans = self.telemetry.traces.search(
            service, name_contains, attributes or None, minDuration, t_start, t_end, limit
        )
        return {
            "status": "success",
            "resultCount": len(spans),
            "traces": [
                {
                    "traceID": s.trace_id,
                    "spanID": s.span_id,
                    "parentSpanID": s.parent_id,
                    "rootServiceName": s.service,
                    "name": s.name,
                    "startTime": s.start.isoformat(),
                    "durationMs": round(s.duration_ms, 2),
                    "attributes": s.attributes,
                    "status": s.status,
                }
                for s in spans
            ],
        }

    def _get_trace(self, traceID: str, **_: Any) -> dict:
        """Fetch every span in one trace."""
        spans = self.telemetry.traces.trace(traceID)
        if not spans:
            raise MCPCallError(f"trace {traceID} not found")
        return {
            "traceID": traceID,
            "spanCount": len(spans),
            "spans": [
                {
                    "spanID": s.span_id,
                    "parentSpanID": s.parent_id,
                    "name": s.name,
                    "service": s.service,
                    "durationMs": round(s.duration_ms, 2),
                    "attributes": s.attributes,
                    "status": s.status,
                }
                for s in sorted(spans, key=lambda s: s.start)
            ],
        }

    # -- profiles ----------------------------------------------------------
    def _profile_types(self, **_: Any) -> list[str]:
        """List available Pyroscope profile types."""
        return ["process_cpu:cpu:nanoseconds", "memory:alloc_space:bytes"]

    def _fetch_profile(
        self, service: str, profileType: str = "process_cpu:cpu:nanoseconds", **_: Any
    ) -> dict:
        """Fetch a flame-graph summary for one service."""
        return {
            "service": service,
            "profileType": profileType,
            "top": self.telemetry.profiles.fetch(service),
        }

    # -- dashboards --------------------------------------------------------
    def _search_dashboards(
        self, query: str = "", tag: str | list | None = None, limit: int = 20, **_: Any
    ) -> list[dict]:
        """Search dashboards by title and tag."""
        tags = [tag] if isinstance(tag, str) else (tag or [])
        out = []
        for d in self.telemetry.grafana.dashboards:
            if query and query.lower() not in d["title"].lower():
                continue
            if tags and not set(tags) & set(d["tags"]):
                continue
            out.append(d)
        return out[:limit]

    def _get_dashboard(self, uid: str, **_: Any) -> dict:
        """Fetch a dashboard by uid."""
        for d in self.telemetry.grafana.dashboards:
            if d["uid"] == uid:
                return d
        raise MCPCallError(f"dashboard {uid} not found")

    def _generate_deeplink(
        self,
        resourceType: str = "dashboard",
        dashboardUid: str = "",
        timeRange: dict | None = None,
        queryParams: dict | None = None,
        **_: Any,
    ) -> dict:
        """Build a Grafana URL a human can open to review the same evidence."""
        from ..config import get_settings

        base = get_settings().grafana_url.rstrip("/")
        dash = None
        for d in self.telemetry.grafana.dashboards:
            if d["uid"] == dashboardUid:
                dash = d
                break
        path = dash["url"] if dash else f"/d/{dashboardUid}"
        params = dict(queryParams or {})
        if timeRange:
            params.setdefault("from", timeRange.get("from", "now-30m"))
            params.setdefault("to", timeRange.get("to", "now"))
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{base}{path}" + (f"?{query}" if query else "")
        return {"url": url, "resourceType": resourceType}

    # -- annotations -------------------------------------------------------
    def _find_annotations(
        self,
        tags: list | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = 50,
        **_: Any,
    ) -> list[dict]:
        """Find annotations (deployments, configuration changes, approvals, recovery)."""
        now = utcnow()
        t_end = _parse_time(end, now)
        t_start = _parse_time(start, t_end - timedelta(hours=2))
        found = self.telemetry.grafana.find_annotations(tags, t_start, t_end, limit)
        return [
            {
                "id": a.annotation_id,
                "time": a.time.isoformat(),
                "timeEnd": a.time_end.isoformat() if a.time_end else None,
                "text": a.text,
                "tags": a.tags,
                "dashboardUID": a.dashboard_uid,
            }
            for a in found
        ]

    def _create_annotation(
        self,
        text: str,
        tags: list | None = None,
        dashboardUID: str | None = None,
        panelId: int | None = None,
        time: Any = None,
        timeEnd: Any = None,
        **_: Any,
    ) -> dict:
        """Write an annotation onto the Grafana timeline."""
        a = self.telemetry.grafana.add_annotation(
            text,
            list(tags or []),
            dashboardUID,
            panelId,
            _parse_time(time, utcnow()),
            _parse_time(timeEnd, None) if timeEnd else None,
        )
        return {
            "id": a.annotation_id,
            "message": "Annotation added",
            "text": a.text,
            "tags": a.tags,
        }

    # -- incidents ---------------------------------------------------------
    def _list_incidents(self, status: str = "active", limit: int = 20, **_: Any) -> list[dict]:
        """List Grafana incidents."""
        return [
            {
                "incidentID": i.incident_id,
                "title": i.title,
                "severity": i.severity,
                "status": i.status,
                "createdTime": i.created_at.isoformat(),
                "labels": i.labels,
                "activityCount": len(i.activity),
            }
            for i in self.telemetry.grafana.incidents.values()
            if status in ("", i.status)
        ][:limit]

    def _create_incident(
        self, title: str, severity: str = "minor", labels: dict | None = None, **_: Any
    ) -> dict:
        """Declare a Grafana incident."""
        inc = self.telemetry.grafana.create_incident(title, severity, labels)
        return {
            "incidentID": inc.incident_id,
            "title": inc.title,
            "severity": inc.severity,
            "status": inc.status,
        }

    def _get_incident(self, incidentID: str, **_: Any) -> dict:
        """Fetch one Grafana incident and its timeline."""
        inc = self.telemetry.grafana.incidents.get(incidentID)
        if inc is None:
            raise MCPCallError(f"incident {incidentID} not found")
        return {
            "incidentID": inc.incident_id,
            "title": inc.title,
            "severity": inc.severity,
            "status": inc.status,
            "labels": inc.labels,
            "activity": inc.activity,
        }

    def _add_activity(self, incidentID: str, body: str, **_: Any) -> dict:
        """Append an entry to a Grafana incident timeline."""
        return self.telemetry.grafana.add_activity(incidentID, body)

    # -- org ---------------------------------------------------------------
    def _list_teams(self, **_: Any) -> list[dict]:
        """List Grafana teams."""
        return [
            {"id": 1, "name": "accessibility-operations"},
            {"id": 2, "name": "streaming-sre"},
            {"id": 3, "name": "broadcast-operations"},
        ]

    def _list_contact_points(self, **_: Any) -> list[dict]:
        """List alerting contact points."""
        return [
            {"uid": "cp-a11y", "name": "accessibility-ops", "type": "webhook"},
            {"uid": "cp-td", "name": "technical-director", "type": "webhook"},
        ]
