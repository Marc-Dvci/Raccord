# Judge mode — how to evaluate AccessPulse in ten minutes

**No credentials. No cloud account. No network. No downloads.**

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

accesspulse hero          # the closed loop, in the terminal, ~30 seconds
accesspulse serve         # the product, at http://localhost:8080
```

Or, if you would rather not install anything:

```bash
docker build -t accesspulse . && docker run --rm -p 8080:8080 accesspulse
docker run --rm accesspulse accesspulse hero
```

Everything below runs offline. The Grafana MCP server, Gemini and Google Cloud are all *real*
integrations, and all of them are optional to see the system work — §6 explains how to point it
at the real ones.

---

## 1. Two minutes: does the closed loop actually close?

```bash
accesspulse hero
```

You should see the Grafana MCP call chain print as it happens, then:

```
detected                        yes
scope                           DE/ES/FR/GB · ctv-9.3.1/ctv-9.4.0 · en
scope precision/recall          1.00 / 1.00
policy                          approval_required
approval                        t.duval@studio.example (event_technical_director)
action                          select_synchronized_standby
verification                    9/9 assertions passing
recovered                       yes
Grafana MCP calls               17
unsafe action                   no
audit chain                     yes
```

**What to look at:** *scope precision/recall 1.00* is measured against a fault specification the
agents never see. *9/9 assertions* includes three that prove French, German and Spanish
captions, the described audio and the interpreter feed were not regressed by the fix.

Machine-readable, if you prefer: `accesspulse hero --json run.json`.

---

## 2. Three minutes: the product

```bash
accesspulse serve      # http://localhost:8080
```

Seven views. The two worth your time:

- **Incident workspace** — evidence, ranked diagnosis with *contradicting* evidence, change
  correlation, policy, approval, verification and a hash-chained audit trail. Evidence,
  hypothesis, policy and verified result are structurally separated so they can never be
  confused for each other.
- **Agent & MCP observability** — every tool call, its latency, capability resolution, every
  agent step.

The UI is keyboard-operable throughout; try it with Tab and arrow keys. It is dependency-free —
no framework, no CDN, nothing loaded from a third-party host.

### Deterministic reset

```bash
curl -X POST http://localhost:8080/api/reset
```

Same seed, same timeline, clean incident store. Use it between runs; `test_reset_is_
deterministic` asserts it produces an identical state.

### A different fault

```bash
accesspulse faults                              # all 45
accesspulse hero --fault infra.stale_config     # the hardest class
```

---

## 3. Two minutes: is Grafana load-bearing, or decorative?

This is the question the track exists to ask, so here is how to check rather than take our word.

**The state machine will not let an incident leave `SCOPED` without Grafana MCP evidence** for
alerts, metrics, logs, traces *and* dashboards:

```bash
grep -A8 "REQUIRED_EVIDENCE_TOOLS" src/accesspulse/incident.py
pytest -q tests/test_mcp_and_loop.py -k "without_mcp or only_through_mcp"
```

`test_evidence_complete_is_unreachable_without_mcp_evidence` removes the MCP evidence and
asserts the incident is stuck. `test_the_investigation_goes_through_mcp_and_only_through_mcp`
asserts every evidence item in a completed incident carries an MCP `source_tool`. There is no
fallback path that queries Prometheus, Loki or Tempo directly.

**The dashboards and alert rules are generated from the SLO definitions**, so a panel threshold
cannot drift from the objective the probes are measured against:

```bash
python tools/generate_grafana_assets.py && git diff --stat observability/
```

No diff means the committed assets match the SLOs. CI fails the build if they do not.

---

## 4. Two minutes: the numbers, including the bad ones

```bash
cat bench/results/summary.json | head -40
```

Published run: 1,000 scenarios, 45 faults, seed 20260803.

| | |
|---|---|
| Detection rate | 1.000 |
| Scope precision / recall | 1.000 / 0.993 |
| Top-1 / top-3 root cause | 0.652 / 0.897 |
| Recovered **and verified** | 0.919 |
| **False closure rate** | **0.001** |
| **Unsafe action rate** | **0.000** |
| **Top-1 on the hardest band (n=147)** | **0.150** |

That last row is in this table on purpose. [BENCHMARK.md](BENCHMARK.md) §3 explains exactly
where the system is weak and why, and §4 reports an ablation whose result is *null* — probe
abstention shows no measurable system-level effect — rather than promoting it.

Re-run it yourself:

```bash
python -m bench.harness --scenarios 45 --no-ablations --workers 4   # ~3 minutes
accesspulse bench --scenarios 1000                                   # ~44 minutes on 7 workers
```

Two more, both fast:

```bash
python -m bench.calibration                                    # the measurement models
python -m accesspulse.probes.accelerated.benchmark             # the alignment kernels
```

---

## 5. One minute: the safety argument

```bash
pytest -q tests/test_approvals.py tests/test_policy.py tests/test_executor.py tests/test_state_machine.py
```

Thirty-odd tests covering the boundary: an uncatalogued action is refused; a non-allow-listed
target is refused; a tampered action hash is refused; a replayed token is refused; an expired
token is refused; a wrong-role approval is refused at issue time; a duplicate idempotency key
executes nothing; the state machine refuses every skipped transition; the audit chain detects a
mutated entry.

The one-line version: **the language model cannot decide anything, and cannot act at all.** The
default reasoning mode is offline, and the whole loop closes with the model removed
([ADR 0001](adr/0001-deterministic-core-decides.md), [ADR 0011](adr/0011-offline-reasoning-is-the-default.md)).

---

## 6. If you want to see the real integrations

**Real Grafana stack** — Grafana, Prometheus, Loki, Tempo, Pyroscope, five provisioned
dashboards and 31 alert rules:

```bash
docker compose up -d
accesspulse serve                 # Prometheus scrapes /metrics
open http://localhost:3000        # admin / accesspulse
```

**The official Grafana MCP server** instead of the in-process one — no agent code changes.
This is the whole track premise, so it is worth the extra commands:

```bash
docker compose up -d
./tools/grafana_token.sh                            # service-account token into .env
docker compose --profile mcp up -d mcp-grafana      # the official grafana/mcp-grafana
pip install -e ".[cloud]"                           # the MCP SDK

# .env
AP_MCP_TRANSPORT=http
AP_MCP_HTTP_URL=http://localhost:8000/mcp
AP_EXPORT_TELEMETRY=true      # so the stack has something to be asked about
```

```bash
python tools/mcp_conformance.py --transport http    # 65 tools, 18/20, 12/12 required
accesspulse serve
curl -X POST localhost:8080/api/inject -H 'content-type: application/json' \
     -d '{"fault_id":"cap.progressive_drift","ticks":10,"seconds_per_tick":20}'
curl -X POST 'localhost:8080/api/incident/run?auto_approve=true'
```

Then open the **Agent & MCP** tab. The header reads `http · 65 tools · N calls`, the call chain
carries network latencies rather than microseconds, and the capability table shows what had to
be translated to get there: `list_alert_rules → alerting_manage_rules`,
`query_tempo_traces → grafana_api_request`.

Our own run of exactly this is committed: [`real_mcp_run.json`](real_mcp_run.json) — 16 calls,
all successful, `REVIEWED`, 9/9 assertions, scope 1.00/1.00. The reasoning, and what it still
does **not** prove, is in [MCP_CONFORMANCE.md](MCP_CONFORMANCE.md).

**Gemini on Vertex AI:**

```bash
pip install -e ".[cloud]"
# .env
AP_REASONING_MODE=gemini
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<project>
```

**Deployment** — Cloud Run plus Agent Engine, with the trust boundary in the Terraform:

```bash
python tools/deploy_agent_engine.py --check      # preflight, deploys nothing
terraform -chdir=infra/terraform plan -var project_id=<project> -var image=<image> -var grafana_url=<url>
```

---

## 7. Where to look for what

| Question | File |
|---|---|
| How does it work? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Is Grafana really load-bearing? | [MCP_CALL_CHAIN.md](MCP_CALL_CHAIN.md) · §3 above |
| How good is it, and where is it bad? | [BENCHMARK.md](BENCHMARK.md) |
| What can the model do? | [THREAT_MODEL.md](THREAT_MODEL.md) §5 |
| Does it collect data about disabled viewers? | [PRIVACY.md](PRIVACY.md) — no, and §3 explains the structure that makes it impossible |
| Is the product itself accessible? | [ACCESSIBILITY_CONFORMANCE.md](ACCESSIBILITY_CONFORMANCE.md) — 63 automated checks, `python tools/a11y_audit.py` |
| What do the models claim? | [model_card.md](model_card.md) · [dataset_card.md](dataset_card.md) |
| Is the media licensed? | [MEDIA_RIGHTS.md](MEDIA_RIGHTS.md) — every asset is original; nothing third-party |
| Why is it built this way? | [adr/](adr/) — twelve decisions with the alternatives rejected |
| What is it made of? | [`sbom.json`](../sbom.json) |
| How do I record the demo? | [DEMO_SCRIPT.md](DEMO_SCRIPT.md) |

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `accesspulse: command not found` | The editable install puts it on PATH; otherwise `python -m accesspulse.cli hero` |
| An incident is already open | `curl -X POST http://localhost:8080/api/reset` |
| Port 8080 in use | `accesspulse serve --port 8090` |
| Approval token expired mid-demo | It is meant to: 300-second TTL. Reset and re-run. |
| Grafana not running | Everything still works — the in-process MCP server serves the same tool surface |
