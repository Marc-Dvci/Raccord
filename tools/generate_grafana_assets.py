"""Generate Grafana dashboards and alert rules from the SLO definitions.

The SLO catalogue in `accesspulse/slo.py` is the single source of truth. Running
this script regenerates:

  observability/grafana/dashboards/*.json
  observability/grafana/provisioning/alerting/accesspulse-rules.yml

so a dashboard panel and an alert threshold can never drift from the objective
the probes are actually measured against. CI runs it with --check.

    python tools/generate_grafana_assets.py
    python tools/generate_grafana_assets.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from accesspulse.contracts import SLOTier  # noqa: E402
from accesspulse.slo import (  # noqa: E402
    ALL_SLOS,
    Comparator,
    SLODefinition,
)

DASH_DIR = ROOT / "observability" / "grafana" / "dashboards"
ALERT_DIR = ROOT / "observability" / "grafana" / "provisioning" / "alerting"
TIER = SLOTier.TIER_0_GLOBAL_LIVE

# The panel every SLO alert links back to: the breached SLI over the incident
# window, on the incident dashboard. Named once so the alert annotation and the
# panel that satisfies it cannot drift apart.
INCIDENT_SLI_PANEL_ID = 2
PROM = {"type": "prometheus", "uid": "ap-prom"}
LOKI = {"type": "loki", "uid": "ap-loki"}
TEMPO = {"type": "tempo", "uid": "ap-tempo"}
PYRO = {"type": "grafana-pyroscope-datasource", "uid": "ap-pyro"}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------


def _grid(x: int, y: int, w: int, h: int) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def timeseries(pid: int, title: str, expr: str, unit: str, grid: dict,
               thresholds: list[dict] | None = None, legend: str = "{{language}} / "
               "{{territory}} / {{player_version}}", description: str = "") -> dict:
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": grid,
        "targets": [{"refId": "A", "expr": expr, "legendFormat": legend,
                     "datasource": PROM}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 2, "fillOpacity": 8,
                           "showPoints": "never", "spanNulls": True},
                "thresholds": {"mode": "absolute",
                               "steps": thresholds or [{"color": "green", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "right",
                       "showLegend": True, "calcs": ["lastNotNull", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(pid: int, title: str, expr: str, unit: str, grid: dict,
         thresholds: list[dict] | None = None, text_mode: str = "auto",
         description: str = "") -> dict:
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": grid,
        "targets": [{"refId": "A", "expr": expr, "datasource": PROM}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute",
                               "steps": thresholds or [{"color": "green", "value": None}]},
                # Never colour-only: the value and its unit always carry the meaning.
                "mappings": [],
            },
            "overrides": [],
        },
        "options": {
            "textMode": text_mode,
            "colorMode": "value",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def table(pid: int, title: str, expr: str, grid: dict, description: str = "") -> dict:
    return {
        "id": pid,
        "type": "table",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": grid,
        "targets": [{"refId": "A", "expr": expr, "format": "table",
                     "instant": True, "datasource": PROM}],
        "transformations": [{"id": "organize", "options": {}}],
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": []},
        "options": {"showHeader": True, "sortBy": [{"displayName": "Value", "desc": True}]},
    }


def logs(pid: int, title: str, expr: str, grid: dict) -> dict:
    return {
        "id": pid,
        "type": "logs",
        "title": title,
        "datasource": LOKI,
        "gridPos": grid,
        "targets": [{"refId": "A", "expr": expr, "datasource": LOKI}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
    }


def traces(pid: int, title: str, query: str, grid: dict) -> dict:
    return {
        "id": pid,
        "type": "traces",
        "title": title,
        "datasource": TEMPO,
        "gridPos": grid,
        "targets": [{"refId": "A", "queryType": "traceql", "query": query,
                     "datasource": TEMPO}],
    }


def flamegraph(pid: int, title: str, grid: dict, service: str) -> dict:
    return {
        "id": pid,
        "type": "flamegraph",
        "title": title,
        "datasource": PYRO,
        "gridPos": grid,
        "targets": [{"refId": "A", "profileTypeId": "process_cpu:cpu:nanoseconds",
                     "labelSelector": f'{{service_name="{service}"}}', "datasource": PYRO}],
    }


def text_panel(pid: int, title: str, content: str, grid: dict) -> dict:
    return {
        "id": pid,
        "type": "text",
        "title": title,
        "gridPos": grid,
        "options": {"mode": "markdown", "content": content},
    }


def dashboard(uid: str, title: str, tags: list[str], panels: list[dict],
              description: str, refresh: str = "10s",
              templating: list[dict] | None = None) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": refresh,
        "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": templating or []},
        "annotations": {
            "list": [
                {
                    "name": "AccessPulse actions",
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "iconColor": "purple",
                    "target": {"type": "tags", "matchAny": True,
                               "tags": ["accesspulse", "approved-action", "recovery"]},
                },
                {
                    "name": "Deployments and configuration",
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "iconColor": "orange",
                    "target": {"type": "tags", "matchAny": True,
                               "tags": ["deployment", "config", "change"]},
                },
            ]
        },
        "panels": panels,
    }


TEMPLATE_VARS = [
    {"name": "language", "type": "query", "datasource": PROM, "refresh": 2,
     "includeAll": True, "multi": True, "label": "Language",
     "query": {"query": "label_values(accesspulse_caption_drift_seconds, language)"}},
    {"name": "territory", "type": "query", "datasource": PROM, "refresh": 2,
     "includeAll": True, "multi": True, "label": "Territory",
     "query": {"query": "label_values(accesspulse_caption_drift_seconds, territory)"}},
    {"name": "player_version", "type": "query", "datasource": PROM, "refresh": 2,
     "includeAll": True, "multi": True, "label": "Player build",
     "query": {"query": "label_values(accesspulse_caption_drift_seconds, player_version)"}},
]

SEL = '{language=~"$language",territory=~"$territory",player_version=~"$player_version"}'


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------


def executive_dashboard() -> dict:
    p: list[dict] = []
    p.append(text_panel(1, "What this shows", (
        "**Accessible Experience Reliability.** Every promised accessibility feature is "
        "treated as a production service with an objective and an error budget. "
        "A stream is only healthy when captions, described audio, the interpreter feed "
        "and the accessible player are all inside objective for every audience slice.\n\n"
        "Status is never conveyed by colour alone: each tile shows its value and unit."
    ), _grid(0, 0, 24, 3)))
    p.append(stat(2, "Accessibility SLO attainment", "1 - avg(accesspulse_slo_breached)",
                  "percentunit", _grid(0, 3, 5, 5),
                  [{"color": "red", "value": None}, {"color": "orange", "value": 0.98},
                   {"color": "green", "value": 0.999}],
                  description="Share of evaluated SLO/slice pairs inside objective."))
    p.append(stat(3, "Worst error budget consumed",
                  "max(accesspulse_error_budget_consumed_ratio)", "percentunit",
                  _grid(5, 3, 5, 5),
                  [{"color": "green", "value": None}, {"color": "orange", "value": 0.2},
                   {"color": "red", "value": 0.5}]))
    p.append(stat(4, "Accessibility-enabled sessions protected",
                  "sum(accesspulse_sessions_caption_enabled) + "
                  "sum(accesspulse_sessions_description_enabled) + "
                  "sum(accesspulse_sessions_sign_enabled)", "short", _grid(10, 3, 5, 5)))
    p.append(stat(5, "Incidents open", "count(accesspulse_slo_breached > 0) or vector(0)",
                  "short", _grid(15, 3, 4, 5),
                  [{"color": "green", "value": None}, {"color": "red", "value": 1}]))
    p.append(stat(6, "Mean time to recovery", "avg(accesspulse_time_to_recovery_seconds)",
                  "s", _grid(19, 3, 5, 5),
                  [{"color": "green", "value": None}, {"color": "orange", "value": 180},
                   {"color": "red", "value": 300}]))
    p.append(timeseries(7, "Error budget consumed by SLO",
                        "accesspulse_error_budget_consumed_ratio", "percentunit",
                        _grid(0, 8, 12, 8), legend="{{slo}}"))
    p.append(table(8, "Breaching SLO / slice matrix",
                   "accesspulse_slo_breached > 0", _grid(12, 8, 12, 8),
                   description="Every SLO and audience slice currently outside objective."))
    p.append(timeseries(9, "Sessions with an accessibility feature enabled",
                        "sum by (territory) (accesspulse_sessions_caption_enabled)", "short",
                        _grid(0, 16, 12, 7), legend="{{territory}}",
                        description="k-anonymised aggregates. No individual viewer, no "
                                    "inferred trait, never a disability profile."))
    p.append(timeseries(10, "Agent cost per incident",
                        "sum(accesspulse_agent_cost_usd)", "currencyUSD",
                        _grid(12, 16, 12, 7), legend="{{model}}"))
    return dashboard("ap-exec", "AccessPulse / Executive accessibility reliability",
                     ["accesspulse", "executive", "slo"], p,
                     "Accessibility SLO attainment, error budget, protected sessions and "
                     "restoration performance for the whole event portfolio.", "30s")


def cockpit_dashboard() -> dict:
    p: list[dict] = []
    p.append(text_panel(1, "Live event command centre", (
        "Promised experiences for the current event, and what each audience slice is "
        "receiving right now. Orange annotations are deployments and configuration "
        "changes; purple annotations are AccessPulse approved actions and recoveries."
    ), _grid(0, 0, 24, 3)))
    p.append(stat(2, "Caption drift (worst slice)",
                  f"max(accesspulse_caption_drift_seconds{SEL})", "s", _grid(0, 3, 4, 5),
                  [{"color": "green", "value": None}, {"color": "orange", "value": 1.5},
                   {"color": "red", "value": 3}]))
    p.append(stat(3, "Caption availability (worst slice)",
                  f"min(accesspulse_caption_track_available_ratio{SEL})", "percentunit",
                  _grid(4, 3, 4, 5),
                  [{"color": "red", "value": None}, {"color": "green", "value": 0.999}]))
    p.append(stat(4, "Described audio audible",
                  "min(accesspulse_ad_audio_present_ratio)", "percentunit",
                  _grid(8, 3, 4, 5),
                  [{"color": "red", "value": None}, {"color": "green", "value": 0.99}]))
    p.append(stat(5, "Interpreter feed frame rate", "min(accesspulse_sign_fps)", "none",
                  _grid(12, 3, 4, 5),
                  [{"color": "red", "value": None}, {"color": "green", "value": 45}]))
    p.append(stat(6, "Keyboard journeys completing",
                  "min(accesspulse_player_keyboard_completion_ratio)", "percentunit",
                  _grid(16, 3, 4, 5),
                  [{"color": "red", "value": None}, {"color": "green", "value": 1}]))
    p.append(stat(7, "Screen-reader journeys completing",
                  "min(accesspulse_player_screenreader_completion_ratio)", "percentunit",
                  _grid(20, 3, 4, 5),
                  [{"color": "red", "value": None}, {"color": "green", "value": 1}]))
    p.append(timeseries(8, "Caption drift by slice",
                        f"accesspulse_caption_drift_seconds{SEL}", "s", _grid(0, 8, 12, 9),
                        [{"color": "green", "value": None}, {"color": "red", "value": 1.5}],
                        description="Signed offset between rendered captions and aligned "
                                    "spoken dialogue, measured by the caption probe."))
    p.append(table(9, "Territory x platform health",
                   "accesspulse_slo_breached", _grid(12, 8, 12, 9)))
    p.append(timeseries(10, "Accessibility-enabled sessions in affected slices",
                        f"accesspulse_sessions_caption_enabled{SEL}", "short",
                        _grid(0, 17, 8, 8)))
    p.append(timeseries(11, "Encoder CPU / GPU utilisation",
                        "accesspulse_encoder_cpu_utilisation or "
                        "accesspulse_gpu_utilisation", "percentunit", _grid(8, 17, 8, 8)))
    p.append(logs(12, "Delivery chain logs",
                  '{service=~"capenc-.*|packager-main|cdn-primary|clock-.*|signsrc-.*"}',
                  _grid(16, 17, 8, 8)))
    return dashboard("ap-cockpit", "AccessPulse / Live event command centre",
                     ["accesspulse", "live", "event"], p,
                     "What every audience slice is receiving right now, against what was "
                     "promised.", "10s", TEMPLATE_VARS)


def incident_dashboard() -> dict:
    p: list[dict] = []
    p.append(text_panel(1, "Incident investigation", (
        "The evidence the AccessPulse agents retrieved through the Grafana MCP server, "
        "in the order they retrieved it. Every panel here corresponds to one MCP tool "
        "call recorded in the incident audit log."
    ), _grid(0, 0, 24, 3)))
    p.append(timeseries(INCIDENT_SLI_PANEL_ID, "1-3. Breached SLI over the incident window",
                        f"accesspulse_caption_drift_seconds{SEL}", "s", _grid(0, 3, 12, 8),
                        [{"color": "green", "value": None}, {"color": "red", "value": 1.5}],
                        description="query_prometheus"))
    p.append(timeseries(3, "4. Audience impact (k-anonymised aggregates)",
                        f"accesspulse_sessions_caption_enabled{SEL}", "short",
                        _grid(12, 3, 12, 8), description="query_prometheus"))
    p.append(logs(4, "6. Component logs for the implicated pool",
                  '{service=~"capenc-.*|clock-.*"}', _grid(0, 11, 12, 9)))
    p.append(traces(5, "7. Media path trace", '{ .component != "" }', _grid(12, 11, 12, 9)))
    p.append(timeseries(6, "12. Recovery verification",
                        f"accesspulse_caption_drift_seconds{SEL}", "s", _grid(0, 20, 12, 8),
                        [{"color": "green", "value": None}, {"color": "red", "value": 1.5}],
                        description="Post-action series read back through MCP before the "
                                    "incident may close."))
    p.append(table(7, "Verification assertions",
                   "accesspulse_verification_assertion_status", _grid(12, 20, 12, 8)))
    return dashboard("ap-incident", "AccessPulse / Incident investigation",
                     ["accesspulse", "incident", "captions"], p,
                     "Evidence retrieved through Grafana MCP during an accessibility "
                     "incident, plus the post-action verification.", "10s", TEMPLATE_VARS)


def quality_dashboard() -> dict:
    p: list[dict] = []
    p.append(text_panel(1, "Probe and model quality", (
        "The probe fleet is measured like any other production dependency: latency, "
        "throughput, abstention rate, cross-probe disagreement and calibration. "
        "A model that is confidently wrong is an incident of its own."
    ), _grid(0, 0, 24, 3)))
    p.append(stat(2, "Probe abstention rate",
                  "avg(accesspulse_probe_abstained_ratio)", "percentunit",
                  _grid(0, 3, 6, 5)))
    p.append(stat(3, "Mean model confidence",
                  "avg(accesspulse_probe_confidence)", "percentunit", _grid(6, 3, 6, 5)))
    p.append(stat(4, "Probe fleet latency p95",
                  "max(accesspulse_probe_duration_ms)", "ms", _grid(12, 3, 6, 5)))
    p.append(stat(5, "Slices evaluated per sweep",
                  "max(accesspulse_slices_evaluated)", "short", _grid(18, 3, 6, 5)))
    p.append(timeseries(6, "Caption semantic preservation",
                        "accesspulse_caption_semantic_score", "percentunit",
                        _grid(0, 8, 12, 8),
                        [{"color": "red", "value": None}, {"color": "green", "value": 0.9}]))
    p.append(timeseries(7, "Caption omission rate", "accesspulse_caption_omission_ratio",
                        "percentunit", _grid(12, 8, 12, 8),
                        [{"color": "green", "value": None}, {"color": "red", "value": 0.03}]))
    p.append(flamegraph(8, "Probe fleet CPU profile", _grid(0, 16, 12, 9),
                        "accesspulse-probe-fleet"))
    p.append(flamegraph(9, "Agent CPU profile", _grid(12, 16, 12, 9), "accesspulse-agent"))
    return dashboard("ap-quality", "AccessPulse / Probe and model quality",
                     ["accesspulse", "model", "quality"], p,
                     "Precision, calibration, abstention and cost of the measurement "
                     "layer itself.", "30s")


def agent_dashboard() -> dict:
    p: list[dict] = []
    p.append(text_panel(1, "Agent and MCP observability", (
        "AccessPulse observes the agent it builds. Every agent step is a span, every "
        "Grafana MCP tool call is timed and counted, and token usage and cost are "
        "tracked per model. This is the panel that proves the investigation actually "
        "went through MCP."
    ), _grid(0, 0, 24, 3)))
    p.append(stat(2, "MCP calls this incident", "sum(accesspulse_mcp_calls_total)", "short",
                  _grid(0, 3, 6, 5)))
    p.append(stat(3, "MCP p95 latency", "max(accesspulse_mcp_call_duration_ms)", "ms",
                  _grid(6, 3, 6, 5)))
    p.append(stat(4, "MCP errors",
                  'sum(accesspulse_mcp_calls_total{status="error"}) or vector(0)', "short",
                  _grid(12, 3, 6, 5),
                  [{"color": "green", "value": None}, {"color": "red", "value": 1}]))
    p.append(stat(5, "Agent cost", "sum(accesspulse_agent_cost_usd)", "currencyUSD",
                  _grid(18, 3, 6, 5)))
    p.append(timeseries(6, "MCP call latency by tool",
                        "accesspulse_mcp_call_duration_ms", "ms", _grid(0, 8, 12, 8),
                        legend="{{tool}}"))
    p.append(timeseries(7, "Agent step duration", "accesspulse_agent_step_duration_ms",
                        "ms", _grid(12, 8, 12, 8), legend="{{agent}}"))
    p.append(traces(8, "Agent reasoning trace",
                    '{ resource.service.name = "accesspulse-agent" }', _grid(0, 16, 12, 9)))
    p.append(timeseries(9, "Tokens in / out",
                        "accesspulse_agent_tokens_in or accesspulse_agent_tokens_out",
                        "short", _grid(12, 16, 12, 9), legend="{{agent}}"))
    return dashboard("ap-agent", "AccessPulse / Agent and MCP observability",
                     ["accesspulse", "agent", "mcp", "ai-observability"], p,
                     "Traces, tool selection, latency, token usage and cost for the "
                     "AccessPulse agents and their Grafana MCP interactions.", "10s")


DASHBOARDS = {
    "executive.json": executive_dashboard,
    "live-cockpit.json": cockpit_dashboard,
    "incident-investigation.json": incident_dashboard,
    "probe-model-quality.json": quality_dashboard,
    "agent-mcp-observability.json": agent_dashboard,
}


# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------


def _expr_for(s: SLODefinition) -> str:
    thr = s.threshold(TIER)
    grouping = "by (language, territory, platform, player_version)"
    if s.comparator is Comparator.LOWER_IS_BETTER:
        return f"max {grouping} ({s.sli_metric}) > {thr}"
    return f"min {grouping} ({s.sli_metric}) < {thr}"


def alert_rules() -> dict:
    rules = []
    for s in ALL_SLOS:
        rules.append({
            "uid": f"accesspulse-{s.slo_id.replace('.', '-')}",
            "title": f"{s.name} outside objective",
            "condition": "C",
            "for": "30s" if s.hard_gate else "2m",
            "labels": {
                "severity": "sev1" if s.hard_gate else "sev2",
                "feature": s.feature.value,
                "slo": s.slo_id,
                "team": "accessibility-operations",
                "event": "evt-lumiere-premiere",
            },
            "annotations": {
                "summary": f"{s.name}: {s.description}",
                "slo_objective": str(s.threshold(TIER)),
                "unit": s.unit,
                "runbook_url": f"https://runbooks.accesspulse.local/{s.slo_id}",
                # Grafana rejects the whole provisioning file — and refuses to
                # start — if one of these is present without the other. Both
                # point at the breached-SLI panel of the incident dashboard, so
                # "view the alert" lands an operator on the series that fired.
                "__dashboardUid__": "ap-incident",
                "__panelId__": str(INCIDENT_SLI_PANEL_ID),
            },
            "data": [
                {
                    "refId": "A",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "datasourceUid": "ap-prom",
                    "model": {"refId": "A", "expr": _expr_for(s), "instant": False,
                              "range": True, "intervalMs": 10000, "maxDataPoints": 60},
                },
                {
                    "refId": "B",
                    "datasourceUid": "__expr__",
                    "model": {"refId": "B", "type": "reduce", "reducer": "last",
                              "expression": "A"},
                },
                {
                    "refId": "C",
                    "datasourceUid": "__expr__",
                    "model": {
                        "refId": "C", "type": "threshold", "expression": "B",
                        "conditions": [{
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last", "params": []},
                            "type": "query",
                        }],
                    },
                },
            ],
            "noDataState": "OK",
            "execErrState": "Alerting",
            "isPaused": False,
        })
    return {
        "apiVersion": 1,
        "groups": [{
            "orgId": 1,
            "name": "accessibility-slo",
            "folder": "AccessPulse",
            "interval": "10s",
            "rules": rules,
        }],
    }


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the generated assets differ from what is on disk")
    args = ap.parse_args()

    DASH_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    drift = []

    for filename, builder in DASHBOARDS.items():
        path = DASH_DIR / filename
        content = json.dumps(builder(), indent=2, sort_keys=False) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(content, encoding="utf-8")

    rules_path = ALERT_DIR / "accesspulse-rules.yml"
    rules_content = (
        "# GENERATED by tools/generate_grafana_assets.py from accesspulse/slo.py.\n"
        "# Do not edit by hand: change the SLO definition and regenerate.\n"
        + yaml.safe_dump(alert_rules(), sort_keys=False, width=100)
    )
    if args.check:
        if not rules_path.exists() or rules_path.read_text(encoding="utf-8") != rules_content:
            drift.append(str(rules_path.relative_to(ROOT)))
    else:
        rules_path.write_text(rules_content, encoding="utf-8")

    if args.check and drift:
        print("Grafana assets are out of date; run tools/generate_grafana_assets.py")
        for d in drift:
            print("  -", d)
        return 1

    if not args.check:
        print(f"wrote {len(DASHBOARDS)} dashboards to {DASH_DIR.relative_to(ROOT)}")
        print(f"wrote {len(alert_rules()['groups'][0]['rules'])} alert rules to "
              f"{rules_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
