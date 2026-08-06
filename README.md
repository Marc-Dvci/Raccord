# AccessPulse

**Accessible Experience Reliability for live media.**
A stream is not healthy unless every promised accessibility experience is healthy.

> **Agentic Cinema — Grafana track.**
> AccessPulse certifies, monitors, diagnoses, repairs and *proves* the accessibility of a live
> media experience. It investigates through the **Grafana Cloud MCP server** and reasons with
> **Gemini on Google Cloud via the Agent Development Kit** — and it cannot complete an
> investigation without either of them.

---

## The problem

Media reliability systems declare a stream healthy when the picture and the primary audio are
online. By that definition a stream is "healthy" while:

- captions are eight seconds behind the dialogue on two connected-TV builds in Western Europe,
- the described-audio track is declared in the manifest but carries silence,
- the sign-language interpreter is frozen on one repeated frame,
- the caption menu has a keyboard trap, so a keyboard-only viewer cannot turn captions on at all.

Every one of those is total loss of service for the viewers who depend on it, and none of them
moves a conventional availability dashboard. Nobody is paged. The error budget does not burn.
The incident is discovered from social media, an hour later, by which time a live premiere is
over.

AccessPulse treats captions, audio description, alternate-language audio, sign-language video,
accessible playback controls, accessible authentication and accessible purchase as
**production services with SLOs, error budgets, owners, incident procedures and proof of
recovery**.

## What it does

| Mode | What happens |
|---|---|
| **Preflight certification** | Every promise the event made is tested against the real chain and real players before the event may say "accessibility ready". Hard assertions block certification. Output is a signed record. |
| **Live assurance** | The probe fleet measures the *rendered* experience across the language × territory × platform × device × build matrix and evaluates it against the promise that was in force at that moment. |
| **Closed-loop incident response** | Detect → scope → gather evidence through Grafana MCP → diagnose → evaluate policy → obtain a signed approval → execute one allow-listed action → re-measure → communicate → review. Twelve states, no skipping. |
| **Reliability intelligence** | Post-incident measurement of what was missed, which change caused it, how much error budget it cost, and which improvements to propose to a human. |

---

## Run it

The full demonstration runs **with no credentials, no cloud account and no network**.

```bash
git clone <this repo> && cd accesspulse
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

accesspulse hero        # the closed loop, in the terminal, ~30 seconds
accesspulse serve       # the product, at http://localhost:8080
```

`accesspulse hero` injects a documented fault into the digital twin and runs the whole loop,
printing the Grafana MCP call chain and the public status update it generated.

Evaluating rather than exploring? **[docs/JUDGE.md](docs/JUDGE.md)** walks the whole system in
ten minutes offline, including how to check that Grafana MCP is load-bearing rather than taking
our word for it.

### With the real Grafana stack

```bash
docker compose up -d                    # Grafana, Prometheus, Loki, Tempo, Pyroscope
accesspulse serve                       # AccessPulse exposes /metrics; Prometheus scrapes it
open http://localhost:3000              # admin / accesspulse — five provisioned dashboards
```

Then point the agent at the **official Grafana MCP server** instead of the in-process one:

```bash
# .env
AP_MCP_TRANSPORT=stdio
AP_GRAFANA_URL=http://localhost:3000
AP_GRAFANA_SERVICE_ACCOUNT_TOKEN=<token from Grafana → Administration → Service accounts>
```

No agent code changes. The client discovers the server's real tool list, resolves the
capabilities it needs against it, and refuses to start the investigation if a required
capability is missing.

We ran exactly that against the official server and wrote down what happened, including the
part that does not work:

```bash
python tools/mcp_conformance.py --transport http --out docs/mcp_conformance.json
# 65 tools advertised · 14/20 capabilities resolved · 3 required ones unavailable
```

The official `mcp/grafana` server has renamed, consolidated and removed tools since this
capability table was written — most notably, current builds expose **no Tempo tool at all**, so
the trace step of the mandatory chain has no route and an investigation correctly refuses to
leave `SCOPED`. The refusal is the safety property working, and the committed measurement is
[`docs/mcp_conformance.json`](docs/mcp_conformance.json). The published benchmark and the hero
run were produced against the **in-process** MCP server, which implements the surface the
capability table targets. **[docs/MCP_CONFORMANCE.md](docs/MCP_CONFORMANCE.md)** states what has
and has not been demonstrated against the official server, and what would close the gap.

### With Gemini

```bash
# .env
AP_REASONING_MODE=gemini
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<project>
pip install -e ".[cloud]"
```

---

## How Grafana is load-bearing

This is not a project that renders a chart in Grafana at the end. **Grafana MCP is the agent's
only route to operational truth**, and the state machine enforces it: the transition into
`EVIDENCE_COMPLETE` has a machine-checkable precondition requiring evidence whose `source_tool`
is a Grafana MCP tool for alerts, metrics, logs, traces *and* dashboards.

```python
# src/accesspulse/incident.py
REQUIRED_EVIDENCE_TOOLS = (
    "grafana.mcp:list_alert_rules",
    "grafana.mcp:query_prometheus",
    "grafana.mcp:query_loki_logs",
    "grafana.mcp:query_tempo_traces",
    "grafana.mcp:search_dashboards",
)
```

Delete the MCP server and the incident cannot leave `SCOPED`. There is no fallback path that
queries Prometheus, Loki or Tempo directly — a test asserts that every evidence item in a
completed incident came through MCP (`tests/test_mcp_and_loop.py`).

**The mandatory chain**, in order, for the hero incident:

| # | Capability | What it establishes |
|---|---|---|
| 1 | `list_alert_rules` | which accessibility rule is firing |
| 2 | `get_alert_rule_by_uid` | the rule's objective, labels and runbook |
| 3 | `query_prometheus` | the breached SLI across the slice matrix |
| 4 | `query_prometheus` | k-anonymised accessibility-enabled session aggregates |
| 5 | `find_annotations` | deployments and configuration changes in the window |
| 6 | `query_loki_logs` | encoder-pool logs for the implicated component |
| 7 | `query_loki_logs` | the timing-reference daemon's log |
| 8 | `query_tempo_traces` | the media path through the affected pool |
| 9 | `search_dashboards` | the dashboard a human should open |
| 10 | `generate_deeplink` | a reviewable link scoped to the incident window |
| 11 | `create_incident` | declares it in Grafana |
| 12 | `add_activity_to_incident` | the approved action on the Grafana timeline |
| 13 | `query_prometheus` | the recovery series, read back before closing |
| 14 | `create_annotation` | the recovery annotation |

Full detail with request and response shapes: **[docs/MCP_CALL_CHAIN.md](docs/MCP_CALL_CHAIN.md)**.

Everything AccessPulse learns also *becomes* Grafana data: Prometheus series for every probe
finding, SLO evaluation and session aggregate; Loki lines from every delivery component; Tempo
spans for the media path **and for the agent's own reasoning**; Pyroscope profiles for the probe
fleet. Five dashboards and 31 alert rules are generated from the SLO definitions
(`tools/generate_grafana_assets.py`), so a panel threshold can never drift from the objective
the probes are measured against — CI fails if it does.

## How Gemini and Google Cloud are load-bearing

The split is deliberate:

- **The deterministic core owns the facts.** Detection, scope, evidence retrieval, the ranking
  arithmetic, the policy decision and the verification result are computed by typed, testable
  code. No language model decides whether something is broken or whether an action may run.
- **Gemini owns synthesis and language.** It is given the typed incident record and asked to do
  what a model is genuinely good at: explain a multimodal picture across metrics, logs, traces,
  probe findings and change events; state what is uncertain and what evidence would resolve it;
  and write six audience-specific communications in the right register. It also reaches the
  Grafana MCP tool surface through ADK's MCP toolset, so an operator's follow-up question pulls
  one more piece of evidence through the same governed path.
- **It cannot act.** `RemediationExecutor` requires a redeemed, single-use, HMAC-signed approval
  token bound to an exact action hash and evidence hash. No agent can mint one.

Google Cloud: Gemini on Vertex AI, ADK for the agent definitions and MCP toolset, Agent Engine
for the managed runtime (`tools/deploy_agent_engine.py`), Cloud Run for the app, Pub/Sub and
Dataflow for the event plane, Spanner/BigQuery/Cloud Storage for evidence and analytics, Secret
Manager and IAM for the security boundary. See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## The hero incident

A global film-festival premiere is live. Picture and primary audio are perfect. A PTP
grandmaster failover moves the caption chain onto an NTP fallback, and English captions on two
connected-TV builds in Western Europe drift progressively to eight seconds behind the dialogue.

```
$ accesspulse hero
baseline healthy: 0 SLOs breaching

╭─ fault injected ─────────────────────────────────────────────╮
│ Progressive caption drift                                    │
│ ground truth: caption.progressive_drift                      │
╰──────────────────────────────────────────────────────────────╯
breaching now: ['cap.drift', 'cap.omission_rate', 'cap.speaker_accuracy']

detected                        yes
diagnosis                       yes (0.683 posterior)
state                           REVIEWED
scope                           DE/ES/FR/GB · ctv-9.3.1/ctv-9.4.0 · en
scope precision/recall          1.00 / 1.00
policy                          approval_required
approval                        t.duval@studio.example (event_technical_director)
action                          select_synchronized_standby
verification                    9/9 assertions passing
recovered                       yes
sessions affected / protected   8,053 / 140,295
Grafana MCP calls               17
unsafe action                   no
audit chain                     yes
```

Note what the system got right *without being told*: the scope is exactly the four Western
European territories, the two CTV builds and the English track — precision and recall 1.00
against a fault specification the agents never see. It distinguished a progressive drift from a
fixed clock offset by the **shape** of the SLI over the window, not just its magnitude.

And note what it refuses to do: the action is `approval_required` because the event is tier-0
and live, so a signed token from the technical director is required before anything moves; the
incident closes only after nine assertions pass, including *adjacent* checks proving French,
German and Spanish captions, the described audio and the interpreter feed were not regressed by
the fix.

---

## Benchmark

1,000 seeded scenarios drawn from a library of 45 documented faults across every accessibility
feature. The agents never see the fault specification; the harness scores against it.

Run: `accesspulse bench --scenarios 1000` · results: [`bench/results/summary.json`](bench/results/summary.json)
· methodology and full numbers: **[docs/BENCHMARK.md](docs/BENCHMARK.md)**.

Published run: 1,000 scenarios, 45 fault types, seed 20260803.

| Metric | Result |
|---|---|
| Detection rate | **1.000** |
| Scope precision / recall | **1.000 / 0.993** |
| Top-1 root cause | 0.652 |
| Top-3 root cause | 0.897 |
| Corrective action chosen | 0.895 |
| Recovered **and verified** | 0.919 |
| Rolled back after failed verification | 0.081 |
| **False closure rate** | **0.001** |
| **Unsafe action rate** | **0.000** |
| Mean Grafana MCP calls per incident | 16.9 |
| **Top-1 on the hardest band** (difficulty ≥ 0.75, n=147) | **0.150** |

The two numbers that matter most are the two near zero. A false closure — declaring an
accessibility feature restored when it is not — is the failure mode that makes an automated
system worse than no system; it happens once in a thousand scenarios because closure is gated on
re-measurement, not on a model's opinion. When the system picks the wrong action (10.5% of the
time) verification catches it and rolls back rather than closing.

The last row is in the table on purpose. Accuracy is strongly stratified: 0.817 top-1 on the
easiest band, **0.150** on the hardest, where infrastructure causes present as the media symptoms
they induce. [docs/BENCHMARK.md](docs/BENCHMARK.md) §3 names the eight most-misdiagnosed faults
and explains why, §4 reports an ablation whose result is *null* rather than promoting it, and §7
lists nine limitations including one that says our impact figures are arithmetic over modelled
populations and should not be quoted as audience numbers.

---

## The product

`accesspulse serve` is an operational product, not a chat window:

- **Overview** — global accessible-experience map, promise registry, error budgets, fault library
- **Readiness studio** — every preflight assertion with result, evidence, owner and blocker list
- **Live cockpit** — audience experience, telemetry, environment state and delivery logs
- **Incident workspace** — evidence, ranked diagnosis, change correlation, policy, approval,
  verification, communications and the hash-chained audit trail, each visually and structurally
  separated so that *evidence*, *hypothesis*, *policy* and *verified result* are never confused
- **Evidence replay** — the exact affected interval: what was spoken against what was captioned
- **Agent & MCP observability** — every tool call, latency, capability resolution and agent step
- **Benchmark laboratory** — the measured results, in the product

The UI is dependency-free (no framework, no bundler, no CDN), conforms to WCAG 2.2 AA, and is
keyboard-operable throughout. Status is never conveyed by colour alone. `python
tools/a11y_audit.py` re-checks that claim — 63 checks covering contrast in both palettes,
language of parts, the tab keyboard contract, accessible names, reflow at 320 CSS pixels and
colour-independence of every status — and CI fails the build on a regression. See
**[docs/ACCESSIBILITY_CONFORMANCE.md](docs/ACCESSIBILITY_CONFORMANCE.md)** — a product about
accessibility that is not itself accessible would be self-refuting.

---

## Repository map

```
src/accesspulse/
  contracts.py        typed records for every agent/tool/storage boundary + the state graph
  registry.py         versioned accessibility promise registry (point-in-time reads)
  twin.py             the digital twin: versioned topology graph + blast-radius traversal
  media.py            "The Lumière Protocol" — original programme, dialogue, scenes
  simulator.py        the instrumented delivery chain; faults change what audiences receive
  faults.py           45 documented faults — the benchmark's ground truth, read by nothing else
  probes/             caption, audio-description, sign-feed and player probes + alignment
  probes/accelerated/ wavefront, Triton and CUDA alignment kernels — bit-identical to the
                      reference, 9.9× faster at 1024 tokens (docs/PERFORMANCE.md)
  slo.py              31 SLOs with per-tier objectives and error budgets — the source of truth
  assurance.py        sweep → SLO evaluation → error-budget burn → structured alerts
  incident.py         the 12-state machine, preconditions, hash-chained audit, persistence
  policy.py           policy as code (12 rules) + the 12-action allow-list catalog
  approvals.py        signed, single-use, expiring approval tokens
  executor.py         the only component permitted to change the environment
  verification.py     post-action assertion suites (original / adjacent / dependent scope)
  certification.py    the Accessibility Release Gate
  grafana_mcp/        MCP client: capability resolution, stdio/http transports, in-process server
  agents/             coordinator, scope, evidence, quality, correlation, diagnosis,
                      communication, learning + the Gemini/ADK reasoning plane
  telemetry.py        metrics, logs, traces, profiles, Grafana annotations and incidents
  api.py, web/        the product
observability/        docker-compose stack config, provisioned datasources, generated
                      dashboards and 31 alert rules
bench/                the benchmark harness, the probe calibration study, and their results
tools/                asset generation, accessibility audit, SBOM, Agent Engine deployment
training/             QLoRA specialist adapters: configs, dataset builder, assertion-based eval
ebpf/                 delivery-path kernel telemetry, correlated to media symptoms
infra/terraform/      Cloud Run, Secret Manager, evidence bucket — the trust boundary as code
docs/                 architecture, MCP chain, benchmark, performance, threat model, privacy,
                      accessibility conformance, model and dataset cards, media rights, ADRs,
                      demo script, judge instructions
```

## Documentation

| | |
|---|---|
| **[JUDGE.md](docs/JUDGE.md)** | **evaluate the whole thing in ten minutes, offline — start here** |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | the whole system, the trust boundaries, the cloud footprint |
| [MCP_CALL_CHAIN.md](docs/MCP_CALL_CHAIN.md) | every Grafana MCP call, why it is made, what it returns |
| [MCP_CONFORMANCE.md](docs/MCP_CONFORMANCE.md) | what the *official* server actually offers, measured — and what that breaks |
| [BENCHMARK.md](docs/BENCHMARK.md) | methodology, results, ablations, limitations |
| [PERFORMANCE.md](docs/PERFORMANCE.md) | the alignment kernels, measured — including where they lose |
| [THREAT_MODEL.md](docs/THREAT_MODEL.md) | what an attacker or a confused model can and cannot do |
| [PRIVACY.md](docs/PRIVACY.md) | why we never infer disability, and what we measure instead |
| [ACCESSIBILITY_CONFORMANCE.md](docs/ACCESSIBILITY_CONFORMANCE.md) | the product's own conformance |
| [model_card.md](docs/model_card.md) · [dataset_card.md](docs/dataset_card.md) | what the measurement models do and do not claim |
| [MEDIA_RIGHTS.md](docs/MEDIA_RIGHTS.md) | provenance of every asset |
| [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) · [RECORDING_CHECKLIST.md](docs/RECORDING_CHECKLIST.md) | the three-minute demonstration, and how to record it |
| [screenshots/](docs/screenshots/) | the seven product views, captured from a real run |
| [adr/](docs/adr/) | twelve decisions, with the alternatives rejected and what each one costs |
| [`sbom.json`](sbom.json) | CycloneDX software bill of materials — `python tools/generate_sbom.py` |

## Licence

Apache-2.0. See [LICENSE](LICENSE). The demonstration programme *The Lumière Protocol* — every
line of dialogue, every scene description, every translation — is original work created for this
project and licensed under the same terms. No third-party film, script, subtitle file or
recording is used anywhere in this repository.
