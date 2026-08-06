# The Grafana MCP call chain

Every operational fact in an AccessPulse incident arrives through the Grafana MCP server. This
document lists each call, why it is made, what comes back, and what would break without it.

Source: [`src/accesspulse/agents/evidence.py`](../src/accesspulse/agents/evidence.py).

## Capability resolution, not hard-coded tool names

Grafana MCP tool names differ between releases and between the open-source server and the hosted
Cloud endpoint. AccessPulse therefore asks for a **capability** and resolves it against whatever
the connected server actually advertises:

```python
Capability("query_tempo_traces", ("query_tempo_traces", "find_traces", "search_traces"),
           required=True, purpose="follow the media path trace through the affected pool")
```

On connect, the client calls `list_tools()`, resolves every capability, and **refuses to start**
if a required one is missing:

```
MCPUnavailable: the connected Grafana MCP server does not expose required capabilities:
query_tempo_traces (server advertises 41 tools)
```

`accesspulse mcp` prints the resolution table for the currently configured transport.

## Transports

| `AP_MCP_TRANSPORT` | What it is | When to use it |
|---|---|---|
| `stub` (default) | An in-process server implementing the same tool names, argument shapes and response shapes against the local telemetry plane | Reproducible demo, CI, and the 1,000-scenario benchmark — no account, no token, no network |
| `stdio` | The official `grafana/mcp-grafana` binary or `mcp/grafana` container over stdio, with a Grafana service-account token | Unattended server-side operation |
| `http` | The hosted Grafana Cloud MCP endpoint (`https://mcp.grafana.com/mcp`), streamable HTTP with OAuth 2.1 | Interactive operation |

The agent code is identical across all three. Nothing is faked when a real server is present —
the stub exists so the project is reviewable and benchmarkable without credentials, not to avoid
Grafana.

## The mandatory chain

Fourteen steps the investigation cannot skip, plus three conditional calls — **16.9 calls per
incident** across the 1,000-scenario benchmark:

| Conditional call | When it fires |
|---|---|
| `query_tempo_traces`, second issue | the component-scoped trace search returned `resultCount: 0`, so it is re-issued unscoped rather than concluding there is no trace evidence |
| `list_incidents` | reconciles against incidents already open in Grafana before declaring a new one |
| `create_annotation`, approved action | the approved action is annotated on the dashboards as well as added to the incident timeline |

### 1 · `list_alert_rules`

```json
{ "label_selectors": { "feature": "captions" }, "limit": 50 }
```

Returns the accessibility rule group with current state. AccessPulse selects the rule whose
`slo` label matches the breached objective. If the server does not index by that label, it falls
back to the unfiltered list rather than guessing.

**Why:** the incident begins from a *Grafana alert*, not from an internal signal. If the rule
group is not provisioned, the investigation stops here with an explicit error.

### 2 · `get_alert_rule_by_uid`

```json
{ "uid": "accesspulse-cap-drift" }
```

Returns the objective (`slo_objective: "1.5"`), the query, the labels and the runbook URL.

**Why:** the agent must reason against the objective the rule actually encodes, not a constant
compiled into itself.

### 3 · `query_prometheus` — the breached SLI

```json
{ "expr": "accesspulse_caption_drift_seconds", "start": "...", "end": "...",
  "queryType": "range", "aggregation": "max" }
```

Returns one series per audience slice with its sample history.

**Why:** two things at once. The **magnitude** tells you how bad it is; the **shape** tells you
what it is. A fixed clock offset and a progressive drift breach the same objective by the same
amount — only the trajectory separates them, and the diagnosis agent classifies that trajectory
(`ramp` / `step` / `flat`) directly from these samples.

### 4 · `query_prometheus` — audience impact

```json
{ "expr": "accesspulse_sessions_caption_enabled", "aggregation": "last" }
```

**Why:** severity is not the size of the number, it is the number of people. These are
k-anonymised aggregates: "1,842 caption-enabled sessions on the affected platforms", never a
list of viewers. See [PRIVACY.md](PRIVACY.md).

### 5 · `find_annotations`

```json
{ "tags": ["deployment", "config", "change"], "start": "...", "end": "..." }
```

**Why:** the change-correlation engine needs candidates. The window is never empty — a real
change feed contains a packager rollout, a CDN TTL tune, an auth dependency bump, a traffic ramp
and a peering change alongside the one that mattered. Finding the cause is a search, not a
lookup.

### 6 · `query_loki_logs` — the implicated component

```json
{ "expr": "{service=\"capenc-pool-a\"} |= \"resync\"", "limit": 40 }
```

Falls back to the unfiltered stream selector when the line filter returns nothing, so a wrong
guess about wording does not silently produce "no evidence".

### 7 · `query_loki_logs` — the timing reference

```json
{ "expr": "{service=~\"clock-.*\"}", "limit": 20 }
```

**Why:** captions, described audio and the interpreter feed all hang off one timing reference.
Its log is relevant to any of their symptoms, not only to a drift alert. This is the call that
finds:

```
msg="clock resynchronisation" reason=grandmaster_unreachable
previous=clock-ptp-primary current=clock-ntp-fallback step_ms=8000
```

### 8 · `query_tempo_traces`

```json
{ "service": "media-path", "query": "capenc-pool-a", "limit": 10 }
```

Returns the ingest → encode → package → origin → deliver → render spans with `component`,
`clock_source` and `caption_drift_ms` attributes.

**Why:** it localises the delay to a hop. The trace shows the drift attribute rising inside
`caption.encode` on the affected pool and not upstream of it.

### 9 · `search_dashboards`

```json
{ "query": "Incident investigation", "tag": "accesspulse", "limit": 5 }
```

### 10 · `generate_deeplink`

```json
{ "resourceType": "dashboard", "dashboardUid": "ap-incident",
  "timeRange": { "from": "...", "to": "..." },
  "queryParams": { "var-slo": "cap.drift", "var-feature": "captions" } }
```

**Why:** every conclusion the agent reaches must be checkable by a human in Grafana, scoped to
the same window and the same variables. The link is attached to the evidence item and surfaced
in the incident workspace as "Open in Grafana for human review".

### 11 · `create_incident`

```json
{ "title": "...", "severity": "sev2",
  "labels": { "slo": "cap.drift", "accesspulse_incident": "inc-…" } }
```

**Why:** the incident exists in Grafana, where the rest of the on-call organisation already
works — not only inside AccessPulse.

### 12 · `add_activity_to_incident`

Writes the approved action onto the Grafana incident timeline:

```
AccessPulse inc-e5b89a8337: approved action select_synchronized_standby on
capenc-pool-b executed by t.duval@studio.example. Scope: DE/ES/FR/GB|ctv-9.3.1/ctv-9.4.0.
```

### 13 · `query_prometheus` — recovery

Re-reads the SLI after the action and after a settle period.

**Why:** this is the call that makes closure honest. The incident does not close because the
action ran; it closes because the series came back inside objective, read from Grafana, after
the fact.

### 14 · `create_annotation` — recovery

```json
{ "text": "AccessPulse: inc-… recovered — accesspulse_caption_drift_seconds back inside
   objective (worst 0.09)",
  "tags": ["accesspulse", "recovery", "inc-…"], "dashboardUID": "ap-incident" }
```

The annotation lands on the incident dashboard, so the timeline shows the change that caused the
incident, the approved action, and the recovery, on the same axis as the SLI.

---

## The enforcement

The chain is not a convention, it is a precondition:

```python
# src/accesspulse/incident.py
REQUIRED_EVIDENCE_TOOLS = (
    "grafana.mcp:list_alert_rules",
    "grafana.mcp:query_prometheus",
    "grafana.mcp:query_loki_logs",
    "grafana.mcp:query_tempo_traces",
    "grafana.mcp:search_dashboards",
)

def _p_evidence_complete(inc: Incident) -> list[str]:
    tools = {e.source_tool for e in inc.evidence}
    return [f"missing evidence from {r}" for r in REQUIRED_EVIDENCE_TOOLS if r not in tools]
```

Remove the MCP server and the incident cannot leave `SCOPED`. Two tests hold this in place:

- `test_the_investigation_goes_through_mcp_and_only_through_mcp` — asserts every evidence item in
  a completed incident has a `grafana.mcp:` source tool, and that the required tools were called.
- `test_evidence_complete_is_unreachable_without_mcp_evidence` — strips the Prometheus evidence
  and asserts the state machine refuses to advance, naming the missing tool.

There is no code path that queries Prometheus, Loki or Tempo directly during an investigation.

## Observing the MCP layer

Every call is timed, counted, sized and recorded:

- `accesspulse_mcp_call_duration_ms{tool}`
- `accesspulse_mcp_calls_total{tool,status}`

surfaced on the **Agent and MCP observability** dashboard and in the product's own
*Agent & MCP* tab, alongside the capability resolution table. A typical incident makes 16–17
calls; the learning agent flags an investigation that exceeds the expected count as tool-call
inefficiency to be fixed.
