# Grafana MCP conformance — measured, not assumed

AccessPulse never hard-codes a Grafana MCP tool name into an agent. An agent asks
for a **capability** ("read the firing alert"); `src/accesspulse/grafana_mcp/client.py`
resolves that capability against whatever the connected server actually advertises,
and refuses to begin an investigation if a required capability is missing
([ADR 0002](adr/0002-grafana-mcp-is-the-only-route-to-truth.md)).

That indirection exists because the Grafana MCP tool surface is not stable. This
document records what happened when we pointed AccessPulse at a real one.

Reproduce it:

```bash
docker compose up -d
tools/grafana_token.sh                                   # mints a service-account token
docker compose --profile mcp up -d mcp-grafana           # the official server
python tools/mcp_conformance.py --transport http --out docs/mcp_conformance.json
```

---

## The measured result

**Server:** official `mcp/grafana` image, `streamable-http` transport, against
Grafana 11.5.1 provisioned by this repository's `docker-compose.yml`.
**Date:** 6 August 2026. **Artifact:** [`mcp_conformance.json`](mcp_conformance.json).

| | |
|---|---|
| Tools the server advertises | **65** |
| Capabilities AccessPulse defines | 20 |
| Capabilities that resolved | **14** |
| **Required** capabilities that did not resolve | **3** |

### What resolved

`list_datasources` · `query_prometheus` · `query_loki_logs` · `query_loki_stats` ·
`list_prometheus_metric_names` · `list_prometheus_label_values` ·
`search_dashboards` · `get_dashboard_by_uid` · `generate_deeplink` ·
`create_annotation` · `list_incidents` · `create_incident` ·
`add_activity_to_incident` — and `find_annotations`, which resolved to the
server's `get_annotations`. That last one is the mechanism working: the tool was
renamed, the capability table already carried both names, and no agent changed.

### What did not

| Capability | Why | Consequence |
|---|---|---|
| `list_alert_rules` | Consolidated into an action-dispatch tool, `alerting_manage_rules` | Step 1 of the mandatory chain is unavailable **by that name** |
| `get_alert_rule` | Same consolidation | Step 2 unavailable by name |
| `query_tempo_traces` | **No trace tool exists in this build.** The server's enabled-tool list is `search,datasource,incident,prometheus,loki,alerting,dashboard,folder,oncall,asserts,sift,pyroscope,navigation,proxied,annotations,rendering,snapshot,plugin,api,config,provisioning` — there is no Tempo category | Step 7 has no route at all |

The first two are a **naming** problem: the capability exists, behind a different
tool with a different argument shape. Resolving them needs an argument-translation
layer, not just another entry in the candidate list — adding the name alone would
turn a clean refusal at connect time into a confusing failure mid-investigation,
which is worse. That layer is **not built**.

The third is a **capability** problem: the current open-source server exposes no
way to query traces. Grafana's own API is reachable through the server's generic
`grafana_api_request` tool, so the datasource proxy is a plausible route, but
routing trace evidence that way is a design decision with real consequences for
what "evidence came through MCP" means, and it has not been taken.

---

## What this means, stated plainly

**Against this server today, an AccessPulse investigation cannot leave `SCOPED`.**
That is the system behaving exactly as documented — `REQUIRED_EVIDENCE_TOOLS` in
`src/accesspulse/incident.py` demands alert, metric, log, trace and dashboard
evidence, all with a Grafana MCP `source_tool`, and it will not accept a chain
with a hole in it. The refusal is the safety property, not a bug in it.

It does mean the numbers in [BENCHMARK.md](BENCHMARK.md) and the hero run in the
README were produced against the **in-process MCP server** (`AP_MCP_TRANSPORT=stub`),
which implements the tool surface the capability table was written against. That
is stated in those documents and it is what makes the benchmark reproducible with
no credentials — but it should not be read as "this has been run end to end
against the official server", because it has not.

What *has* been demonstrated against the official server: connection over the MCP
protocol, session initialisation, tool discovery of all 65 tools, and capability
resolution — the artifact above is the output, not a description of it.

## What would close the gap

1. An argument-translation layer per resolved tool, so a capability call is
   rewritten into the connected server's actual schema and its response
   normalised back. This is the real work; the capability table already gives it
   the right place to live.
2. A decision on traces: either route them through `grafana_api_request` to the
   Tempo datasource proxy and say so, or demote the trace capability to optional
   and accept that an investigation can close without a trace — which weakens a
   claim this project makes deliberately.
3. Re-running `tools/mcp_conformance.py` after either change, and replacing the
   artifact above rather than editing this prose.
