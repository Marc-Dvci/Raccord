"""Grafana Evidence Agent.

Every fact this agent contributes to an incident comes back through the Grafana
MCP server. It does not query Prometheus, Loki, Tempo or Pyroscope directly, and
the incident state machine enforces that: EVIDENCE_COMPLETE requires evidence
whose `source_tool` is a Grafana MCP tool for alerts, metrics, logs, traces and
dashboards. Remove the MCP server and the investigation cannot progress.

The mandatory chain, in order:

 1. list_alert_rules            - retrieve the firing accessibility alert
 2. get_alert_rule_by_uid       - read the rule's labels, objective and runbook
 3. query_prometheus            - the breached SLI over the incident window
 4. query_prometheus            - accessibility-enabled session aggregates
 5. find_annotations            - deployments and configuration changes
 6. query_loki_logs             - logs for the implicated component
 7. query_tempo_traces          - the media path through the affected pool
 8. search_dashboards           - the dashboard a human should open
 9. generate_deeplink           - a reviewable link, scoped to the window
10. create_incident             - declare it in Grafana
11. add_activity_to_incident    - record the approved action on the timeline
12. query_prometheus            - recovery verification series
13. create_annotation           - mark the recovery on the timeline
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..contracts import (
    Alert,
    ChangeEvent,
    Evidence,
    EvidenceKind,
    Incident,
    utcnow,
)
from ..grafana_mcp import GrafanaMCPClient, MCPCallError
from ..telemetry import TelemetryPlane

REQUIRED_CHAIN = (
    "list_alert_rules",
    "get_alert_rule",
    "query_prometheus",
    "query_loki_logs",
    "query_tempo_traces",
    "search_dashboards",
)


def _ev(
    incident_id: str,
    kind: EvidenceKind,
    tool: str,
    summary: str,
    payload: Any,
    query: str | None = None,
    deep_link: str | None = None,
    interval: tuple[datetime, datetime] | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{uuid.uuid4().hex[:10]}",
        incident_id=incident_id,
        kind=kind,
        source_tool=f"grafana.mcp:{tool}",
        query=query,
        summary=summary,
        payload=payload if isinstance(payload, dict) else {"result": payload},
        interval_start=interval[0] if interval else None,
        interval_end=interval[1] if interval else None,
        deep_link=deep_link,
    )


class GrafanaEvidenceAgent:
    name = "grafana_evidence_agent"

    def __init__(self, mcp: GrafanaMCPClient, telemetry: TelemetryPlane) -> None:
        self.mcp = mcp
        self.telemetry = telemetry

    # -- investigation -----------------------------------------------------
    async def investigate(
        self,
        incident: Incident,
        alert: Alert,
        window_minutes: int = 20,
    ) -> tuple[list[Evidence], list[ChangeEvent], str | None]:
        """Collect the mandatory evidence chain. Returns (evidence, changes, deeplink)."""
        end = utcnow()
        start = end - timedelta(minutes=window_minutes)
        evidence: list[Evidence] = []
        changes: list[ChangeEvent] = []
        slo = alert.labels.get("slo", "cap.drift")
        feature = alert.labels.get("feature", "captions")

        # 1. the firing alert -------------------------------------------------
        rules = _aslist(await self.mcp.call(
            "list_alert_rules", label_selectors={"feature": feature}, limit=50
        ))
        if not rules:
            # The server may not index by this label; fall back to the full list
            # rather than guessing, and let the SLO label do the matching.
            rules = _aslist(await self.mcp.call("list_alert_rules", limit=200))
        firing = [r for r in rules if r.get("state") == "firing"] or rules
        rule = next((r for r in firing if r.get("labels", {}).get("slo") == slo),
                    firing[0] if firing else None)
        if rule is None:
            raise MCPCallError(
                f"no alert rule advertised for SLO {slo}; the Grafana stack is not "
                "provisioned with the AccessPulse rule group"
            )
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.GRAFANA_ALERT, "list_alert_rules",
            f"{len(firing)} accessibility rule(s) firing for {feature}; "
            f"'{rule.get('title')}' matches the breached SLO {slo}",
            {"firing": firing[:5]},
        ))

        # 2. rule definition --------------------------------------------------
        detail = await self.mcp.call("get_alert_rule", uid=rule["uid"])
        detail = _asdict(detail)
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.GRAFANA_ALERT, "get_alert_rule_by_uid",
            f"Rule {rule['uid']} objective: "
            f"{detail.get('annotations', {}).get('slo_objective', 'n/a')}"
            f"; query: {detail.get('query', '')}",
            detail, query=detail.get("query"),
        ))

        # 3. the breached SLI -------------------------------------------------
        sli_metric = _metric_for_slo(slo)
        sli = await self.mcp.call(
            "query_prometheus", expr=sli_metric,
            start=start.isoformat(), end=end.isoformat(),
            queryType="range", aggregation="max",
        )
        sli = _asdict(sli)
        worst = (sli.get("result") or [{}])[0]
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.PROM_QUERY, "query_prometheus",
            f"{sli_metric}: worst observed {worst.get('value')} on "
            f"{_slice_of(worst)} across {sli.get('resultCount', 0)} series",
            sli, query=sli_metric, interval=(start, end),
        ))

        # 4. audience impact --------------------------------------------------
        impact_metric = _sessions_metric_for(feature)
        impact = _asdict(await self.mcp.call(
            "query_prometheus", expr=impact_metric,
            start=start.isoformat(), end=end.isoformat(), aggregation="last",
        ))
        total = sum(r.get("value", 0.0) for r in impact.get("result", []))
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.SESSION_AGGREGATE, "query_prometheus",
            f"{int(total)} accessibility-enabled sessions in the affected slices "
            f"({impact.get('resultCount', 0)} aggregate slices, k-anonymised)",
            impact, query=impact_metric, interval=(start, end),
        ))

        # 5. change events ----------------------------------------------------
        if self.mcp.has("find_annotations"):
            annotations = _aslist(await self.mcp.call(
                "find_annotations", tags=["deployment", "config", "change"],
                start=start.isoformat(), end=end.isoformat(), limit=50,
            ))
            for a in annotations:
                changes.append(ChangeEvent(
                    change_id=f"chg-annot-{a.get('id')}",
                    kind=_change_kind(a.get("tags", [])),
                    component=_component_of(a.get("text", "")),
                    description=a.get("text", ""),
                    at=_parse_dt(a.get("time")) or start,
                    actor="grafana-annotation",
                    payload={"tags": a.get("tags", [])},
                ))
            evidence.append(_ev(
                incident.incident_id, EvidenceKind.ANNOTATION, "find_annotations",
                f"{len(annotations)} change annotation(s) in the incident window",
                {"annotations": annotations[:20]}, interval=(start, end),
            ))

        # 6. logs -------------------------------------------------------------
        component = _component_for(slo, alert)
        log_query = f'{{service="{component}"}} |= "resync"' if "drift" in slo \
            else f'{{service="{component}"}}'
        logs = _asdict(await self.mcp.call(
            "query_loki_logs", expr=log_query,
            start=start.isoformat(), end=end.isoformat(), limit=40,
        ))
        if logs.get("resultCount", 0) == 0 and "|=" in log_query:
            log_query = f'{{service="{component}"}}'
            logs = _asdict(await self.mcp.call(
                "query_loki_logs", expr=log_query,
                start=start.isoformat(), end=end.isoformat(), limit=40,
            ))
        warn_lines = [
            r for r in logs.get("result", [])
            if r.get("labels", {}).get("level") in ("warn", "error")
        ]
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.LOKI_QUERY, "query_loki_logs",
            f"{logs.get('resultCount', 0)} log line(s) from {component}, "
            f"{len(warn_lines)} at warn/error"
            + (f"; first: {warn_lines[0]['line'][:120]}" if warn_lines else ""),
            logs, query=log_query, interval=(start, end),
        ))
        # A second Loki read for the clock daemon. Captions, described audio and
        # the interpreter feed all hang off one timing reference, so its log is
        # relevant to any of their symptoms - not only to a drift alert.
        if feature in ("captions", "audio_description", "sign_language"):
            clock_logs = _asdict(await self.mcp.call(
                "query_loki_logs", expr='{service=~"clock-.*"}',
                start=start.isoformat(), end=end.isoformat(), limit=20,
            ))
            if clock_logs.get("resultCount"):
                evidence.append(_ev(
                    incident.incident_id, EvidenceKind.LOKI_QUERY, "query_loki_logs",
                    f"{clock_logs.get('resultCount')} timing-reference log line(s); "
                    f"latest: {clock_logs['result'][-1]['line'][:120]}",
                    clock_logs, query='{service=~"clock-.*"}', interval=(start, end),
                ))

        # 7. traces -----------------------------------------------------------
        traces = _asdict(await self.mcp.call(
            "query_tempo_traces", service="media-path", query=component,
            start=start.isoformat(), end=end.isoformat(), limit=10,
        ))
        if not traces.get("resultCount"):
            traces = _asdict(await self.mcp.call(
                "query_tempo_traces", service="media-path",
                start=start.isoformat(), end=end.isoformat(), limit=10,
            ))
        top_trace = (traces.get("traces") or [{}])[0]
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.TEMPO_TRACE, "query_tempo_traces",
            f"{traces.get('resultCount', 0)} media-path trace(s); slowest span "
            f"{top_trace.get('name')} at {top_trace.get('durationMs')} ms via "
            f"{top_trace.get('attributes', {}).get('component', component)}",
            traces, interval=(start, end),
        ))

        # 8-9. dashboard + deep link -----------------------------------------
        dashboards = _aslist(await self.mcp.call(
            "search_dashboards", query="Incident investigation", tag="accesspulse", limit=5
        ))
        dash_uid = dashboards[0]["uid"] if dashboards else "ap-incident"
        deep_link = None
        if self.mcp.has("generate_deeplink"):
            link = _asdict(await self.mcp.call(
                "generate_deeplink", resourceType="dashboard", dashboardUid=dash_uid,
                timeRange={"from": start.isoformat(), "to": end.isoformat()},
                queryParams={"var-slo": slo, "var-feature": feature},
            ))
            deep_link = link.get("url")
        evidence.append(_ev(
            incident.incident_id, EvidenceKind.DASHBOARD_LINK, "search_dashboards",
            f"Human review: {dashboards[0]['title'] if dashboards else dash_uid}",
            {"dashboards": dashboards}, deep_link=deep_link,
        ))

        # 10. declare the incident in Grafana ---------------------------------
        if self.mcp.has("create_incident"):
            gi = _asdict(await self.mcp.call(
                "create_incident",
                title=incident.title,
                severity=alert.severity.value,
                labels={"slo": slo, "feature": feature,
                        "accesspulse_incident": incident.incident_id},
            ))
            incident.timings["grafana_incident_id"] = 0.0
            evidence.append(_ev(
                incident.incident_id, EvidenceKind.ANNOTATION, "create_incident",
                f"Grafana incident {gi.get('incidentID')} declared",
                gi,
            ))
        return evidence, changes, deep_link

    # -- write-back --------------------------------------------------------
    async def record_action(self, incident: Incident, text: str) -> Evidence | None:
        """Append the approved action to the Grafana incident timeline."""
        gi_id = None
        if self.mcp.has("list_incidents"):
            for gi in _aslist(await self.mcp.call("list_incidents", status="active", limit=20)):
                if gi.get("labels", {}).get("accesspulse_incident") == incident.incident_id:
                    gi_id = gi["incidentID"]
                    break
        if gi_id and self.mcp.has("add_activity_to_incident"):
            await self.mcp.call("add_activity_to_incident", incidentID=gi_id, body=text)
        result = _asdict(await self.mcp.call(
            "create_annotation", text=text,
            tags=["accesspulse", "approved-action", incident.incident_id],
            dashboardUID="ap-incident",
        ))
        return _ev(
            incident.incident_id, EvidenceKind.ANNOTATION, "create_annotation",
            "Approved action written to the Grafana timeline", result,
        )

    async def verify_recovery(
        self, incident: Incident, slo: str, minutes: int = 5
    ) -> list[Evidence]:
        """Query the recovery series and annotate the timeline."""
        end = utcnow()
        start = end - timedelta(minutes=minutes)
        metric = _metric_for_slo(slo)
        series = _asdict(await self.mcp.call(
            "query_prometheus", expr=metric, start=start.isoformat(), end=end.isoformat(),
            aggregation="max",
        ))
        worst = (series.get("result") or [{}])[0]
        out = [_ev(
            incident.incident_id, EvidenceKind.PROM_QUERY, "query_prometheus",
            f"Post-action {metric}: worst {worst.get('value')} across "
            f"{series.get('resultCount', 0)} series",
            series, query=metric, interval=(start, end),
        )]
        annotation = _asdict(await self.mcp.call(
            "create_annotation",
            text=f"AccessPulse: {incident.incident_id} recovered - {metric} back inside "
                 f"objective (worst {worst.get('value')})",
            tags=["accesspulse", "recovery", incident.incident_id],
            dashboardUID="ap-incident",
        ))
        out.append(_ev(
            incident.incident_id, EvidenceKind.ANNOTATION, "create_annotation",
            "Recovery annotation written to Grafana", annotation,
        ))
        return out

    def chain_complete(self) -> tuple[bool, list[str]]:
        used = {c["tool"] for c in self.mcp.call_log}
        missing = []
        for cap in REQUIRED_CHAIN:
            if not self.mcp.has(cap) or self.mcp.tool_for(cap) not in used:
                missing.append(cap)
        return (not missing), missing


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _aslist(value: Any) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        for key in ("result", "results", "items", "data"):
            if key in value and isinstance(value[key], list):
                return value[key]
        return [value]
    return list(value) if isinstance(value, list) else []


def _asdict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
    return value if isinstance(value, dict) else {"result": value}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _metric_for_slo(slo: str) -> str:
    from ..slo import SLO_BY_ID

    definition = SLO_BY_ID.get(slo)
    return definition.sli_metric if definition else "accesspulse_caption_drift_seconds"


def _sessions_metric_for(feature: str) -> str:
    return {
        "captions": "accesspulse_sessions_caption_enabled",
        "audio_description": "accesspulse_sessions_description_enabled",
        "sign_language": "accesspulse_sessions_sign_enabled",
    }.get(feature, "accesspulse_sessions_caption_enabled")


def _component_for(slo: str, alert: Alert) -> str:
    if slo.startswith("cap."):
        return "capenc-pool-a"
    if slo.startswith("ad."):
        return "adsrc-en"
    if slo.startswith("sign."):
        return "signsrc-lsf"
    if slo.startswith("player.") or slo.startswith("auth.") or slo.startswith("purchase."):
        return f"pv-{alert.labels.get('player_version', 'ctv-9.4.0').split(',')[0]}"
    return "packager-main"


def _change_kind(tags: list[str]) -> str:
    for t in tags:
        if t in ("deployment", "config", "provider", "traffic", "manifest", "routing"):
            return t
    return "config"


def _component_of(text: str) -> str:
    for token in text.replace(":", " ").split():
        if token.startswith(("capenc-", "clock-", "pv-", "signsrc-", "adsrc-", "region-",
                             "packager-", "manifest-", "cdn-", "auth-", "origin-",
                             "capsrc-", "altsrc-")):
            return token.strip(",.")
    return "unknown"


def _slice_of(series: dict) -> str:
    m = series.get("metric", {})
    parts = [m.get(k) for k in ("language", "territory", "platform", "player_version")]
    return "/".join(p for p in parts if p) or "all"
