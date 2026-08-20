"""Translate Raccord capability calls into a real server's tool schemas.

Raccord agents call *capabilities*, not tools: `mcp.call("query_prometheus",
expr=..., start=..., end=..., aggregation="max")`. The in-process server
(`stub.py`) accepts exactly that, because it was written to. The official
`grafana/mcp-grafana` server was not: it wants `datasourceUid`, RFC3339 times
without fractional seconds, an explicit `stepSeconds`, and it answers with raw
Prometheus matrix data rather than the reduced series the agents read.

Capability *resolution* (`client.CAPABILITIES`) already handles a tool being
renamed. It cannot handle a tool taking different arguments, returning a
different shape, or being replaced by an action-dispatch tool that multiplexes
several capabilities. That is what this module is for.

Design notes, because the alternative was tempting and wrong:

* Adapters are keyed by **(capability, resolved tool name)**. A server whose
  tool already speaks the canonical shape needs no entry and gets a pass-through.
  So this file describes deviations, not the protocol.
* Nothing here decides anything operational. An adapter renames arguments,
  reduces a matrix to the aggregate the caller asked for, and re-labels fields.
  If a translation cannot be made honestly it raises, and the capability is
  treated as unavailable — the state machine then refuses to leave `SCOPED`
  rather than proceeding on evidence that is not there (ADR 0002).
* Every adapted response keeps the same keys the agents already read, so no
  agent, probe or test knows which server answered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class AdapterContext:
    """What a translation needs to know about the server it is talking to.

    Populated once at connect time. Datasource UIDs are discovered rather than
    assumed: a Grafana provisioned by somebody else will not use ours.
    """

    grafana_url: str = ""
    datasources: dict[str, str] | None = None  # type -> uid

    def uid(self, kind: str) -> str:
        ds = self.datasources or {}
        found = ds.get(kind)
        if not found:
            raise AdapterError(
                f"no {kind} datasource is configured on this Grafana; "
                f"available: {sorted(ds) or 'none'}"
            )
        return found


class AdapterError(RuntimeError):
    """A capability cannot be expressed against this server."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


def _dt(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rfc3339(value: Any, default: datetime) -> str:
    """Grafana's time parser rejects fractional seconds; Python emits them."""
    return _dt(value, default).astimezone(timezone.utc).strftime(_RFC3339)


def _epoch_ms(value: Any, default: datetime) -> int:
    return int(_dt(value, default).timestamp() * 1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payload(result: Any) -> Any:
    """MCP content is text; most of these tools put JSON in it."""
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


def _reduce(values: list, how: str) -> float:
    """Collapse a Prometheus range vector to the single number the agents read."""
    nums = []
    for point in values or []:
        try:
            nums.append(float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
    if not nums:
        return 0.0
    if how == "max":
        return max(nums)
    if how == "min":
        return min(nums)
    if how == "mean":
        return sum(nums) / len(nums)
    return nums[-1]  # "last", and the default


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


def _req_prometheus(args: dict, ctx: AdapterContext) -> dict:
    end = _dt(args.get("end"), _now())
    start = _dt(args.get("start"), end - timedelta(minutes=20))
    span = max((end - start).total_seconds(), 60.0)
    return {
        "datasourceUid": ctx.uid("prometheus"),
        "expr": args["expr"],
        "queryType": "range",
        "startTime": _rfc3339(start, start),
        "endTime": _rfc3339(end, end),
        # Roughly 60 points across the window, floor 10s: enough shape to tell a
        # progressive drift from a step change, which is the distinction the
        # diagnosis agent is making.
        "stepSeconds": max(int(span // 60), 10),
    }


def _res_prometheus(result: Any, args: dict) -> dict:
    data = _payload(result)
    series = data.get("data", []) if isinstance(data, dict) else (data or [])
    how = args.get("aggregation", "last")
    rows = []
    for item in series if isinstance(series, list) else []:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        if values is None and item.get("value") is not None:
            values = [item["value"]]
        rows.append({"metric": item.get("metric", {}), "value": _reduce(values, how)})
    reverse = how != "min"
    rows.sort(key=lambda r: r["value"], reverse=reverse)
    return {
        "status": "success",
        "queryType": "range",
        "expr": args.get("expr", ""),
        "resultCount": len(rows),
        "result": rows[:40],
    }


# ---------------------------------------------------------------------------
# Loki
# ---------------------------------------------------------------------------


def _req_loki(args: dict, ctx: AdapterContext) -> dict:
    end = _dt(args.get("end"), _now())
    start = _dt(args.get("start"), end - timedelta(minutes=30))
    return {
        "datasourceUid": ctx.uid("loki"),
        "logql": args["expr"],
        "startRfc3339": _rfc3339(start, start),
        "endRfc3339": _rfc3339(end, end),
        "limit": int(args.get("limit", 50)),
        "direction": "backward",
    }


def _res_loki(result: Any, args: dict) -> dict:
    data = _payload(result)
    entries = data.get("data", []) if isinstance(data, dict) else (data or [])
    rows = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        labels = e.get("labels") or e.get("stream") or {}
        ts = e.get("timestamp") or e.get("ts") or e.get("time")
        rows.append(
            {
                "timestamp": _iso(ts),
                "labels": labels,
                "line": e.get("line") or e.get("value") or e.get("message") or "",
            }
        )
    return {
        "status": "success",
        "expr": args.get("expr", ""),
        "resultCount": len(rows),
        "result": rows,
    }


def _iso(ts: Any) -> str:
    """Loki timestamps arrive as RFC3339, epoch ns, or epoch ms depending on hop."""
    if isinstance(ts, str) and "T" in ts:
        return ts
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return _now().isoformat()
    if n > 1e17:  # nanoseconds
        n /= 1e9
    elif n > 1e11:  # milliseconds
        n /= 1e3
    return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Alerting — one dispatch tool now serves two capabilities
# ---------------------------------------------------------------------------


# `alerting_manage_rules` list responses carry state and labels but an empty
# uid. Ours are provisioned from the SLO catalogue by
# tools/generate_grafana_assets.py, so the uid is recoverable from the slo
# label. Recovered rather than invented: if the label is absent the uid stays
# empty and the rule simply cannot be fetched in detail.
def _uid_from_labels(labels: dict) -> str:
    slo = (labels or {}).get("slo")
    return f"raccord-{slo.replace('.', '-')}" if slo else ""


def _req_alert_list(args: dict, ctx: AdapterContext) -> dict:
    out: dict[str, Any] = {"operation": "list", "limit_alerts": 0}
    selectors = args.get("label_selectors") or {}
    if selectors:
        joined = ", ".join(f'{k}="{v}"' for k, v in selectors.items())
        out["label_selectors"] = ["{" + joined + "}"]
    return out


def _res_alert_list(result: Any, args: dict) -> list[dict]:
    data = _payload(result)
    rules = (
        data if isinstance(data, list) else data.get("rules", []) if isinstance(data, dict) else []
    )
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        labels = r.get("labels", {}) or {}
        annotations = r.get("annotations", {}) or {}
        out.append(
            {
                "uid": r.get("uid") or _uid_from_labels(labels),
                "title": r.get("title", ""),
                "folderUID": r.get("folder_uid", ""),
                "ruleGroup": r.get("rule_group", ""),
                "for": r.get("for", ""),
                "labels": labels,
                "annotations": annotations,
                # The agents read `state == "firing"`; Grafana says "alerting" in
                # some builds and "firing" in others.
                "state": "firing" if r.get("state") in ("firing", "alerting") else r.get("state"),
                "activeAt": r.get("last_evaluation"),
                "query": _expr_of(r),
            }
        )
    return out[: int(args.get("limit", 200))]


def _req_alert_get(args: dict, ctx: AdapterContext) -> dict:
    uid = args.get("uid")
    if not uid:
        raise AdapterError("alert rule uid is required and was not resolvable")
    return {"operation": "get", "rule_uid": uid}


def _res_alert_get(result: Any, args: dict) -> dict:
    r = _payload(result)
    if not isinstance(r, dict):
        raise AdapterError("alert rule detail was not an object")
    labels = r.get("labels", {}) or {}
    return {
        "uid": r.get("uid", ""),
        "title": r.get("title", ""),
        "folderUID": r.get("folder_uid", ""),
        "ruleGroup": r.get("rule_group", ""),
        "for": r.get("for", ""),
        "labels": labels,
        "annotations": r.get("annotations", {}) or {},
        "state": r.get("state"),
        "query": _expr_of(r),
    }


def _expr_of(rule: dict) -> str:
    """The PromQL behind a rule, which the evidence record quotes."""
    for item in rule.get("data") or []:
        model = (item or {}).get("model") or {}
        expr = model.get("expr")
        if expr:
            return expr
    return ""


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def _req_annotations(args: dict, ctx: AdapterContext) -> dict:
    end = _dt(args.get("end"), _now())
    start = _dt(args.get("start"), end - timedelta(hours=2))
    return {
        "from": _epoch_ms(start, start),
        "to": _epoch_ms(end, end),
        "limit": int(args.get("limit", 50)),
        "tags": list(args.get("tags") or []),
        # Ours carry one of several change kinds; requiring all of them at once
        # would return nothing.
        "matchAny": True,
    }


def _res_annotations(result: Any, args: dict) -> list[dict]:
    data = _payload(result)
    items = data.get("Payload", data) if isinstance(data, dict) else data
    out = []
    for a in items if isinstance(items, list) else []:
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "id": a.get("id"),
                "time": _iso(a.get("time")),
                "timeEnd": _iso(a.get("timeEnd")) if a.get("timeEnd") else None,
                "text": a.get("text", ""),
                "tags": a.get("tags", []) or [],
                "dashboardUID": a.get("dashboardUID") or a.get("dashboardUid"),
            }
        )
    return out


def _req_create_annotation(args: dict, ctx: AdapterContext) -> dict:
    out: dict[str, Any] = {"text": args["text"], "tags": list(args.get("tags") or [])}
    if args.get("dashboardUID"):
        out["dashboardUid"] = args["dashboardUID"]
    return out


def _res_create_annotation(result: Any, args: dict) -> dict:
    data = _payload(result)
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "message": data.get("message", "Annotation added"),
            "text": args.get("text", ""),
            "tags": args.get("tags", []),
        }
    return {"id": None, "message": str(data)[:200], "text": args.get("text", "")}


# ---------------------------------------------------------------------------
# Dashboards and deep links
# ---------------------------------------------------------------------------


def _req_search_dashboards(args: dict, ctx: AdapterContext) -> dict:
    return {"query": args.get("query", ""), "limit": int(args.get("limit", 20))}


def _res_search_dashboards(result: Any, args: dict) -> list[dict]:
    data = _payload(result)
    items = data.get("dashboards", []) if isinstance(data, dict) else data
    out = []
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict):
            continue
        out.append(
            {
                "uid": d.get("uid", ""),
                "title": d.get("title", ""),
                "tags": d.get("tags", []) or [],
                "url": d.get("url", ""),
                "folderTitle": d.get("folderTitle", ""),
            }
        )
    return out


def _req_deeplink(args: dict, ctx: AdapterContext) -> dict:
    out: dict[str, Any] = {
        "resourceType": args.get("resourceType", "dashboard"),
        "dashboardUid": args.get("dashboardUid", ""),
    }
    tr = args.get("timeRange") or {}
    if tr:
        out["timeRange"] = {
            "from": _rfc3339(tr.get("from"), _now() - timedelta(minutes=30)),
            "to": _rfc3339(tr.get("to"), _now()),
        }
    if args.get("queryParams"):
        out["queryParams"] = args["queryParams"]
    return out


def _res_deeplink(result: Any, args: dict) -> dict:
    data = _payload(result)
    if isinstance(data, dict):
        return {"url": data.get("url", ""), "resourceType": args.get("resourceType", "")}
    return {"url": str(data).strip(), "resourceType": args.get("resourceType", "")}


# ---------------------------------------------------------------------------
# Traces — no Tempo tool exists on current open-source builds
# ---------------------------------------------------------------------------

# The server exposes no trace tool at all. It does expose `grafana_api_request`,
# which reaches any Grafana API path — including the datasource proxy, which is
# how Grafana's own Explore queries Tempo. Routing traces through it keeps the
# invariant that matters: every fact still arrives through the MCP server, over
# the MCP protocol, with the same call recorded in the same audit log. It is a
# different *path* to Tempo, not a bypass of MCP.


def _req_tempo(args: dict, ctx: AdapterContext) -> dict:
    end = _dt(args.get("end"), _now())
    start = _dt(args.get("start"), end - timedelta(minutes=30))
    uid = ctx.uid("tempo")
    params = [
        f"start={int(start.timestamp())}",
        f"end={int(end.timestamp())}",
        f"limit={int(args.get('limit', 20))}",
    ]
    service = args.get("service")
    if service:
        # TraceQL, which the proxy accepts on /api/search.
        params.append(f'q={{resource.service.name="{service}"}}')
    return {
        "endpoint": f"/api/datasources/proxy/uid/{uid}/api/search?" + "&".join(params),
        "method": "GET",
    }


def _res_tempo(result: Any, args: dict) -> dict:
    data = _payload(result)
    body = data.get("data", data) if isinstance(data, dict) else data
    traces = body.get("traces", []) if isinstance(body, dict) else []
    out = []
    for t in traces if isinstance(traces, list) else []:
        if not isinstance(t, dict):
            continue
        start_ns = t.get("startTimeUnixNano")
        out.append(
            {
                "traceID": t.get("traceID", ""),
                "spanID": t.get("spanID", ""),
                "parentSpanID": None,
                "rootServiceName": t.get("rootServiceName", ""),
                "name": t.get("rootTraceName", ""),
                "startTime": _iso(start_ns) if start_ns else _now().isoformat(),
                "durationMs": t.get("durationMs", 0),
                "attributes": {"component": t.get("rootServiceName", "")},
                "status": "ok",
            }
        )
    return {"status": "success", "resultCount": len(out), "traces": out}


# ---------------------------------------------------------------------------
# Grafana Incident (IRM)
# ---------------------------------------------------------------------------


def _req_create_incident(args: dict, ctx: AdapterContext) -> dict:
    """Grafana IRM wants labels as a list of objects, not a mapping."""
    labels = args.get("labels") or {}
    return {
        "title": args.get("title", ""),
        "severity": args.get("severity", "minor"),
        "labels": [{"label": f"{k}:{v}"} for k, v in labels.items()],
    }


def _res_create_incident(result: Any, args: dict) -> dict:
    data = _payload(result)
    if isinstance(data, dict):
        inc = data.get("incident", data)
        return {
            "incidentID": inc.get("incidentID") or inc.get("id"),
            "title": inc.get("title", args.get("title", "")),
            "severity": inc.get("severity", args.get("severity", "")),
            "status": inc.get("status", "active"),
        }
    return {"incidentID": None, "title": args.get("title", "")}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Adapter:
    request: Callable[[dict, AdapterContext], dict]
    response: Callable[[Any, dict], Any]


# [capability][server tool name] -> Adapter.
# Absent means "this server already speaks the canonical shape": pass through.
ADAPTERS: dict[str, dict[str, Adapter]] = {
    "query_prometheus": {
        "query_prometheus": Adapter(_req_prometheus, _res_prometheus),
    },
    "query_loki_logs": {
        "query_loki_logs": Adapter(_req_loki, _res_loki),
    },
    "list_alert_rules": {
        "alerting_manage_rules": Adapter(_req_alert_list, _res_alert_list),
    },
    "get_alert_rule": {
        "alerting_manage_rules": Adapter(_req_alert_get, _res_alert_get),
    },
    "find_annotations": {
        "get_annotations": Adapter(_req_annotations, _res_annotations),
    },
    "create_annotation": {
        "create_annotation": Adapter(_req_create_annotation, _res_create_annotation),
    },
    "search_dashboards": {
        "search_dashboards": Adapter(_req_search_dashboards, _res_search_dashboards),
    },
    "generate_deeplink": {
        "generate_deeplink": Adapter(_req_deeplink, _res_deeplink),
    },
    "query_tempo_traces": {
        "grafana_api_request": Adapter(_req_tempo, _res_tempo),
    },
    "create_incident": {
        "create_incident": Adapter(_req_create_incident, _res_create_incident),
    },
}


def adapter_for(capability: str, tool: str) -> Adapter | None:
    return ADAPTERS.get(capability, {}).get(tool)
