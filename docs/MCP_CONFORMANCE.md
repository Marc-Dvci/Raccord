# Grafana MCP conformance — measured, not assumed

Raccord agents never name a Grafana MCP tool. An agent asks for a
**capability** ("read the firing alert"); the client resolves that against
whatever the connected server advertises, translates the call into that server's
actual schema, and normalises the answer back
([ADR 0002](adr/0002-grafana-mcp-is-the-only-route-to-truth.md)).

That indirection exists because the Grafana MCP tool surface is not stable. This
document records what happened when we pointed Raccord at a real one, and it
is regenerated from artifacts rather than written from memory.

Reproduce it:

```bash
docker compose up -d
./tools/grafana_token.sh                                  # mints a service-account token
docker compose --profile mcp up -d mcp-grafana            # the official server
python tools/mcp_conformance.py --transport http --out docs/mcp_conformance.json
```

---

## 1. What the official server offers

**Server:** official `grafana/mcp-grafana:1.0.0` image, `streamable-http`, against Grafana
11.5.1 provisioned by this repository's `docker-compose.yml`.
**Date:** 6 August 2026. **Artifact:** [`mcp_conformance.json`](mcp_conformance.json).

| | |
|---|---|
| Tools the server advertises | **65** |
| Capabilities Raccord defines | 20 |
| Capabilities resolved | **18** |
| **Required capabilities resolved** | **12 of 12** |

The two unresolved are optional and have no route on this build:
`get_trace` (fetch one trace by id) and `fetch_pyroscope_profile`.

### Three of them do not resolve by name

Resolution alone was not enough, and this is the interesting part:

| Capability | What the server actually has | How it is reached |
|---|---|---|
| `list_alert_rules` | folded into `alerting_manage_rules`, an action-dispatch tool | `operation: "list"`, plus label selectors rewritten into Prometheus-style strings |
| `get_alert_rule` | same tool | `operation: "get"`, `rule_uid` |
| `query_tempo_traces` | **nothing — there is no Tempo tool.** The enabled-tool list has no trace category at all | `grafana_api_request` against Grafana's Tempo datasource proxy, which is how Grafana's own Explore queries Tempo |

`find_annotations` resolved to the server's `get_annotations` with no code
change at all — the capability table already carried both names. That is the
mechanism working as designed.

## 2. Why a name is not enough

The dedicated tools and the dispatch tool take different arguments and answer in
different shapes. `src/raccord/grafana_mcp/adapters.py` holds one adapter per
(capability, tool) pair that deviates from the canonical shape. Things it had to
reconcile, each found by running it:

- **Time formats.** Grafana's parser rejects the fractional seconds Python's
  `isoformat()` emits. Every timestamp is re-emitted as `%Y-%m-%dT%H:%M:%SZ`;
  annotations want epoch milliseconds instead.
- **Datasource UIDs.** Real tools require an explicit `datasourceUid`. These are
  **discovered** at connect time via `list_datasources`, not assumed — a Grafana
  somebody else provisioned will not have named things the way ours does.
- **Prometheus returns a matrix**, not the reduced series the agents read. The
  adapter collapses it with the aggregation the caller asked for, and sorts worst-first.
- **Alert rules come back with an empty `uid`** from the list operation. Ours are
  provisioned from the SLO catalogue, so the uid is *recovered* from the `slo`
  label rather than invented; without that label it stays empty and the rule
  simply cannot be fetched in detail.
- **`generate_deeplink` answers with a bare URL string**, not JSON.
- **Grafana Incident wants labels as a list of objects**, not a mapping.

Nothing in that layer decides anything operational. It renames, reduces and
re-labels. When a translation cannot be made honestly it raises, and the
capability is treated as unavailable — the state machine then refuses to leave
`SCOPED` rather than proceed on evidence that is not there.

`tests/test_adapters.py` pins all of the above against the shapes recorded here,
so the next time the server moves, a test names the translation that stopped
being true instead of an investigation dying halfway through.

## 3. The closed loop, against the official server

**It runs.** Artifact: [`real_mcp_run.json`](real_mcp_run.json).

| | |
|---|---|
| Final state | **`REVIEWED`** |
| Recovered and verified | **yes** |
| Post-action assertions | **9 / 9 passing** |
| Scope precision / recall | **1.00 / 1.00** |
| Unsafe actions | **0** |
| Audit chain valid | yes |
| Sessions affected / protected | 8,053 / 140,295 |
| **Grafana MCP calls** | **16, all successful** |

The call chain, every one of them through the official server:

```
 1 list_datasources        7 query_loki_logs        13 list_incidents
 2 alerting_manage_rules   8 query_loki_logs        14 create_annotation
 3 alerting_manage_rules   9 grafana_api_request    15 query_prometheus
 4 query_prometheus       10 search_dashboards      16 create_annotation
 5 query_prometheus       11 generate_deeplink
 6 get_annotations        12 create_incident
```

Every evidence item in the resulting incident carries a `grafana.mcp:` source
tool. The alert that opened it was a **real Grafana alert rule in `firing`
state**, evaluated by Grafana against real Prometheus data. The logs are real
Loki. The traces are real Tempo. The change annotations are real Grafana
annotations.

### What makes that possible

Raccord has to *put* data in the stack before it can read it back through
MCP. `RACCORD_EXPORT_TELEMETRY=true` pushes probe findings (scraped from `/metrics`),
component logs (Loki push API), media-path spans (OTLP) and change annotations
(Grafana annotations API) into the stack as the event advances.

That is Raccord emitting its own telemetry, not the agent gathering
evidence. The agent still learns nothing except through the MCP server. The
export path also maps the simulated programme clock onto the real one, because
the event advances faster than wall time and metric, log and trace evidence for
one incident have to land in one window to be correlated at all.

## 4. Graceful degradation on optional capabilities

`create_incident`, `add_activity_to_incident` and `list_incidents` are Grafana
Incident (IRM) features, declared **optional**. A call that fails is recorded as
a degradation note rather than raised: filing an incident record is a write-back,
and a caption fix must never be blocked because an optional one could not be
filed.

Required evidence is never routed that way. A hole in the alert / metric / log /
trace / dashboard chain stops the state machine dead.

## 5. Deployment surface

The client speaks the same streamable-HTTP transport to the open-source server
and to **Grafana Cloud's hosted endpoint** (`https://mcp.grafana.com/mcp`);
`mcp_grafana_url` carries the OAuth-side configuration for it. The measurements
above are the open-source server, which is the build most teams run.

`get_trace` and Pyroscope profiles resolve when a server exposes them; this build
does not, and the capability table reports that rather than assuming it.

The 1,000-scenario benchmark in [BENCHMARK.md](BENCHMARK.md) runs against the
in-process server by design, so the corpus reproduces with no credentials and no
network on any machine. The real-server path is measured by the committed run
above.
