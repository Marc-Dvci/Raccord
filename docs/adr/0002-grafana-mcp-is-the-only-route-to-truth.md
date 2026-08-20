# ADR 0002 — Grafana MCP is the agent's only route to operational truth

**Status:** Accepted

## Context

Raccord's own simulator holds the state of the delivery chain, and its own telemetry module
writes the metrics. It would have been trivial — and much faster — for the agents to read those
objects directly.

That would also have made the Grafana integration a presentation layer: a dashboard rendered
after the decisions were already made. Judges of an observability track can tell the difference,
and so can operators, because a system that reads its own memory rather than the observability
stack cannot be dropped into someone else's estate.

## Decision

The agents reach operational facts **only** through the Grafana MCP server, and the incident
state machine enforces it. The transition into `EVIDENCE_COMPLETE` has a machine-checkable
precondition requiring evidence whose `source_tool` is a Grafana MCP tool for **alerts, metrics,
logs, traces and dashboards**:

```python
REQUIRED_EVIDENCE_TOOLS = (
    "grafana.mcp:list_alert_rules",
    "grafana.mcp:query_prometheus",
    "grafana.mcp:query_loki_logs",
    "grafana.mcp:query_tempo_traces",
    "grafana.mcp:search_dashboards",
)
```

There is no fallback path that queries Prometheus, Loki or Tempo directly. Delete the MCP server
and the incident cannot leave `SCOPED`.

The client resolves the capabilities it needs against the server's *advertised* tool list at
connect time and refuses to start an investigation if any is missing, so a downgraded or
substituted server fails closed rather than degrading silently.

## Alternatives considered

**Read the simulator directly, publish to Grafana for display.** Rejected: it makes Grafana
decorative, and the resulting agent could not investigate a real estate.

**Query Prometheus/Loki/Tempo over their own HTTP APIs.** Rejected: three bespoke clients, three
auth paths, no single governed tool surface to observe or restrict — and no MCP story.

**Use MCP but allow a direct fallback when it is unavailable.** Rejected explicitly. A fallback
is the path that gets taken under pressure, and then the guarantee is worth nothing. Failing
closed is the honest behaviour: if the observability stack is down, the correct response is to
tell a human, not to guess from private state.

## Consequences

**Good.** The investigation is portable to any Grafana estate. Every fact in an incident is
attributable to a named tool call with a request, a response digest and a timestamp, all hashed
into `evidence_hash`. The in-process MCP server (`grafana_mcp/stub.py`) offers the same tool
surface, so the whole demonstration runs with no credentials while exercising the same code path
as the real server.

**Costly.** Round-trips: the hero incident makes 17 MCP calls where direct reads would be a
handful of attribute lookups. Mean 16.9 calls per incident across the benchmark. We consider
this the price of the guarantee and report it as a metric rather than hiding it.

**Residual risk.** An attacker who controls the MCP path can mislead the agent. Contained and
attributable, not prevented — see [THREAT_MODEL.md](../THREAT_MODEL.md) §4.8.
